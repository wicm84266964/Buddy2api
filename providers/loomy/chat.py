"""Loomy chat. The model gateway speaks OpenAI chat completions."""

from __future__ import annotations

import json
import time
from typing import AsyncGenerator

import httpx

from tls_context import TLS_CONTEXT

import rate_limiter

import auth_manager
import database as db
from context_budget import is_context_rejection
from providers.loomy.constants import CHANNEL_ID, MODEL_BASE, STATIC_MODELS
from providers.loomy.headers import model_headers
from providers.loomy.token import LoomyAuthError, is_expired, refresh_account

RETRYABLE_STATUS = {401, 403, 408, 429, 500, 502, 503, 504}


def translate_model(model: str) -> str:
    import aliases

    inner = (model or "auto").strip() or "auto"
    return aliases.resolve(CHANNEL_ID, inner)


def accepts_model(inner: str) -> bool:
    import aliases
    import catalog

    value = (inner or "").strip()
    if value in aliases.merged_map(CHANNEL_ID):
        return True
    models = catalog.models_for(CHANNEL_ID, [{"id": item} for item in STATIC_MODELS])
    ids = {str(item.get("id")) for item in models if isinstance(item, dict)}
    return value in ids


def _log(api_key_info, account, model_name, stream, prompt_t, completion_t, total_t,
         finish_reason, status_code, error_msg, t0, increment_usage=True):
    try:
        db.record_request(
            {
                "api_key_id": api_key_info["id"] if api_key_info else None,
                "api_key_name": api_key_info["name"] if api_key_info else None,
                "account_id": account["id"] if account else None,
                "account_name": account.get("name") if account else None,
                "provider": CHANNEL_ID,
                "model": model_name,
                "resolved_model": (api_key_info.get("_resolved_model")
                                   if isinstance(api_key_info, dict) else None) or model_name,
                "stream": 1 if stream else 0,
                "prompt_tokens": prompt_t,
                "completion_tokens": completion_t,
                "total_tokens": total_t,
                "credit": 0,
                "finish_reason": finish_reason,
                "duration_ms": int((time.time() - t0) * 1000),
                "status_code": status_code,
                "error_msg": error_msg,
                "increment_usage": increment_usage,
            }
        )
    except Exception:
        pass


def _prompt_tokens(payload: dict) -> int:
    import router

    return router.estimate_prompt_tokens(payload)


def _body(payload: dict) -> tuple[dict, str]:
    body = dict(payload)
    body["model"] = translate_model(str(body.get("model") or "auto"))
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw


async def _ready(account: dict) -> dict:
    if not account.get("access_token") or is_expired(account):
        return await refresh_account(account)
    return account


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = api_key_info.get("_log_model") if isinstance(api_key_info, dict) else None
    model_name = log_model if log_model is not None else payload.get("model", "auto")
    body, raw = _body(payload)
    if client_wants_stream:
        return ("stream", _stream(body, raw, api_key_info, model_name))
    tried: set[int] = set()
    last_error = None
    max_attempts = auth_manager.account_attempt_limit(CHANNEL_ID)
    for attempt in range(max_attempts):
        account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        try:
            account = await _ready(account)
            async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=120.0) as client:
                response = await client.post(
                    f"{MODEL_BASE}/chat/completions",
                    headers=model_headers(str(account.get("access_token") or "")),
                    content=raw,
                )
        except (httpx.HTTPError, LoomyAuthError) as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            continue
        if response.status_code < 400:
            auth_manager.mark_account_success(account["id"])
            try:
                data = response.json()
            except ValueError:
                data = {"id": "loomy", "object": "chat.completion", "choices": []}
            usage = data.get("usage") or {}
            _log(
                api_key_info, account, model_name, False,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                ((data.get("choices") or [{}])[0].get("finish_reason") or "stop"),
                response.status_code, "", t0,
            )
            return ("json", data)
        status = response.status_code
        detail = _detail(response)
        if is_context_rejection(status, detail, prompt_tokens=_prompt_tokens(body)):
            return ("error", (status, detail))
        if status in {401, 403} and attempt < max_attempts - 1:
            try:
                await refresh_account(account)
            except LoomyAuthError:
                pass
        auth_manager.mark_account_failure(account["id"], status)
        last_error = ("error", (status, detail))
        if status not in RETRYABLE_STATUS:
            return last_error
    return last_error or (
        "error",
        (503, {"error": {"message": "No available accounts", "type": "channel_unavailable", "code": "channel_unavailable", "channel": CHANNEL_ID}}),
    )


def _detail(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {"error": {"message": response.text[:400], "type": "server_error"}}
    return payload if isinstance(payload, dict) else {"error": {"message": str(payload)[:400], "type": "server_error"}}


async def _stream(body: dict, raw: str, api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b"data: {\"error\":{\"message\":\"No available accounts\"}}\n\n"
    max_attempts = auth_manager.account_attempt_limit(CHANNEL_ID)
    for _attempt in range(max_attempts):
        account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        started = False
        try:
            account = await _ready(account)
            async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{MODEL_BASE}/chat/completions",
                    headers=model_headers(str(account.get("access_token") or "")),
                    content=raw,
                ) as response:
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        detail = {"error": {"message": text, "type": "upstream_error"}}
                        if is_context_rejection(response.status_code, detail, prompt_tokens=_prompt_tokens(body)):
                            yield f"data: {json.dumps(detail, ensure_ascii=False)}\n\n".encode("utf-8")
                            return
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = f"data: {json.dumps(detail, ensure_ascii=False)}\n\n".encode()
                        if response.status_code not in RETRYABLE_STATUS:
                            yield last_error
                            return
                        continue
                    auth_manager.mark_account_success(account["id"])
                    async for line in response.aiter_lines():
                        if line == "":
                            continue
                        started = True
                        yield (line + "\n\n").encode("utf-8") if not line.endswith("\n") else (line + "\n").encode("utf-8")
            _log(api_key_info, account, model_name, True, 0, 0, 0, "stop", 200, "", t0)
            return
        except (httpx.HTTPError, LoomyAuthError) as exc:
            if started:
                return
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = f"data: {json.dumps({'error': {'message': str(exc)[:240]}}, ensure_ascii=False)}\n\n".encode()
    yield last_error


async def test_chat(account: dict, model: str = "auto", prompt: str = "ping") -> dict:
    payload = {
        "model": model or "auto",
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "stream": False,
        "max_tokens": 64,
    }
    t0 = time.time()
    _parsed, raw = _body(payload)
    try:
        ready = await _ready(account)
        async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=45.0) as client:
            response = await client.post(
                f"{MODEL_BASE}/chat/completions",
                headers=model_headers(str(ready.get("access_token") or "")),
                content=raw,
            )
    except (httpx.HTTPError, LoomyAuthError) as exc:
        return {"ok": False, "status_code": 0, "duration_ms": int((time.time() - t0) * 1000), "message": str(exc)[:240]}
    duration_ms = int((time.time() - t0) * 1000)
    if response.status_code >= 400:
        return {"ok": False, "status_code": response.status_code, "duration_ms": duration_ms, "message": response.text[:240]}
    try:
        data = response.json()
    except ValueError:
        data = {}
    text = ""
    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        text = str((message or {}).get("content") or "")
    return {"ok": True, "status_code": response.status_code, "duration_ms": duration_ms, "message": text or "ok"}
