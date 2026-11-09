"""Refresh Xiaohuanxiong access tokens."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx

from tls_context import TLS_CONTEXT

import database as db
from providers.xiaohuanxiong.constants import AUTH_BASE, POINTS_BASE, REFRESH_MARGIN_SECONDS
from providers.xiaohuanxiong.headers import client_headers


class XiaohuanxiongAuthError(RuntimeError):
    pass


# The desktop client (box-agent) writes only access_token + office_identity into
# auth.json -- there is no refresh_token anywhere under ~/.office-raccoon. So the
# usual refresh flow cannot work; instead we re-read the client's auth.json and
# adopt whatever token it currently holds. Re-login in the client rotates the
# access token, which is exactly the case this rescues.
_RESYNC_TTL_SECONDS = 300
_resync_cache: dict[int, float] = {}


def jwt_payload(token: str) -> dict:
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def jwt_exp_ms(token: str) -> int:
    try:
        exp = int(jwt_payload(token).get("exp") or 0)
    except (TypeError, ValueError):
        return 0
    if exp <= 0:
        return 0
    return exp * 1000 if exp < 10_000_000_000 else exp


def jwt_sub(token: str) -> str:
    return str(jwt_payload(token).get("sub") or "").strip()


def _mark_expired(account: dict) -> None:
    """Reflect a dead login in the DB so the admin page stops showing it as 正常."""
    account_id = int(account.get("id") or 0)
    if not account_id:
        return
    try:
        db.update_account(account_id, {"status": "expired"})
    except Exception:
        pass


def is_token_expired(account: dict, skew_seconds: int = REFRESH_MARGIN_SECONDS) -> bool:
    expires_at = int(account.get("expires_at") or 0) or jwt_exp_ms(str(account.get("access_token") or ""))
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at - skew_seconds * 1000


async def probe_token(token: str) -> bool:
    """Ask the upstream whether this access token still works.

    The desktop client never writes a refresh_token, so there is no refresh call to
    make. Without a real probe, renewing such an account was a silent no-op that
    reported "已续期" even after the server had revoked the session -- the user saw
    a green 续期成功 while every API call kept failing with 401.
    """
    token = str(token or "").strip()
    if not token:
        return False
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30.0) as client:
            response = await client.get(
                f"{POINTS_BASE}/balance",
                headers=client_headers(token),
            )
    except httpx.HTTPError:
        # A network blip must not be reported as a dead login.
        return True
    # Only an explicit authentication rejection proves the session is dead.
    # Billing outages, rate limiting and other failures must not expire it.
    return response.status_code not in {401, 403}


async def refresh_account(account: dict) -> dict:
    refresh = str(account.get("refresh_token") or "").strip()
    if not refresh:
        # No refresh_token exists to exchange. The upstream accepts the access
        # token itself as the refresh credential, so try that first -- it also
        # keeps a still-valid session alive instead of merely trusting its exp.
        access = str(account.get("access_token") or "").strip()
        if access and await probe_token(access):
            # Session is alive: rotate it for real. A failure here is reported, not
            # swallowed -- pretending it succeeded is what hid the dead account.
            try:
                return await _exchange(account, access)
            except XiaohuanxiongAuthError:
                _mark_expired(account)
                raise
        _mark_expired(account)
        raise XiaohuanxiongAuthError("小浣熊账号没有 refresh_token，请重新导入 auth.json")
    try:
        return await _exchange(account, refresh)
    except XiaohuanxiongAuthError:
        _mark_expired(account)
        raise


async def _exchange(account: dict, refresh: str) -> dict:
    body = json.dumps({"refresh_token": refresh}, separators=(",", ":"))
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30.0) as client:
            response = await client.post(
                f"{AUTH_BASE}/refresh",
                headers=client_headers(json_body=True),
                content=body.encode("utf-8"),
            )
    except httpx.HTTPError as exc:
        raise XiaohuanxiongAuthError(f"刷新 token 请求失败: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise XiaohuanxiongAuthError(f"刷新 token 失败: HTTP {response.status_code}") from exc
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    code = payload.get("code")
    if response.status_code >= 400 or (code not in (None, 0)):
        message = payload.get("message") or payload.get("msg") or response.text[:180]
        raise XiaohuanxiongAuthError(f"刷新 token 失败: HTTP {response.status_code} {message}")
    access = str(data.get("access_token") or "").strip()
    rotated = str(data.get("refresh_token") or "").strip() or refresh
    if not access:
        raise XiaohuanxiongAuthError("刷新 token 失败: 上游未返回 access_token")
    patch = {
        "access_token": access,
        "refresh_token": rotated,
        "expires_at": jwt_exp_ms(access),
        "status": "active",
    }
    db.update_account(int(account["id"]), patch)
    return db.get_account(account["id"]) or {**account, **patch}


def resync_from_disk(account: dict) -> dict | None:
    """Re-read the desktop client's auth.json and adopt its current access token.

    The box-agent client never exposes a refresh_token (its auth.json holds only
    access_token and office_identity), so a rotated/revoked token cannot be
    renewed through AUTH_BASE/refresh. The only way forward is to pick up whatever
    the client is holding now, which changes when the user re-logs-in there.

    Returns the patched account, or None when nothing usable was found on disk.
    """
    account_id = int(account.get("id") or 0)
    if account_id and _resync_cache.get(account_id, 0) > time.time():
        return None
    if account_id:
        _resync_cache[account_id] = time.time() + _RESYNC_TTL_SECONDS

    # Imported lazily: providers.xiaohuanxiong.store imports this module for
    # jwt_exp_ms, so a module-level import would be circular. Patch
    # providers.xiaohuanxiong.store.auth_candidates to influence these lookups.
    from providers.xiaohuanxiong import store as _store
    from providers.xiaohuanxiong.store import auth_candidates, session_to_account

    wanted = str(account.get("uid") or "").strip()
    current = str(account.get("access_token") or "").strip()
    for path in auth_candidates():
        try:
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            parsed = session_to_account(payload, source=str(path))
        except Exception:
            continue
        access = str(parsed.get("access_token") or "").strip()
        if not access or access == current:
            continue
        if wanted and str(parsed.get("uid") or "") and parsed["uid"] != wanted:
            continue
        patch = {
            "access_token": access,
            "refresh_token": parsed.get("refresh_token") or "",
            "expires_at": parsed.get("expires_at") or jwt_exp_ms(access),
            "status": "active",
        }
        db.update_account(account_id, patch)
        return db.get_account(account_id) or {**account, **patch}
    return None


async def ensure_ready(account: dict) -> dict:
    """Return an account with a usable token, re-syncing from disk when needed.

    Order: (1) still-valid token -> as is; (2) refresh_token present -> refresh;
    (3) desktop client's auth.json has a newer token -> adopt it; (4) fail loudly.
    """
    token = str(account.get("access_token") or "").strip()
    if token and not is_token_expired(account):
        # exp alone is not proof of life: the server revokes the session (sid) when
        # the user logs into a different account in the desktop client, and the JWT
        # still looks valid. Verify with a cheap request before trusting it.
        if await probe_token(token):
            return account
    if str(account.get("refresh_token") or "").strip():
        try:
            return await refresh_account(account)
        except XiaohuanxiongAuthError:
            pass
    fresh = resync_from_disk(account)
    if fresh and fresh.get("access_token"):
        return fresh
    _mark_expired(account)
    raise XiaohuanxiongAuthError(
        "小浣熊 access_token 已失效且没有 refresh_token；请在小浣熊客户端重新登录后重试"
    )
