"""QwenWork device-token refresh. Isolated from WorkBuddy refresh."""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from tls_context import TLS_CONTEXT

from providers.qwenwork.constants import (
    CLAIM_APP_MODE,
    CLAIM_BUILD,
    CLAIM_IDE_VERSION,
    CLAIM_RELEASE_VERSION,
    GATEWAY,
    OPENAPI_USER_AGENT,
    REFRESH_PATH,
)
from providers.qwenwork.store import iso_to_ms, read_official_session, write_refreshed_auth


class QwenWorkAuthError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def openapi_headers(request_id: str = "") -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": OPENAPI_USER_AGENT,
        "X-Request-Id": request_id or f"buddy2api-{int(time.time() * 1000)}",
        "X-QwenWork-Version": CLAIM_IDE_VERSION,
        "X-QwenWork-Release-Version": CLAIM_RELEASE_VERSION,
        "X-QwenWork-Build": CLAIM_BUILD,
        "X-QwenWork-Platform": "win32",
        "X-QwenWork-Arch": "x64",
        "X-QwenWork-Channel": "stable",
        "X-QwenWork-App-Mode": CLAIM_APP_MODE,
        "Content-Type": "application/json",
    }


def is_token_expired(account: dict, skew_ms: int = 300_000) -> bool:
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at - skew_ms


def _still_valid_until(account: dict) -> str:
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= int(time.time() * 1000):
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(expires_at / 1000))


def _invalid_refresh_message(account: dict) -> str:
    until = _still_valid_until(account)
    if until:
        return (
            f"续期令牌已失效。访问令牌仍可用到 {until}，领取不受影响。"
            "请在千问办公重新登录后再导入"
        )
    return "续期令牌已失效。请在千问办公重新登录后再导入"


def _response_error(response, account: dict) -> QwenWorkAuthError:
    payload = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            payload = parsed
    except ValueError:
        payload = {}
    code = str(payload.get("errorCode") or "")
    detail = str(payload.get("errorMessage") or payload.get("message") or "")
    upstream = payload.get("upstreamError")
    upstream_code = ""
    if isinstance(upstream, dict):
        upstream_code = str(upstream.get("code") or "")
        if not detail:
            detail = str(upstream.get("message") or "")
    rejected = (
        response.status_code in {401, 403}
        or code == "INVALID_REFRESH_TOKEN"
        or upstream_code == "invalid_grant"
    )
    if rejected and (code == "INVALID_REFRESH_TOKEN" or upstream_code == "invalid_grant" or response.status_code == 401):
        return QwenWorkAuthError(
            _invalid_refresh_message(account),
            status_code=response.status_code,
            code=code or "INVALID_REFRESH_TOKEN",
        )
    text = f"deviceToken refresh failed: HTTP {response.status_code}"
    if detail and detail != "None":
        text += f" {detail}"
    return QwenWorkAuthError(text[:240], status_code=response.status_code, code=code)


def _official_replacement(account: dict) -> dict | None:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    auth_path = str(extra.get("auth_path") or "").strip()
    try:
        official = read_official_session(Path(auth_path) if auth_path else None)
    except Exception:
        return None
    if not isinstance(official, dict):
        return None
    official_uid = str(official.get("uid") or "")
    account_uid = str(account.get("uid") or "")
    if official_uid and account_uid and official_uid != account_uid:
        return None
    official_refresh = str(official.get("refresh_token") or "")
    if not official_refresh or official_refresh == str(account.get("refresh_token") or ""):
        return None
    return official


def _adopt_official(account: dict, official: dict) -> dict:
    import database as db

    patch = {
        "access_token": official.get("access_token") or account.get("access_token") or "",
        "refresh_token": official.get("refresh_token") or "",
        "expires_at": official.get("expires_at") or account.get("expires_at") or 0,
        "refresh_expires_at": official.get("refresh_expires_at") or 0,
        "status": "active",
    }
    db.update_account(account["id"], patch)
    fresh = db.get_account(account["id"])
    return fresh or {**account, **patch}


async def _refresh_once(account: dict) -> dict:
    import database as db

    refresh = str(account.get("refresh_token") or "")
    if not refresh:
        raise QwenWorkAuthError("QwenWork account has no refresh_token")
    # Official 1.3.1 refresh sends only the refresh token. A bearer access token
    # makes the gateway judge the access token first and can hide a usable refresh.
    headers = openapi_headers()
    body = {"refresh_token": refresh, "target": "c"}
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30.0) as client:
        response = await client.post(f"{GATEWAY}{REFRESH_PATH}", headers=headers, json=body)
    if response.status_code >= 400:
        raise _response_error(response, account)
    try:
        data = response.json()
    except ValueError as exc:
        raise QwenWorkAuthError("deviceToken refresh returned non-JSON") from exc
    if not isinstance(data, dict):
        raise QwenWorkAuthError("deviceToken refresh returned non-JSON")
    token = data.get("device_token") or data.get("token") or ""
    new_refresh = data.get("refresh_token") or refresh
    if not token:
        raise QwenWorkAuthError("refresh response missing device_token")
    expires_at = iso_to_ms(data.get("expires_at"))
    if not expires_at and isinstance(data.get("expires_in"), (int, float)):
        expires_at = int((time.time() + float(data["expires_in"])) * 1000)
    refresh_expires_at = iso_to_ms(data.get("refresh_token_expires_at"))
    if not refresh_expires_at and isinstance(data.get("refresh_token_expires_in"), (int, float)):
        refresh_expires_at = int((time.time() + float(data["refresh_token_expires_in"])) * 1000)
    patch = {
        "access_token": token,
        "refresh_token": new_refresh,
        "expires_at": expires_at,
        "refresh_expires_at": refresh_expires_at,
        "status": "active",
    }
    db.update_account(account["id"], patch)
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    auth_path = extra.get("auth_path")
    if auth_path:
        try:
            write_refreshed_auth(Path(auth_path), patch)
        except Exception:
            pass
    fresh = db.get_account(account["id"])
    if not fresh:
        raise QwenWorkAuthError("account disappeared after refresh")
    return fresh


async def refresh_account(account: dict) -> dict:
    try:
        return await _refresh_once(account)
    except QwenWorkAuthError as exc:
        if exc.status_code not in {401, 403} and exc.code != "INVALID_REFRESH_TOKEN":
            raise
        replacement = _official_replacement(account)
        if replacement is None:
            raise
        adopted = _adopt_official(account, replacement)
        return await _refresh_once(adopted)
