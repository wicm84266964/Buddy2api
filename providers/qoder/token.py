"""Refresh a Qoder device or job token and write it back to the desktop file."""

from __future__ import annotations

import time

import httpx

from tls_context import TLS_CONTEXT_NO_ENV

import database as db
from providers.qoder.constants import region
from providers.qoder.store import write_refreshed_auth


class QoderAuthError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


def is_token_expired(account: dict, skew_ms: int = 300_000) -> bool:
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at - skew_ms


def _refresh_path(refresh_token: str) -> str:
    if str(refresh_token).startswith("jrt-"):
        return "/api/v1/jobToken/refresh"
    return "/api/v1/deviceToken/refresh"


def _first(payload: dict, names: tuple[str, ...]) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        return _first(data, names)
    return ""


async def refresh_account(account: dict) -> dict:
    channel = str(account.get("provider") or "qoder")
    refresh = str(account.get("refresh_token") or "").split("|", 1)[0].strip()
    if not refresh:
        raise QoderAuthError("Qoder 账号没有刷新凭证，请重新登录后再导入")
    url = f"{region(channel)['openapi']}{_refresh_path(refresh)}"
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT_NO_ENV, timeout=20.0, trust_env=False) as client:
            response = await client.post(
                url,
                headers={"User-Agent": "qoder-local-proxy", "Content-Type": "application/json"},
                json={"refresh_token": refresh},
            )
    except Exception as exc:
        raise QoderAuthError(str(exc)[:180]) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code in {401, 403}:
        raise QoderAuthError("Qoder 刷新令牌已失效或与所选地区不符，请重新登录", status_code=401)
    if response.status_code >= 400 or not isinstance(payload, dict):
        raise QoderAuthError(f"Qoder 续期失败 HTTP {response.status_code}", status_code=response.status_code)
    token = _first(payload, ("device_token", "deviceToken", "token", "access_token"))
    new_refresh = _first(payload, ("refresh_token", "refreshToken")) or refresh
    if not token or "|" in new_refresh:
        raise QoderAuthError("Qoder 续期响应缺少令牌，旧凭证未被覆盖")
    returned_uid = _first(payload, ("user_id", "uid", "userId"))
    current_uid = str(account.get("uid") or "")
    if returned_uid and current_uid and returned_uid != current_uid:
        raise QoderAuthError("Qoder 续期返回了不同账号，旧凭证未被覆盖")
    expires_at = int(time.time() * 1000) + 29 * 24 * 3600 * 1000
    refresh_expires_at = int(account.get("refresh_expires_at") or 0)
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    if extra.get("auth_path"):
        write_refreshed_auth(channel, token, new_refresh, expires_at, refresh_expires_at)
    if account.get("id"):
        db.update_account(int(account["id"]), {
            "access_token": token,
            "refresh_token": new_refresh,
            "expires_at": expires_at,
            "status": "active",
        })
        fresh = db.get_account(int(account["id"]))
        if fresh:
            return fresh
    account = dict(account)
    account["access_token"] = token
    account["refresh_token"] = new_refresh
    account["expires_at"] = expires_at
    return account
