"""Qoder chat. China and international stay on their own gateways."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

import httpx

import rate_limiter

import auth_manager
import database as db
import resource_sync
from providers.qoder import cosy
from providers.qoder import quota as qoder_quota
from providers.qoder.constants import (
    CHAT_PATH,
    CHAT_QUERY,
    MAX_OUTPUT_TOKENS,
    inference_base,
    region,
    upstream_key,
)
from providers.qoder.encode import encode_body
from providers.qoder.token import is_token_expired, refresh_account


class QoderChatError(RuntimeError):
    pass


def translate_model(channel: str, model: str) -> str:
    import aliases

    inner = (model or "auto").strip() or "auto"
    return aliases.resolve(channel, inner)


def accepts_model(channel: str, inner: str) -> bool:
    import aliases
    import catalog
    from providers.qoder.constants import model_rows

    value = (inner or "").strip()
    if value in aliases.merged_map(channel):
        return True
    ids = {item_id for item_id, *_rest in model_rows(channel)}
    live = catalog.models_for(channel, [{"id": item_id} for item_id in ids])
    return value in {str(item.get("id")) for item in live if isinstance(item, dict)}


def _ids(account: dict) -> tuple[str, str, str, str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    return (
        str(account.get("uid") or extra.get("uid") or ""),
        str(account.get("nickname") or account.get("name") or extra.get("name") or ""),
        str(extra.get("email") or ""),
        str(account.get("access_token") or ""),
        str(extra.get("machine_id") or extra.get("login_device_id") or ""),
    )


def _text_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    if value is None:
        return ""
    return str(value)


def _raw_calls(message: dict) -> list[dict]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        calls = message.get("toolCalls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _normalize_call(call: dict, index: int) -> dict:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or call.get("name") or "")
    arguments = function.get("arguments", call.get("arguments", call.get("input", {})))
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False, separators=(",", ":"))
    item = {
        "id": str(call.get("id") or f"call_{index}"),
        "type": "function",
        "function": {"name": name, "arguments": arguments or "{}"},
    }
    return item


def _call_ids(message: dict) -> list[str]:
    return [str(call.get("id")) for call in _raw_calls(message) if call.get("id")]


def _tool_id(message: dict) -> str:
    return str(message.get("tool_call_id") or message.get("toolCallId") or "")


def _as_user_tool_note(message: dict) -> dict:
    name = str(message.get("name") or _tool_id(message) or "tool")
    text = _text_content(message.get("content"))
    return {"role": "user", "content": f"[工具结果 {name}]\n{text}".strip()}


def prepare_messages(messages: list) -> list:
    """Make tool results acceptable to Qoder's stricter chat models.

    DeepSeek rejects a tool message unless it answers the assistant message
    directly before it. Orphan results stay in the transcript as user text.
    """
    source = [item for item in messages if isinstance(item, dict)]
    consumed: set[int] = set()
    ordered: list[dict] = []
    for index, message in enumerate(source):
        if index in consumed:
            continue
        role = message.get("role")
        if role != "assistant" or not _call_ids(message):
            if role == "tool":
                ordered.append(_as_user_tool_note(message))
            else:
                ordered.append(message)
            continue
        wanted = set(_call_ids(message))
        matches = []
        between = []
        for later in range(index + 1, len(source)):
            nxt = source[later]
            if nxt.get("role") == "assistant" and _call_ids(nxt):
                break
            if nxt.get("role") == "tool" and _tool_id(nxt) in wanted:
                matches.append(nxt)
                consumed.add(later)
                continue
            if nxt.get("role") == "tool":
                between.append(_as_user_tool_note(nxt))
                consumed.add(later)
                continue
            between.append(nxt)
            consumed.add(later)
        assistant = dict(message)
        calls = [_normalize_call(call, index) for index, call in enumerate(_raw_calls(assistant))]
        matched_ids = {_tool_id(item) for item in matches}
        surviving = [call for call in calls if call["id"] in matched_ids]
        if surviving:
            text = _text_content(assistant.get("content")).strip()
            assistant["content"] = text or " "
            assistant["tool_calls"] = surviving
            assistant.pop("toolCalls", None)
            ordered.append(assistant)
            for item in matches:
                ordered.append({
                    "role": "tool",
                    "tool_call_id": _tool_id(item),
                    "content": _text_content(item.get("content")),
                })
        else:
            assistant.pop("tool_calls", None)
            assistant.pop("toolCalls", None)
            ordered.append(assistant)
            ordered.extend(_as_user_tool_note(item) for item in matches)
        ordered.extend(between)
    return ordered


def build_body(channel: str, payload: dict) -> tuple[dict, str, str]:
    requested = str(payload.get("model") or "auto")
    inner = requested.split("/", 1)[-1] if requested.startswith(f"{channel}/") else requested
    model = translate_model(channel, inner)
    key = upstream_key(channel, model)
    messages = prepare_messages(payload.get("messages") if isinstance(payload.get("messages"), list) else [])
    limit = payload.get("max_tokens") or payload.get("max_completion_tokens") or MAX_OUTPUT_TOKENS
    try:
        limit = min(int(limit), MAX_OUTPUT_TOKENS)
    except (TypeError, ValueError):
        limit = MAX_OUTPUT_TOKENS
    request_id = uuid.uuid4().hex
    last_user = ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            last_user = content if isinstance(content, str) else ""
            break
    body = {
        "request_id": request_id,
        "request_set_id": request_id,
        "chat_record_id": request_id,
        "session_id": str(payload.get("session_id") or payload.get("user") or request_id),
        "stream": True,
        "chat_task": "FREE_INPUT",
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "session_type": "qodercli",
        "agent_id": "agent_common",
        "task_id": "common",
        "system": "",
        "messages": messages,
        "tools": payload.get("tools") or [],
        "parameters": {"max_tokens": limit},
        "chat_context": {
            "text": last_user,
            "features": [],
            "chatPrompt": "",
            "extra": {
                "context": [],
                "modelConfig": {"key": key, "is_reasoning": False},
                "originalContent": last_user,
            },
        },
        "model_config": {"key": key, "source": "system", "format": "openai", "is_reasoning": False},
        "business": {
            "product": "cli",
            "version": "1.0.0",
            "type": "agent",
            "stage": "start",
            "id": request_id,
            "name": last_user[:30],
            "begin_at": int(time.time() * 1000),
        },
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return body, raw, model


def _openai_chunk(model: str, text: str = "", finish: str | None = None, tool_calls: list | None = None) -> bytes:
    delta = {}
    if text:
        delta["content"] = text
    if tool_calls:
        delta["tool_calls"] = tool_calls
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    payload = {"id": "chatcmpl-qoder", "object": "chat.completion.chunk", "model": model, "choices": [choice]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _tool_call_delta(call: dict, index: int) -> dict:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    arguments = function.get("arguments", call.get("arguments", ""))
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    item = {
        "index": int(call.get("index") if call.get("index") is not None else index),
        "type": call.get("type") or "function",
        "function": {
            "name": str(function.get("name") or call.get("name") or ""),
            "arguments": arguments,
        },
    }
    if call.get("id"):
        item["id"] = str(call["id"])
    return item


def _delta_text(payload: dict, *, include_reasoning: bool = True) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
        content = str(delta.get("content") or "")
        if content or not include_reasoning:
            return content
        return str(delta.get("reasoning_content") or "")
    for key in ("content", "text", "output"):
        if isinstance(payload.get(key), str) and payload[key]:
            return payload[key]
    return ""


def _choice_of(payload: dict) -> dict:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def _tool_calls_of(payload: dict) -> list[dict]:
    choice = _choice_of(payload)
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    calls = delta.get("tool_calls") or message.get("tool_calls") or payload.get("tool_calls") or []
    if not isinstance(calls, list):
        return []
    return [_tool_call_delta(call, index) for index, call in enumerate(calls) if isinstance(call, dict)]


def _parts_from_event(raw: str, *, include_reasoning: bool = True) -> tuple[list[str], list[dict]]:
    text = raw.strip()
    if not text or text == "[DONE]":
        return [], []
    if text.startswith("data:"):
        text = text[5:].strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return [], []
    if not isinstance(payload, dict):
        return [], []
    status = payload.get("statusCodeValue")
    if isinstance(status, int) and status != 200:
        body = payload.get("body")
        message = body if isinstance(body, str) else json.dumps(payload, ensure_ascii=False)
        raise QoderChatError(message[:240])
    inner = payload.get("body")
    if isinstance(inner, str):
        if inner.strip() == "[DONE]":
            return [], []
        return _parts_from_event(inner, include_reasoning=include_reasoning)
    target = inner if isinstance(inner, dict) else payload
    piece = _delta_text(target, include_reasoning=include_reasoning)
    return ([piece] if piece else []), _tool_calls_of(target)


def _texts_from_event(raw: str, *, include_reasoning: bool = True) -> list[str]:
    texts, _calls = _parts_from_event(raw, include_reasoning=include_reasoning)
    return texts


def _log(channel, api_key_info, account, model_name, finish_reason, status_code, error_msg, started, text_len):
    info = api_key_info if isinstance(api_key_info, dict) else {}
    try:
        db.record_request(
            {
                "api_key_id": info.get("id"),
                "api_key_name": info.get("name"),
                "account_id": account.get("id") if account else None,
                "account_name": (account.get("name") or account.get("nickname")) if account else None,
                "provider": channel,
                "model": model_name,
                "resolved_model": (api_key_info.get("_resolved_model")
                                   if isinstance(api_key_info, dict) else None) or model_name,
                "stream": 1,
                "prompt_tokens": 0,
                "completion_tokens": max(1, text_len // 4) if text_len else 0,
                "total_tokens": max(1, text_len // 4) if text_len else 0,
                "credit": 0,
                "finish_reason": finish_reason,
                "duration_ms": int((time.time() - started) * 1000),
                "status_code": status_code,
                "error_msg": error_msg,
            }
        )
    except Exception:
        pass


def _signed_request(account: dict, channel: str, raw: str) -> tuple[str, dict, bytes]:
    token = str(account.get("access_token") or "")
    url = f"{inference_base(channel, token).rstrip('/')}{CHAT_PATH}?{CHAT_QUERY}"
    encoded = encode_body(raw.encode("utf-8"))
    uid, name, email, access, machine = _ids(account)
    headers = cosy.auth_headers(
        url=url,
        body=encoded,
        uid=uid,
        name=name,
        email=email,
        access_token=access,
        machine_id=machine,
    )
    return url, headers, encoded


# A request answered by an account whose credits are gone comes back as HTTP 400
# with a business payload: the observed shape is ``{"code":"112","message":
# "{\"pricingUrl\":\"...\"}"}`` — a code plus a link to the pricing page. Both
# markers are accepted because the code is the brittle half (it is upstream's to
# change) while the pricing link is hard to produce for any other reason.
QUOTA_REFUSAL_CODES = frozenset({"112"})
QUOTA_REFUSAL_MARKER = "pricingUrl"


def is_quota_refusal(body: str) -> bool:
    """True when an upstream error says "this account cannot pay any more"."""
    text = (body or "").strip()
    if not text:
        return False
    if QUOTA_REFUSAL_MARKER in text:
        return True
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("code") or "") in QUOTA_REFUSAL_CODES


# How long the live quota read triggered by an upstream refusal may take before
# we fall back to the zero that refusal already implies. The refusal *is* the
# answer — this read only sharpens the number — so it must never hold the error
# response open for long.
QUOTA_REFUSAL_FETCH_TIMEOUT_SECONDS = 5.0


async def _live_remaining(account: dict) -> float | None:
    """Best-effort live quota read, as a number routing can cache.

    Return only a measured total credit balance. A timeout or billing failure
    is unknown, not a measured zero; normal account cooldown handles refusals.
    """
    try:
        snapshot = await asyncio.wait_for(
            qoder_quota.fetch_quota(account), timeout=QUOTA_REFUSAL_FETCH_TIMEOUT_SECONDS
        )
    except Exception:
        return None
    if not getattr(snapshot, "ok", False) or getattr(snapshot, "unsupported", False):
        return None
    if getattr(snapshot, "unit", None) != "credit":
        return None
    value = getattr(snapshot, "remaining", None)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


async def mark_quota_exhausted(channel: str, account: dict) -> None:
    """After a refusal, cache only a successfully measured total balance.

    A billing outage preserves the previous observation. Refusal cooldown and
    queue failover handle the current failure without inventing a zero balance.
    """
    remaining = await _live_remaining(account)
    if remaining is None:
        return
    try:
        resource_sync.store_quota_reading(
            account,
            remaining=remaining,
            source=f"{channel}-upstream-quota",
        )
    except Exception:
        pass


async def _stream(channel: str, raw: str, model: str, api_key_info, model_name) -> AsyncGenerator[bytes, None]:
    tried: set[int] = set()
    last = "没有可用的 Qoder 账号"
    shown = model_name or model
    max_attempts = auth_manager.account_attempt_limit(channel)
    for _attempt in range(max_attempts):
        account = await _pick(channel, tried)
        if not account:
            break
        tried.add(int(account["id"]))
        started = time.time()
        refreshed = False
        while True:
            url, headers, encoded = _signed_request(account, channel, raw)
            try:
                async with rate_limiter.account_slot(account), httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
                    async with client.stream("POST", url, headers=headers, content=encoded) as response:
                        if response.status_code in {401, 403} and not refreshed:
                            refreshed = True
                            await response.aread()
                            try:
                                account = await refresh_account(account)
                            except Exception as exc:
                                last = str(exc)[:240]
                                auth_manager.mark_account_failure(account["id"], response.status_code)
                                break
                            continue
                        if response.status_code >= 400:
                            last = (await response.aread()).decode("utf-8", errors="replace")[:240]
                            auth_manager.mark_account_failure(account["id"], response.status_code)
                            if response.status_code == 400 and is_quota_refusal(last):
                                await mark_quota_exhausted(channel, account)
                            _log(channel, api_key_info, account, shown, "error", response.status_code, last, started, 0)
                            break
                        text_len = 0
                        failed = ""
                        saw_tools = False
                        async for line in response.aiter_lines():
                            try:
                                pieces, calls = _parts_from_event(line, include_reasoning=False)
                            except QoderChatError as exc:
                                failed = str(exc)
                                break
                            for piece in pieces:
                                text_len += len(piece)
                                yield _openai_chunk(shown, piece)
                            if calls:
                                saw_tools = True
                                yield _openai_chunk(shown, tool_calls=calls)
                        if failed:
                            last = failed
                            _log(channel, api_key_info, account, shown, "error", 400, failed, started, text_len)
                            break
                        yield _openai_chunk(shown, finish="tool_calls" if saw_tools else "stop")
                        yield b"data: [DONE]\n\n"
                        auth_manager.mark_account_success(account["id"])
                        _log(channel, api_key_info, account, shown, "stop", 200, "", started, text_len)
                        return
            except rate_limiter.RateLimitError:
                raise
            except Exception as exc:
                last = str(exc)[:240]
                break
    err = json.dumps(
        {"error": {"message": last, "type": "channel_unavailable",
                   "code": "channel_unavailable", "channel": channel}},
        ensure_ascii=False,
    )
    yield f"data: {err}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


async def _pick(channel: str, exclude: set[int]) -> dict | None:
    provider = __import__("providers").get_provider(channel)
    if provider is None:
        return None
    account = await provider.pick_account_with_fallback(exclude)
    if account and is_token_expired(account):
        try:
            return await refresh_account(account)
        except Exception:
            return account
    return account


async def test_chat(account: dict, model: str = "auto", prompt: str = "ping") -> dict:
    channel = str(account.get("provider") or "qoder")
    payload = {
        "model": model or "auto",
        "messages": [{"role": "user", "content": prompt or "ping"}],
        "stream": True,
        "max_tokens": 64,
    }
    started = time.time()
    current = account
    if is_token_expired(current) or not str(current.get("access_token") or ""):
        try:
            current = await refresh_account(current)
        except Exception as exc:
            return _test_result(False, 0, started, str(exc)[:240], model or "auto")
    _body, raw, upstream = build_body(channel, payload)
    token = str(current.get("access_token") or "")
    url = f"{inference_base(channel, token).rstrip('/')}{CHAT_PATH}?{CHAT_QUERY}"
    encoded = encode_body(raw.encode("utf-8"))
    uid, name, email, access, machine = _ids(current)
    headers = cosy.auth_headers(
        url=url,
        body=encoded,
        uid=uid,
        name=name,
        email=email,
        access_token=access,
        machine_id=machine,
    )
    pieces: list[str] = []
    try:
        async with rate_limiter.account_slot(account), httpx.AsyncClient(timeout=45.0, trust_env=False) as client:
            async with client.stream("POST", url, headers=headers, content=encoded) as response:
                status = response.status_code
                if status >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")[:240]
                    return _test_result(False, status, started, text or f"HTTP {status}", upstream)
                async for line in response.aiter_lines():
                    try:
                        pieces.extend(_texts_from_event(line, include_reasoning=False))
                    except QoderChatError as exc:
                        return _test_result(False, 400, started, str(exc), upstream)
                    if len("".join(pieces).strip()) >= 24:
                        break
    except Exception as exc:
        return _test_result(False, 0, started, str(exc)[:240], upstream)
    text = "".join(pieces).strip()
    return _test_result(bool(text), 200, started, text or "上游没有返回文本", upstream)


def _test_result(ok: bool, status_code: int, started: float, message: str, model: str) -> dict:
    return {
        "ok": ok,
        "status_code": status_code,
        "duration_ms": int((time.time() - started) * 1000),
        "message": message,
        "model": model,
    }


def _aggregate_stream(chunks: list[bytes | str]) -> tuple[str, str, list[dict]]:
    texts = []
    error = ""
    finish = "stop"
    calls: dict[int, dict] = {}
    for chunk in chunks:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict) and err.get("message"):
                error = str(err["message"])
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            if choices[0].get("finish_reason"):
                finish = str(choices[0]["finish_reason"])
            delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
            content = delta.get("content")
            if isinstance(content, str):
                texts.append(content)
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                index = int(call.get("index") or 0)
                slot = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                if function.get("name"):
                    slot["function"]["name"] = function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += str(function["arguments"])
    return "".join(texts).strip(), error, [calls[index] for index in sorted(calls)]


async def chat_completions(channel: str, payload: dict, api_key_info: dict | None) -> tuple:
    _body, raw, model = build_body(channel, payload)
    log_model = None
    if isinstance(api_key_info, dict):
        log_model = api_key_info.get("_log_model")
    shown = log_model or payload.get("model") or model
    stream = _stream(channel, raw, model, api_key_info, shown)
    if payload.get("stream"):
        return ("stream", stream)
    chunks = [chunk async for chunk in stream]
    text, error, tool_calls = _aggregate_stream(chunks)
    if error and not text and not tool_calls:
        return ("error", (502, {"error": {"message": error, "type": "server_error"}}))
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return (
        "json",
        {
            "id": "chatcmpl-qoder",
            "object": "chat.completion",
            "model": shown,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
        },
    )
