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
from providers.usage import credit_amount, StreamUsage, request_stream_usage, token_counts
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
         finish_reason, status_code, error_msg, t0, increment_usage=True, *, usage=None):
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
                "credit": credit_amount(usage),
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
    request_stream_usage(body)
    body["model"] = translate_model(str(body.get("model") or "auto"))
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw


async def _ready(account: dict) -> dict:
    if not account.get("access_token") or is_expired(account):
        return await refresh_account(account)
    return account


def _request_error(exc: Exception, timeout: int) -> tuple[int, str]:
    text = str(exc).strip()
    detail = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    if isinstance(exc, httpx.ReadTimeout):
        return 504, f"Loomy upstream response timed out after {timeout}s ({detail})"[:240]
    return 503, detail[:240]


async def _post_collected(client, account: dict, raw: str) -> httpx.Response:
    """Keep upstream SSE active while returning one JSON response to the client."""
    async with client.stream(
        "POST", f"{MODEL_BASE}/chat/completions",
        headers=model_headers(str(account.get("access_token") or "")), content=raw,
    ) as response:
        if response.status_code >= 400 or "application/json" in response.headers.get("content-type", ""):
            await response.aread()
            return response
        result = {"object": "chat.completion"}
        choices = {}
        usage = {}
        done = False
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                break
            try:
                event = json.loads(data)
            except ValueError as exc:
                raise httpx.RemoteProtocolError("Loomy returned malformed SSE JSON") from exc
            if not isinstance(event, dict):
                raise httpx.RemoteProtocolError("Loomy returned a non-object SSE event")
            if event.get("error"):
                return httpx.Response(503, json=event)
            for key in ("id", "created", "model", "system_fingerprint", "service_tier"):
                if key in event:
                    result[key] = event[key]
            if isinstance(event.get("usage"), dict):
                usage.update(event["usage"])
            for choice in event.get("choices") or []:
                index = choice.get("index", 0)
                slot = choices.setdefault(index, {
                    "index": index, "message": {"role": "assistant", "content": ""},
                    "finish_reason": None, "_tools": {},
                })
                message = slot["message"]
                delta = choice.get("delta") or {}
                for key in ("content", "reasoning_content", "refusal"):
                    if isinstance(delta.get(key), str):
                        message[key] = (message.get(key) or "") + delta[key]
                for call in delta.get("tool_calls") or []:
                    tool = slot["_tools"].setdefault(call.get("index", 0), {
                        "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                    })
                    if call.get("id"):
                        tool["id"] = call["id"]
                    if call.get("type"):
                        tool["type"] = call["type"]
                    for key in ("name", "arguments"):
                        value = (call.get("function") or {}).get(key)
                        if value:
                            tool["function"][key] += value
                if choice.get("finish_reason"):
                    slot["finish_reason"] = choice["finish_reason"]
        if not done or not choices or any(not choice["finish_reason"] for choice in choices.values()):
            raise httpx.RemoteProtocolError("Loomy stream ended before a complete response")
        result["choices"] = []
        for _, choice in sorted(choices.items()):
            tools = choice.pop("_tools")
            if tools:
                choice["message"]["tool_calls"] = [tool for _, tool in sorted(tools.items())]
            result["choices"].append(choice)
        result["usage"] = usage
        return httpx.Response(200, json=result)


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = api_key_info.get("_log_model") if isinstance(api_key_info, dict) else None
    model_name = log_model if log_model is not None else payload.get("model", "auto")
    # Non-streaming callers (including compaction) still receive JSON. Sending
    # SSE upstream avoids its reverse proxy timing out while buffering output.
    body, raw = _body({**payload, "stream": True})
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
        timeout = auth_manager.request_timeout(300)
        try:
            account = await _ready(account)
            async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=timeout) as client:
                response = await _post_collected(client, account, raw)
        except (httpx.HTTPError, LoomyAuthError) as exc:
            status, message = _request_error(exc, timeout)
            # A slow response belongs to this request, not to account health.
            if status != 504:
                auth_manager.mark_account_failure(account["id"], status)
            last_error = ("error", (status, {"error": {"message": message, "type": "upstream_timeout" if status == 504 else "server_error"}}))
            final = attempt == max_attempts - 1
            _log(api_key_info, account, model_name, False, 0, 0, 0,
                 "error" if final else "retry", status, message, t0,
                 increment_usage=final)
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
                *token_counts(usage),
                ((data.get("choices") or [{}])[0].get("finish_reason") or "stop"),
                response.status_code, "", t0, usage=usage,
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
        if status != 504:
            auth_manager.mark_account_failure(account["id"], status)
        last_error = ("error", (status, detail))
        final = status not in RETRYABLE_STATUS or attempt == max_attempts - 1
        _log(api_key_info, account, model_name, False, 0, 0, 0,
             "error" if final else "retry", status, str(detail)[:400], t0,
             increment_usage=final)
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
        if response.status_code == 504:
            return {"error": {"message": "Loomy upstream gateway timed out (HTTP 504)", "type": "upstream_timeout", "code": "upstream_timeout"}}
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
        observed = StreamUsage()
        accepted = False
        completed = False
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
                        if response.status_code != 504:
                            auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = f"data: {json.dumps(detail, ensure_ascii=False)}\n\n".encode()
                        final = response.status_code not in RETRYABLE_STATUS or _attempt == max_attempts - 1
                        _log(api_key_info, account, model_name, True, 0, 0, 0,
                             "error" if final else "retry", response.status_code, text, t0,
                             increment_usage=final)
                        if response.status_code not in RETRYABLE_STATUS:
                            yield last_error
                            return
                        continue
                    auth_manager.mark_account_success(account["id"])
                    accepted = True
                    async for line in response.aiter_lines():
                        observed.observe(line)
                        if line == "":
                            continue
                        if line.strip() == "data: [DONE]":
                            completed = True
                        started = True
                        yield (line + "\n\n").encode("utf-8") if not line.endswith("\n") else (line + "\n").encode("utf-8")
            completed = True
            return
        except (httpx.HTTPError, LoomyAuthError) as exc:
            observed.error = f"{type(exc).__name__}: {str(exc).strip() or type(exc).__name__}"[:240]
            if started:
                return
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = f"data: {json.dumps({'error': {'message': observed.error}}, ensure_ascii=False)}\n\n".encode()
            if not accepted:
                final = _attempt == max_attempts - 1
                _log(api_key_info, account, model_name, True, 0, 0, 0,
                     "error" if final else "retry", 503, observed.error, t0,
                     increment_usage=final)
        finally:
            if accepted:
                error = observed.error or ("stream interrupted" if not completed else "")
                _log(api_key_info, account, model_name, True, *observed.counts,
                     "error" if error else observed.finish, 502 if error else 200, error, t0, usage=observed.usage)
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
    timeout = auth_manager.request_timeout(300)
    try:
        ready = await _ready(account)
        async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=timeout) as client:
            response = await client.post(
                f"{MODEL_BASE}/chat/completions",
                headers=model_headers(str(ready.get("access_token") or "")),
                content=raw,
            )
    except (httpx.HTTPError, LoomyAuthError) as exc:
        _status, message = _request_error(exc, timeout)
        return {"ok": False, "status_code": 0, "duration_ms": int((time.time() - t0) * 1000), "message": message}
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
