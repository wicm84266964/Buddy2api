"""WorkBuddy browser login via the official CLI device-authorization flow.

The panel asks the upstream for an auth URL, the operator signs in in a browser,
then we poll until tokens arrive and store them in the encrypted account table.
"""

from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from tls_context import TLS_CONTEXT

import auth_manager
import fingerprint

LOGIN_TTL_SEC = 600
MAX_SESSIONS = 32
_STATE_RE = re.compile(r"^[A-Za-z0-9._-]{8,256}$")
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AUTH_HOSTS = {"tencent.com", "codebuddy.cn", "workbuddy.ai", "qq.com", "qcloud.com"}
_AUTH_SUFFIXES = (".tencent.com", ".codebuddy.cn", ".workbuddy.ai", ".qq.com", ".qcloud.com")

_CN_ORIGIN = "https://www.codebuddy.cn"
_GLOBAL_BASE = "https://www.workbuddy.ai"
_GLOBAL_ORIGIN = "https://www.workbuddy.ai"
_GLOBAL_DOMAIN = "www.workbuddy.ai"

_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def reset_sessions() -> None:
    with _lock:
        _sessions.clear()


def _purge_locked(now: float | None = None) -> None:
    moment = time.time() if now is None else now
    dead = [key for key, item in _sessions.items() if moment - float(item.get("created") or 0) > LOGIN_TTL_SEC]
    for key in dead:
        _sessions.pop(key, None)
    if len(_sessions) <= MAX_SESSIONS:
        return
    oldest = sorted(_sessions, key=lambda key: float(_sessions[key].get("created") or 0))
    for key in oldest[: len(_sessions) - MAX_SESSIONS]:
        _sessions.pop(key, None)


def _realm(value: str) -> str:
    return "global" if str(value or "").strip().lower() == "global" else "cn"


def _endpoints(realm: str) -> tuple[str, str, str, str, str]:
    if realm == "global":
        base = _GLOBAL_BASE
        origin = _GLOBAL_ORIGIN
        domain = _GLOBAL_DOMAIN
    else:
        base = auth_manager.backend_url()
        origin = _CN_ORIGIN
        domain = "www.codebuddy.cn"
    return (
        f"{base}/v2/plugin/auth/state?platform=CLI",
        f"{base}/v2/plugin/auth/token?state=",
        f"{base}/v2/plugin/login/account?state=",
        origin,
        domain,
    )


def accepted_auth_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    allowed = host in _AUTH_HOSTS or host.endswith(_AUTH_SUFFIXES)
    if parsed.scheme != "https" or not host or not allowed:
        raise ValueError(f"upstream auth URL host was rejected: {host or 'empty'}")
    return parsed.geturl()


def _public_message(payload: dict | None, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    text = str(payload.get("msg") or payload.get("message") or "").strip()
    lowered = text.lower()
    if not text or "accesstoken" in lowered or "refreshtoken" in lowered or "bearer " in lowered:
        return fallback
    return text[:180]


def _loads(response: httpx.Response) -> dict | None:
    raw = response.content[:1_000_000]
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _data_of(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


async def _exchange(method: str, url: str, origin: str, bearer: str = "") -> tuple[int, dict | None]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin.rstrip("/") + "/",
        "User-Agent": fingerprint.user_agent(),
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    body = b"{}" if method == "POST" else None
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30) as client:
        response = await client.request(method, url, headers=headers, content=body)
    return response.status_code, _loads(response)


def _mask_uid(uid: str) -> str:
    if len(uid) <= 8:
        return uid
    return uid[:8] + "…"


def _account_fields(payload: dict) -> dict:
    nested = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    source = nested or payload
    return {
        "uid": str(source.get("uid") or payload.get("uid") or "").strip(),
        "nickname": str(source.get("nickname") or payload.get("nickname") or "").strip(),
        "enterprise_id": str(
            source.get("enterpriseId") or source.get("enterprise_id") or payload.get("enterpriseId") or ""
        ).strip(),
        "phone": str(source.get("phoneNumber") or source.get("phone") or "").strip(),
        "account_type": str(source.get("type") or source.get("account_type") or "personal").strip() or "personal",
    }


def _public_result(saved: dict, nickname: str, uid: str) -> dict:
    return {
        "done": True,
        "id": saved["id"],
        "updated": bool(saved.get("updated")),
        "provider": "workbuddy",
        "nickname": nickname,
        "uid_masked": _mask_uid(uid),
        "status": "ok",
    }


async def start_login(realm: str) -> dict:
    selected = _realm(realm)
    state_url, _, _, origin, _domain = _endpoints(selected)
    try:
        status, payload = await _exchange("POST", state_url, origin)
    except httpx.HTTPError as exc:
        raise ValueError("auth state request failed") from exc
    data = _data_of(payload)
    code = payload.get("code") if isinstance(payload, dict) else None
    state = str(data.get("state") or "").strip()
    auth_url = str(data.get("authUrl") or data.get("auth_url") or data.get("url") or "").strip()
    if status >= 300 or code not in {0, None, "0"} or not _STATE_RE.fullmatch(state):
        raise ValueError(_public_message(payload, "auth state was rejected"))
    safe_url = accepted_auth_url(auth_url)
    with _lock:
        _purge_locked()
        _sessions[state] = {"created": time.time(), "realm": selected, "result": None}
    return {"ok": True, "url": safe_url, "state": state, "realm": selected}


async def poll_login(state: str) -> dict:
    key = str(state or "").strip()
    if not _STATE_RE.fullmatch(key):
        raise LookupError("unknown or expired state")
    with _lock:
        _purge_locked()
        session = _sessions.get(key)
        cached = session.get("result") if session else None
    if session is None:
        raise LookupError("unknown or expired state")
    if isinstance(cached, dict):
        return cached

    _state_url, token_url, account_url, origin, default_domain = _endpoints(session["realm"])
    try:
        status, payload = await _exchange("GET", token_url + key, origin)
    except httpx.HTTPError:
        return {"done": False, "message": "等待登录，上游暂时不可达"}
    data = _data_of(payload)
    code = payload.get("code") if isinstance(payload, dict) else None
    access = str(data.get("accessToken") or "").strip()
    if status >= 500 or not isinstance(payload, dict):
        return {"done": False, "message": "等待登录"}
    if status >= 300 or code not in {0, None, "0"} or not access:
        return {"done": False, "message": _public_message(payload, "等待浏览器登录完成")}

    refresh = str(data.get("refreshToken") or "").strip()
    now_ms = int(time.time() * 1000)
    account_payload: dict = {}
    try:
        account_status, account_body = await _exchange("GET", account_url + key, origin, bearer=access)
    except httpx.HTTPError:
        account_status, account_body = 0, None
    if account_status < 300 and isinstance(account_body, dict) and account_body.get("code", 0) in {0, None, "0"}:
        account_payload = _data_of(account_body)
    fields = _account_fields(account_payload)
    uid = fields["uid"]
    if not _UID_RE.fullmatch(uid):
        raise ValueError("login finished but account id was rejected")
    domain = str(data.get("domain") or default_domain).strip() or default_domain
    parsed = {
        "name": fields["nickname"] or uid[:8],
        "uid": uid,
        "nickname": fields["nickname"],
        "phone": fields["phone"],
        "account_type": fields["account_type"],
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": auth_manager.epoch_ms(data.get("expiresAt"), now_ms=now_ms, seconds=data.get("expiresIn")),
        "refresh_expires_at": auth_manager.epoch_ms(
            data.get("refreshExpiresAt"), now_ms=now_ms, seconds=data.get("refreshExpiresIn")
        ),
        "domain": domain,
        "enterprise_id": fields["enterprise_id"],
        "session_state": str(data.get("sessionState") or "").strip(),
        "provider": "workbuddy",
        "status": "active",
    }
    saved = auth_manager.upsert_login_account(parsed)
    result = _public_result(saved, fields["nickname"], uid)
    with _lock:
        current = _sessions.get(key)
        if current is not None:
            current["result"] = result
    return result
