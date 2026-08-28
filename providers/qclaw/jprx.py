"""jprx.m.qq.com business calls. Isolated from the WorkBuddy outbound stack."""

from __future__ import annotations

import httpx

from providers.qclaw.constants import (
    CMD_CREATE_API_KEY,
    CMD_MODEL_LIST,
    CMD_REFRESH_CHANNEL,
    CMD_TIME_SYNC,
    CMD_TODAY_TOKENS,
    CMD_USER_INFO,
    CMD_WX_LOGIN,
    CMD_WX_LOGIN_STATE,
    JPRX_GATEWAY,
    STATIC_MODELS,
    WEB_VERSION,
)
from providers.qclaw.sign import jprx_ctx


class JprxError(RuntimeError):
    def __init__(self, message: str, *, payload: dict | None = None, status_code: int = 0):
        super().__init__(message)
        self.payload = payload or {}
        self.status_code = status_code


def unwrap(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise JprxError("jprx response is not an object")
    ret = payload.get("ret")
    if ret not in (0, None):
        raise JprxError(str(payload.get("msg") or payload.get("message") or f"jprx ret={ret}"), payload=payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    resp = data.get("resp") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        resp = payload.get("resp") if isinstance(payload.get("resp"), dict) else data
    if not isinstance(resp, dict):
        return {}
    common = resp.get("common") if isinstance(resp.get("common"), dict) else {}
    code = common.get("code")
    if code not in (0, None):
        raise JprxError(str(common.get("message") or common.get("msg") or f"jprx code={code}"), payload=payload)
    inner = resp.get("data")
    if isinstance(inner, dict):
        return inner
    return resp


def _account_ids(account: dict) -> tuple[str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    guid = str(extra.get("guid") or account.get("guid") or "") or "1"
    user_id = str(account.get("uid") or extra.get("user_id") or "") or "1"
    jwt = str(account.get("refresh_token") or extra.get("jwt") or "")
    return guid, user_id, jwt


def build_headers(account: dict, body: str) -> dict[str, str]:
    guid, user_id, jwt = _account_ids(account)
    headers = {
        "Content-Type": "application/json",
        "X-Version": "1",
        "X-Token": jwt,
        "X-Guid": guid,
        "X-Account": user_id,
        "X-Session": "",
        "X-Qclaw-DeviceToken": guid if guid != "1" else "",
        "JPrx-Ctx": jprx_ctx(body, guid),
    }
    if jwt:
        headers["X-OpenClaw-Token"] = jwt
    return headers


def business_body(extra: dict | None = None) -> dict:
    payload = {"web_version": WEB_VERSION, "web_env": "release"}
    if extra:
        payload.update(extra)
    return payload


async def post_cmd(
    cmd: str,
    account: dict,
    extra: dict | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[dict, str | None]:
    import json as json_lib

    payload = business_body(extra)
    body = json_lib.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    url = f"{JPRX_GATEWAY}/data/{cmd}/forward"
    headers = build_headers(account, body)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, content=body)
    new_token = response.headers.get("X-New-Token")
    try:
        parsed = response.json()
    except ValueError as exc:
        raise JprxError(f"jprx HTTP {response.status_code} non-json", status_code=response.status_code) from exc
    if response.status_code >= 400:
        raise JprxError(f"jprx HTTP {response.status_code}", payload=parsed if isinstance(parsed, dict) else {}, status_code=response.status_code)
    return unwrap(parsed if isinstance(parsed, dict) else {}), new_token


def apply_new_token(account: dict, new_token: str | None) -> dict:
    if not new_token:
        return account
    import database as db

    aid = account.get("id")
    if aid:
        db.update_account(int(aid), {"refresh_token": new_token})
        fresh = db.get_account(int(aid))
        if fresh:
            return fresh
    updated = dict(account)
    updated["refresh_token"] = new_token
    return updated


async def time_sync(account: dict) -> str:
    data, token = await post_cmd(CMD_TIME_SYNC, account)
    apply_new_token(account, token)
    server_time = data.get("server_time")
    return str(server_time or "")


def parse_model_list(data: dict) -> list[dict]:
    rows = []
    if isinstance(data, dict):
        rows = data.get("model_status_list") or data.get("models") or []
    elif isinstance(data, list):
        rows = data
    models = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
            name = mid
            description = ""
        elif isinstance(row, dict):
            mid = str(row.get("id") or row.get("model_id") or "").strip()
            name = row.get("name") or row.get("display_id") or mid
            description = row.get("description") or ""
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append({"id": mid, "name": name, "description": description})
    return models


async def fetch_supplier_models(account: dict) -> list[dict]:
    """Live cmd 4320 list. Empty means no remote ids; caller decides fallback."""
    data, token = await post_cmd(CMD_MODEL_LIST, account)
    apply_new_token(account, token)
    return parse_model_list(data)


async def list_remote_models(account: dict) -> list[dict]:
    models = await fetch_supplier_models(account)
    return models or [{"id": item, "name": item} for item in STATIC_MODELS]


async def today_tokens(account: dict) -> dict:
    data, token = await post_cmd(CMD_TODAY_TOKENS, account)
    apply_new_token(account, token)
    return data


async def refresh_channel(account: dict) -> dict:
    data, token = await post_cmd(CMD_REFRESH_CHANNEL, account)
    account = apply_new_token(account, token)
    extra = dict(account.get("extra") or {})
    channel_token = data.get("openclaw_channel_token")
    if channel_token:
        extra["openclaw_channel_token"] = channel_token
        aid = account.get("id")
        if aid:
            import database as db

            db.update_account(int(aid), {"extra": extra})
    return data


async def create_api_key(account: dict) -> dict:
    data, token = await post_cmd(CMD_CREATE_API_KEY, account)
    apply_new_token(account, token)
    return data


async def get_user_info(account: dict, extra: dict | None = None) -> dict:
    data, token = await post_cmd(CMD_USER_INFO, account, extra)
    apply_new_token(account, token)
    return data


async def wx_login_state(account: dict, extra: dict | None = None) -> dict:
    data, token = await post_cmd(CMD_WX_LOGIN_STATE, account, extra)
    apply_new_token(account, token)
    return data


async def wx_login(account: dict, extra: dict) -> dict:
    data, token = await post_cmd(CMD_WX_LOGIN, account, extra)
    apply_new_token(account, token)
    return data
