"""QwenWork chat client. Isolated COSY/SSE stack; never uses WorkBuddy fingerprint."""

from __future__ import annotations

import json
import time
import uuid
from typing import AsyncGenerator

import httpx

import auth_manager
import database as db
from reasoning_controls import resolve_reasoning_control
from providers.qwenwork import cosy
from providers.qwenwork.constants import (
    ALIASES,
    BUILD,
    BUSINESS_PRODUCT,
    BUSINESS_TYPE,
    CHANNEL_ID,
    CHAT_PATH,
    CHAT_QUERY,
    CLIENT_TYPE,
    COSY_VERSION,
    GATEWAY,
    IDE_VERSION,
    LOGIN_VERSION,
    MACHINE_OS,
    RELEASE_VERSION,
    RETRYABLE_STATUS,
    SCENE,
    USER_AGENT,
)
from providers.qwenwork.token import is_token_expired, refresh_account


def translate_model(model: str) -> str:
    inner = (model or "qwork-advanced").strip() or "qwork-advanced"
    return ALIASES.get(inner, inner)


def chat_url() -> str:
    return f"{GATEWAY}{CHAT_PATH}?{CHAT_QUERY}"


def _ids(account: dict) -> tuple[str, str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    uid = str(account.get("uid") or extra.get("uid") or "")
    name = str(account.get("nickname") or account.get("name") or extra.get("name") or "")
    email = str(extra.get("email") or "")
    token = str(account.get("access_token") or "")
    return uid, name, email, token


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
            "is_vl": model == "qwork-advanced",
            "is_reasoning": is_reasoning,
            "api_key": "",
            "url": "",
            "source": "system",
            "max_input_tokens": 180000,
        },
        "system": system,
        "messages": messages,
        "tools": payload.get("tools") or [],
        "parameters": parameters,
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
        "Login-Version": LOGIN_VERSION,
        "x-model-key": model,
        "x-model-source": "system",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Accept-Encoding": "identity",
    }
    if machine_id:
        headers["Cosy-MachineId"] = machine_id
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
        usage = item.get("usage") or usage
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
    url = chat_url()
    request_id = str(body.get("request_id") or uuid.uuid4())

    if client_wants_stream:
        return ("stream", _stream(raw, url, request_id, upstream_model, api_key_info, model_name))

    tried: set[int] = set()
    last_error = None
    for attempt in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        try:
            headers = _headers_for(account, url, raw, upstream_model, request_id)
            chunks: list[dict] = []
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", url, headers=headers, content=raw) as response:
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", errors="replace")[:400]
                        auth_manager.mark_account_failure(account["id"], response.status_code)
                        last_error = (
                            "error",
                            (response.status_code, {"error": {"message": text, "type": "server_error"}}),
                        )
                        _log(
                            api_key_info, account, model_name, False, 0, 0, 0,
                            "retry" if response.status_code in RETRYABLE_STATUS and attempt < 2 else "error",
                            response.status_code, text, t0,
                            increment_usage=response.status_code not in RETRYABLE_STATUS or attempt == 2,
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
                200, "", t0,
            )
            return ("json", aggregated)
        except httpx.HTTPError as exc:
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


async def _stream(raw: str, url: str, request_id: str, upstream_model: str, api_key_info, model_name: str) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last_error = b'data: {"error":{"message":"No available accounts"}}\n\n'
    last_status = 503
    for attempt in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(account["id"])
        t0 = time.time()
        output_started = False
        try:
            headers = _headers_for(account, url, raw, upstream_model, request_id)
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, headers=headers, content=raw) as response:
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
                        for inner in unwrap_sse_payload(data):
                            output_started = True
                            yield f"data: {inner}\n\n".encode("utf-8")
            if output_started:
                yield b"data: [DONE]\n\n"
            _log(api_key_info, account, model_name, True, 0, 0, 0, "stop", 200, "", t0)
            return
        except httpx.HTTPError as exc:
            if output_started:
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
    url = chat_url()
    try:
        headers = _headers_for(account, url, raw, upstream_model, str(body["request_id"]))
        chunks: list[dict] = []
        async with httpx.AsyncClient(timeout=45.0) as client:
            async with client.stream("POST", url, headers=headers, content=raw) as response:
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
