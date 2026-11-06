"""Keep a Loomy session alive. The upstream has no refresh token."""

from __future__ import annotations

import asyncio
import time

import httpx

import credential_crypto
import database as db
from providers.loomy.account import LoomyAuthError, login_by_password, new_identity
from providers.loomy.constants import MODEL_BASE, RENEW_WITHIN_SECONDS, SESSION_SECONDS
from providers.loomy.headers import model_headers
from providers.loomy.store import newest_local_session, upsert_account


def extra_of(account: dict) -> dict:
    extra = account.get("extra")
    return extra if isinstance(extra, dict) else {}


def password_of(account: dict) -> str:
    extra = extra_of(account)
    stored = str(extra.get("password_enc") or "")
    if not stored:
        return ""
    try:
        return credential_crypto.decrypt_secret(stored, db.DB_PATH) or ""
    except credential_crypto.CredentialCryptoError:
        return ""


def is_near_expiry(account: dict, within_seconds: int = RENEW_WITHIN_SECONDS) -> bool:
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at - within_seconds * 1000


def is_expired(account: dict) -> bool:
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at


_PROBE_RETRY_SECONDS = 0.4


def _transport_message(exc: Exception) -> str:
    detail = " ".join(str(exc).split())
    name = type(exc).__name__
    if detail and detail != name:
        return f"{name}: {detail}"[:160]
    return name


async def _probe_session(session: str) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                return await client.get(
                    f"{MODEL_BASE}/points/records?record_type=all&page_no=1&page_size=1",
                    headers=model_headers(session, purpose="chat.message"),
                )
        except httpx.HTTPError as exc:
            last = exc
            if attempt == 0:
                await asyncio.sleep(_PROBE_RETRY_SECONDS)
                continue
            raise LoomyAuthError(f"Loomy 保活请求失败: {_transport_message(exc)}") from exc
    raise LoomyAuthError(f"Loomy 保活请求失败: {_transport_message(last or RuntimeError('unknown'))}")


async def _login_account(account: dict, loginid: str, password: str) -> dict:
    identity = extra_of(account).get("identity")
    if not isinstance(identity, dict):
        identity = new_identity()
    got = await login_by_password(loginid, password, identity)
    parsed = {
        "name": got.get("phone") or loginid,
        "nickname": got.get("phone") or loginid,
        "uid": got.get("userid") or account.get("uid") or loginid,
        "provider": "loomy",
        "access_token": got["session"],
        "expires_at": int(time.time() * 1000) + SESSION_SECONDS * 1000,
        "status": "active",
        "extra": {
            **extra_of(account),
            "phone": got.get("phone") or loginid,
            "loginid": loginid,
            "identity": identity,
        },
    }
    info = upsert_account(parsed)
    fresh = db.get_account(info["id"])
    if not fresh:
        raise LoomyAuthError("Loomy 重新登录成功，但无法读取已保存的账号")
    return fresh


async def refresh_account(account: dict) -> dict:
    uid = str(account.get("uid") or "")
    local = newest_local_session(uid)
    current_expiry = int(account.get("expires_at") or 0)
    if local and int(local.get("expires_at") or 0) > current_expiry:
        info = upsert_account(local)
        fresh = db.get_account(info["id"])
        if fresh:
            # A newer file is only a candidate; it may already be revoked.
            account = fresh
    loginid = str(extra_of(account).get("loginid") or extra_of(account).get("phone") or account.get("nickname") or "")
    password = password_of(account)
    if password and loginid and (is_near_expiry(account) or is_expired(account) or not account.get("access_token")):
        return await _login_account(account, loginid, password)
    session = str(account.get("access_token") or "")
    if session and not is_expired(account):
        response = await _probe_session(session)
        if response.status_code not in {401, 403}:
            if response.status_code >= 400:
                raise LoomyAuthError(f"Loomy 保活失败: HTTP {response.status_code}")
            return account
    # The server may revoke a session before its locally estimated expiry.
    if password and loginid:
        return await _login_account(account, loginid, password)
    raise LoomyAuthError("Loomy 登录已失效。重新导入客户端会话，或在会话 JSON 里带上账号密码后再保活")
