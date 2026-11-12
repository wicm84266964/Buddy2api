"""QwenWork chat client. Isolated COSY/SSE stack; never uses WorkBuddy fingerprint."""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncGenerator

import httpx

from tls_context import TLS_CONTEXT

import rate_limiter

import auth_manager
import database as db
from providers.usage import credit_amount, token_counts
from reasoning_controls import resolve_reasoning_control
from providers.qwenwork import cosy
from providers.qwenwork.encode import EncodeError
from providers.qwenwork.constants import (
    BUILD,
    BUSINESS_PRODUCT,
    BUSINESS_TYPE,
    CHANNEL_ID,
    CHAT_PATH,
    CHAT_QUERY,
    CLIENT_TYPE,
    COSY_VERSION,
    DATA_POLICY,
    GATEWAY,
    IDE_VERSION,
    LOGIN_VERSION,
    MACHINE_OS,
    MACHINE_TYPE,
    RELEASE_VERSION,
    RETRYABLE_STATUS,
    SCENE,
    USER_AGENT,
)
from providers.qwenwork.token import is_token_expired, refresh_account


def translate_model(model: str) -> str:
    import aliases

    inner = (model or "qwork-advanced").strip() or "qwork-advanced"
    return aliases.resolve(CHANNEL_ID, inner)


def chat_url() -> str:
    return f"{GATEWAY}{CHAT_PATH}?{CHAT_QUERY}"


def _packed_request(account: dict, raw: str, model: str) -> tuple[str, dict[str, str], str]:
    from providers.qwenwork.encode import prepare_infer

    uid, name, email, token = _ids(account)
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    machine_id = str(extra.get("login_device_id") or "")
    prepared = prepare_infer(
        body=raw,
        model_key=model,
        machine_id=machine_id,
        uid=uid,
        name=name,
        email=email,
        access_token=token,
    )
    headers = static_headers(model, str(uuid.uuid4()), machine_id)
    headers.update(prepared.headers)
    return prepared.url, headers, prepared.body


def _ids(account: dict) -> tuple[str, str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    uid = str(account.get("uid") or extra.get("uid") or "")
    name = str(account.get("nickname") or account.get("name") or extra.get("name") or "")
    email = str(extra.get("email") or "")
    token = str(account.get("access_token") or "")
    return uid, name, email, token


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


def _split_messages(payload: dict) -> tuple[str, list]:
    system_parts = []
    messages = []
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or "user"
        if role == "system":
            content = item.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if content:
                system_parts.append(str(content))
            continue
        messages.append(item)
    return "\n\n".join(system_parts), messages


def _last_user_text(messages: list) -> str:
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
    return ""


def build_body(payload: dict) -> tuple[dict, str, str]:
    model = translate_model(str(payload.get("model") or "qwork-advanced"))
    request_id = str(payload.get("request_id") or uuid.uuid4())
    session_id = str(payload.get("session_id") or uuid.uuid4())
    system, messages = _split_messages(payload)
    if system:
        messages = [{"role": "system", "content": system}, *messages]
    last_user = _last_user_text(messages)
    reasoning_control = resolve_reasoning_control(payload)
    is_reasoning = reasoning_control.enabled is True
    parameters = {}
    for key in ("temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty"):
        if key in payload and payload[key] is not None:
            parameters[key] = payload[key]
    if "max_tokens" not in parameters:
        parameters["max_tokens"] = 32000
    body = {
        "request_id": request_id,
        "request_set_id": str(payload.get("request_set_id") or request_id),
        "chat_record_id": str(payload.get("chat_record_id") or request_id),
        "session_id": session_id,
        "stream": True,
        "chat_task": "FREE_INPUT",
        "chat_context": {
            "text": last_user,
            "features": [],
            "extra": {
                "context": [],
                "modelConfig": {"key": model, "is_reasoning": is_reasoning},
                "originalContent": last_user,
            },
            "chatPrompt": "",
            "imageUrls": None,
        },
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "agent_id": "agent_common",
        "task_id": str(payload.get("task_id") or "common"),
        "session_type": "qoder_work",
        "aliyun_user_type": "",
        "model_config": {
            "key": model,
            "display_name": model,
            "model": "",
            "format": "openai",
            "is_vl": model in {
                "qwork-auto", "qwork-advanced", "qwork-ultimate",
                "pro", "flash", "qwen3.8-max-preview",
            },
            "is_reasoning": is_reasoning,
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": 1000000,
        },
        "system": system,
        "messages": messages,
        "tools": payload.get("tools") or [],
        "parameters": parameters,
        # Catalog lookup reads these fields. Cosy-Business-* headers do not replace them.
        "business": {
            "product": BUSINESS_PRODUCT,
            "type": BUSINESS_TYPE,
            "version": "1",
            "feature_switches": {},
        },
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw, model


def static_headers(model: str, request_id: str, machine_id: str = "") -> dict[str, str]:
    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Request-Id": request_id,
        "X-QwenWork-Version": IDE_VERSION,
        "X-QwenWork-Release-Version": RELEASE_VERSION,
        "X-QwenWork-Build": BUILD,
        "X-QwenWork-Platform": "win32",
        "X-QwenWork-Arch": "x64",
        "X-QwenWork-Channel": "stable",
        "Cosy-Version": COSY_VERSION,
        "Cosy-ClientType": CLIENT_TYPE,
        "Cosy-Business-Product": BUSINESS_PRODUCT,
        "Cosy-Business-Type": BUSINESS_TYPE,
        "Cosy-Scene": SCENE,
        "Cosy-MachineOS": MACHINE_OS,
        "Cosy-MachineType": MACHINE_TYPE,
        "Cosy-Data-Policy": DATA_POLICY,
        "Login-Version": LOGIN_VERSION,
        "X-Model-Key": model,
        "X-Model-Source": "system",
        "x-model-key": model,
        "x-internal-model-key": model,
        "x-model-source": "system",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Accept-Encoding": "identity",
    }
    if machine_id:
        headers["Cosy-MachineId"] = machine_id
        headers["Cosy-MachineToken"] = machine_id
    return headers


def _headers_for(account: dict, url: str, body: str, model: str, request_id: str) -> dict[str, str]:
    uid, name, email, token = _ids(account)
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    machine_id = str(extra.get("login_device_id") or "")
    headers = static_headers(model, request_id, machine_id)
    headers.update(
        cosy.auth_headers(
            uid=uid,
            name=name,
            email=email,
            access_token=token,
            url=url,
            body=body,
            timestamp=int(time.time()),
            request_id=uuid.uuid4().hex,
        )
    )
    return headers


def envelope_error(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        outer = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(outer, dict):
        return None
    status = outer.get("statusCodeValue")
    inner = outer.get("body")
    payload = inner
    if isinstance(inner, str):
        try:
            payload = json.loads(inner)
        except json.JSONDecodeError:
            payload = {"message": inner}
    if isinstance(status, int) and status >= 400:
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("code") or inner or status)
        return str(inner or status)
    if isinstance(payload, dict) and str(payload.get("code") or "") in {"400", "401", "403"}:
        return str(payload.get("message") or payload.get("code"))
    return None


def usage_from_payload(raw: str) -> tuple[dict, str]:
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return {}, ""
    if not isinstance(item, dict):
        return {}, ""
    usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
    choices = item.get("choices") if isinstance(item.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    finish = str(choice.get("finish_reason") or "")
    return usage, finish


def unwrap_sse_payload(raw: str) -> list[str]:
    """Peel the QwenWork outer envelope; return inner OpenAI SSE data payloads."""
    text = raw.strip()
    if not text or text == "[DONE]" or text == "{}":
        return []
    if envelope_error(text):
        return []
    try:
        outer = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if not isinstance(outer, dict):
        return [text]
    if "choices" in outer or outer.get("object") in {"chat.completion.chunk", "chat.completion"}:
        return [text]
    inner = outer.get("body")
    if inner is None:
        return []
    if isinstance(inner, (dict, list)):
        dumped = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
        return [] if dumped in {"{}", "[]"} else [dumped]
    inner_text = str(inner).strip()
    if not inner_text or inner_text in {"[DONE]", "{}", "null"}:
        return []
    return [inner_text]


def _aggregate(chunks: list[dict], model: str) -> dict:
    content = []
    reasoning = []
    tool_calls: dict[int, dict] = {}
    finish = "stop"
    usage = {}
    response_id = "qwenwork"
    for item in chunks:
        if not isinstance(item, dict):
            continue
        response_id = item.get("id") or response_id
        if isinstance(item.get("usage"), dict):
            usage.update(item["usage"])
        choice = (item.get("choices") or [{}])[0]
        finish = choice.get("finish_reason") or finish
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content.append(str(delta["content"]))
        if delta.get("reasoning_content"):
            reasoning.append(str(delta["reasoning_content"]))
        for call in delta.get("tool_calls") or []:
            index = int(call.get("index") or 0)
            slot = tool_calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if call.get("id"):
                slot["id"] = call["id"]
            if call.get("type"):
                slot["type"] = call["type"]
            fn = call.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += str(fn["arguments"])
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {
        "id": response_id,
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _pick(tried: set[int]) -> dict | None:
    account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
    if account and is_token_expired(account):
        try:
            account = await refresh_account(account)
        except Exception:
            account = None
    if account:
        return account
    expired = [
        row
        for row in db.list_accounts(provider=CHANNEL_ID)
        if row.get("status") == "expired" and row.get("id") not in tried
    ]
    for row in expired:
        try:
            return await refresh_account(row)
        except Exception:
            continue
    return None


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    client_wants_stream = bool(payload.get("stream"))
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    body, raw, upstream_model = build_body(payload)
    model_name = log_model if log_model is not None else payload.get("model", upstream_model)
    request_id = str(body.get("request_id") or uuid.uuid4())

    if client_wants_stream:
        return ("stream", _stream(raw, request_id, upstream_model, api_key_info, model_name))

    tried: set[int] = set()
    last_error = None
    max_attempts = auth_manager.account_attempt_limit(CHANNEL_ID)
    for attempt in range(max_attempts):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        try:
            url, headers, packed = _packed_request(account, raw, upstream_model)
            chunks: list[dict] = []
            async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=120.0) as client:
                async with client.stream("POST", url, headers=headers, content=packed) as response:
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = (
                            "error",
                            (response.status_code, {"error": {"message": text, "type": "server_error"}}),
                        )
                        _log(
                            api_key_info, account, model_name, False, 0, 0, 0,
                            "retry" if response.status_code in RETRYABLE_STATUS and attempt < max_attempts - 1 else "error",
                            response.status_code, text, t0,
                            increment_usage=response.status_code not in RETRYABLE_STATUS or attempt == max_attempts - 1,
                        )
                        if response.status_code not in RETRYABLE_STATUS:
                            return last_error
                        continue
                    auth_manager.mark_account_success(account["id"])
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        env_err = envelope_error(data)
                        if env_err:
                            last_error = (
                                "error",
                                (400, {"error": {"message": env_err, "type": "invalid_request_error"}}),
                            )
                            chunks = []
                            break
                        for inner in unwrap_sse_payload(data):
                            try:
                                chunks.append(json.loads(inner))
                            except json.JSONDecodeError:
                                continue
            if last_error and last_error[0] == "error" and last_error[1][0] == 400 and not chunks:
                _log(api_key_info, account, model_name, False, 0, 0, 0, "error", 400, str(last_error[1][1])[:400], t0)
                return last_error
            aggregated = _aggregate(chunks, model_name)
            usage = aggregated.get("usage") or {}
            _log(
                api_key_info, account, model_name, False,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                aggregated["choices"][0].get("finish_reason") or "stop",
                200, "", t0, usage=usage,
            )
            return ("json", aggregated)
        except (httpx.HTTPError, EncodeError) as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            continue
    return last_error or (
        "error",
        (503, {
            "error": {
                "message": "No available accounts",
                "type": "channel_unavailable",
                "code": "channel_unavailable",
                "channel": CHANNEL_ID,
            }
        }),
    )


async def _stream(raw: str, request_id: str, upstream_model: str, api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b'data: {"error":{"message":"No available accounts"}}\n\n'
    last_status = 503
    max_attempts = auth_manager.account_attempt_limit(CHANNEL_ID)
    for attempt in range(max_attempts):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        output_started = False
        usage: dict = {}
        finish = "stop"
        try:
            url, headers, packed = _packed_request(account, raw, upstream_model)
            async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=None) as client:
                async with client.stream("POST", url, headers=headers, content=packed) as response:
                    last_status = response.status_code
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = f"data: {json.dumps({'error': {'message': text}}, ensure_ascii=False)}\n\n".encode()
                        if response.status_code not in RETRYABLE_STATUS:
                            yield last_error
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", response.status_code, text, t0)
                            return
                        continue
                    auth_manager.mark_account_success(account["id"])
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        env_err = envelope_error(data)
                        if env_err:
                            last_error = f"data: {json.dumps({'error': {'message': env_err}}, ensure_ascii=False)}\n\n".encode()
                            if not output_started:
                                yield last_error
                            _log(api_key_info, account, model_name, True, 0, 0, 0, "error", 400, env_err, t0)
                            return
                        outer_usage, outer_finish = usage_from_payload(data)
                        if outer_usage:
                            usage.update(outer_usage)
                        if outer_finish:
                            finish = outer_finish
                        for inner in unwrap_sse_payload(data):
                            inner_usage, inner_finish = usage_from_payload(inner)
                            if inner_usage:
                                usage.update(inner_usage)
                            if inner_finish:
                                finish = inner_finish
                            output_started = True
                            yield f"data: {inner}\n\n".encode("utf-8")
            prompt_t = int(usage.get("prompt_tokens") or 0)
            completion_t = int(usage.get("completion_tokens") or 0)
            total_t = int(usage.get("total_tokens") or (prompt_t + completion_t))
            _log(api_key_info, account, model_name, True, prompt_t, completion_t, total_t, finish, 200, "", t0, usage=usage)
            if output_started:
                yield b"data: [DONE]\n\n"
            return
        except (httpx.HTTPError, EncodeError) as exc:
            if output_started:
                _log(api_key_info, account, model_name, True, *token_counts(usage),
                     "error", 502, str(exc).strip() or type(exc).__name__, t0, usage=usage)
                return
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = f"data: {json.dumps({'error': {'message': str(exc)[:240]}}, ensure_ascii=False)}\n\n".encode()
            last_status = 503
            continue
    yield last_error
    _log(api_key_info, None, model_name, True, 0, 0, 0, "error", last_status, "stream failed", time.time())


async def test_chat(account: dict, model: str = "qwork-advanced", prompt: str = "ping") -> dict:
    payload = {
        "model": model or "qwork-advanced",
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "stream": False,
        "max_tokens": 64,
    }
    t0 = time.time()
    body, raw, upstream_model = build_body(payload)
    try:
        url, headers, packed = _packed_request(account, raw, upstream_model)
        chunks: list[dict] = []
        async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=45.0) as client:
            async with client.stream("POST", url, headers=headers, content=packed) as response:
                status = response.status_code
                if status >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                    return {
                        "ok": False,
                        "status_code": status,
                        "duration_ms": int((time.time() - t0) * 1000),
                        "message": text,
                    }
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    env_err = envelope_error(data)
                    if env_err:
                        return {
                            "ok": False,
                            "status_code": 400,
                            "duration_ms": int((time.time() - t0) * 1000),
                            "message": env_err,
                        }
                    for inner in unwrap_sse_payload(data):
                        try:
                            chunks.append(json.loads(inner))
                        except json.JSONDecodeError:
                            continue
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": 0, "duration_ms": int((time.time() - t0) * 1000), "message": str(exc)[:240]}
    aggregated = _aggregate(chunks, upstream_model)
    message_obj = ((aggregated.get("choices") or [{}])[0].get("message") or {})
    message = message_obj.get("content") or message_obj.get("reasoning_content") or ""
    return {
        "ok": True,
        "status_code": 200,
        "duration_ms": int((time.time() - t0) * 1000),
        "model": aggregated.get("model"),
        "message": str(message)[:240],
        "usage": aggregated.get("usage") or {},
    }
