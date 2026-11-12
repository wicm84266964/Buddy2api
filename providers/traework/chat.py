"""TraeWork chat. Completions go through llm_utils_chat so tools return here."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

import httpx

from tls_context import TLS_CONTEXT

import rate_limiter

import auth_manager
import database as db
from providers.usage import credit_amount
from streaming import ClosingStream, close_stream
from providers.traework.constants import (
    AGENT_API,
    AGENT_ID,
    CHANNEL_ID,
    SESSION_MODE,
    SESSIONS_PATH,
    STATIC_MODELS,
)
from providers.traework.llm_chat import TraeWorkChatError, _chunk as _llm_chunk, iter_llm_events
from providers.traework.token import TraeWorkAuthError, auth_headers, is_token_expired, refresh_account


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


def _part_text(part) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    for key in ("text", "content", "output"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            nested = "\n".join(_part_text(item) for item in value).strip()
            if nested:
                return nested
    return ""


def _message_text(message: dict) -> str:
    parts = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        parts.extend(_part_text(part) for part in content)
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "tool")
            arguments = function.get("arguments") or ""
            parts.append(f"[调用工具 {name}] {arguments}".strip())
    return "\n".join(part.strip() for part in parts if str(part).strip()).strip()


def _role_label(message: dict) -> str:
    role = str(message.get("role") or "user")
    if role != "tool":
        return role
    name = str(message.get("name") or message.get("tool_call_id") or "tool")
    return f"tool:{name}"


def conversation_prompt(payload: dict) -> str:
    """Turn the OpenAI message list into the one query TraeWork accepts.

    Upstream stores history inside a chat session. This gateway opens a new
    session per request, so the prior turns have to travel inside that query.
    A lone user message stays unchanged.
    """
    rendered = []
    for item in payload.get("messages") or []:
        if not isinstance(item, dict):
            continue
        text = _message_text(item)
        if not text:
            continue
        rendered.append((_role_label(item), text))
    if not any(role == "user" for role, _text in rendered):
        return ""
    if len(rendered) == 1 and rendered[0][0] == "user":
        return rendered[0][1]
    transcript = "\n\n".join(f"[{role}]\n{text}" for role, text in rendered)
    return (
        "下面是到目前为止的完整对话。请基于这些内容继续，只处理最新的用户请求。"
        "如果最后一条是工具结果，请根据结果继续完成任务，不要把先前对话复述一遍。\n\n"
        + transcript
    )


def _last_user_text(payload: dict) -> str:
    return conversation_prompt(payload)


_SKIP_EVENT_NAMES = {
    "heartbeat",
    "status_changed",
    "platform_timing",
    "timing_events",
    "token_usage",
    "model_config",
    "project_name_message",
    "session_title_message",
    "session_icon_message",
    "metadata",
}
_SKIP_NODE_TYPES = {"status", "heartbeat", "metadata"}
_REASONING_NODE_TYPES = {"thinking", "reasoning", "thought", "chain_of_thought"}
_TEXT_NODE_TYPES = {"text", "markdown", "output_text", "answer"}
_FINISH_TOOL_NAMES = {"finish", "attempt_completion", "complete_task"}
_RECURSE_KEYS = ("messages", "content", "data", "plan_item", "payload")


def _intish(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_usage(raw) -> dict:
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    prompt = _intish(raw.get("prompt_tokens"))
    completion = _intish(raw.get("completion_tokens"))
    total = _intish(raw.get("total_tokens")) or (prompt + completion)
    if not prompt and not completion and not total and credit_amount(raw) is None:
        return {}
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if credit_amount(raw) is not None:
        usage["credit"] = credit_amount(raw)
    reasoning = _intish(raw.get("reasoning_tokens"))
    if reasoning:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return usage


def _dedupe(chunks: list[str]) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in chunks:
        text = (chunk or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return "\n".join(ordered).strip()


def _finish_summary(info: dict) -> str:
    name = str(info.get("name") or "").strip().lower()
    if name not in _FINISH_TOOL_NAMES:
        return ""
    params = info.get("params") if isinstance(info.get("params"), dict) else {}
    summary = params.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    result = info.get("result") if isinstance(info.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    nested = data.get("summary")
    if isinstance(nested, str) and nested.strip():
        return nested.strip()
    return ""


def _collect(value, content: list[str], reasoning: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                _collect(json.loads(text), content, reasoning)
            except json.JSONDecodeError:
                pass
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, content, reasoning)
        return
    if not isinstance(value, dict):
        return

    kind = str(value.get("type") or "")
    if kind in _SKIP_NODE_TYPES:
        return

    info = value.get("tool_call_info")
    if isinstance(info, dict):
        summary = _finish_summary(info)
        if summary:
            content.append(summary)

    if kind in {"tool", "tool_call"}:
        summary = _finish_summary(value)
        if summary:
            content.append(summary)
        return

    thought = value.get("reasoning_content")
    if isinstance(thought, str) and thought.strip():
        reasoning.append(thought.strip())
    thought = value.get("thought")
    if kind in _REASONING_NODE_TYPES and isinstance(thought, str) and thought.strip():
        reasoning.append(thought.strip())

    if kind in _REASONING_NODE_TYPES:
        for key in ("text_content", "text", "markdown", "plain_text"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                reasoning.append(item.strip())
        return

    if kind in _TEXT_NODE_TYPES or kind in {"", "plan_item"}:
        for key in ("text_content", "text", "markdown", "plain_text"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                content.append(item.strip())

    for key in _RECURSE_KEYS:
        child = value.get(key)
        if child is None or child is value:
            continue
        if key == "content" and isinstance(child, str) and child.strip() and not child.strip().startswith(("{", "[")):
            if child.strip() not in content:
                content.append(child.strip())
            continue
        _collect(child, content, reasoning)


def _text_from_event(event: str, payload: dict) -> str:
    if event in _SKIP_EVENT_NAMES:
        return ""
    content: list[str] = []
    reasoning: list[str] = []
    _collect(payload, content, reasoning)
    return _dedupe(content)


def extract_assistant_turn(items: list) -> dict:
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("role") not in {"assistant", "system"} and item.get("message_type") != "task":
            continue
        _collect(item.get("content"), content, reasoning)
        parsed = _parse_usage(item.get("token_usage"))
        if parsed:
            usage = parsed
    return {
        "text": _dedupe(content),
        "reasoning": _dedupe(reasoning),
        "usage": usage,
    }


def extract_assistant_text(items: list) -> str:
    return extract_assistant_turn(items)["text"]


def _openai_json(
    model: str,
    text: str,
    finish: str = "stop",
    reasoning: str = "",
    usage: dict | None = None,
) -> dict:
    message = {"role": "assistant", "content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": f"traework-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _pick(tried: set[int]) -> dict | None:
    account = auth_manager.pick_account(tried, provider=CHANNEL_ID)
    if account:
        if is_token_expired(account):
            try:
                return await refresh_account(account)
            except TraeWorkAuthError:
                pass
        else:
            return account
    expired = [
        row
        for row in db.list_accounts(provider=CHANNEL_ID)
        if row.get("status") == "expired" and row.get("id") not in tried
    ]
    for row in expired:
        try:
            return await refresh_account(row)
        except TraeWorkAuthError:
            continue
    return None


def _log(api_key_info, account, model_name, stream, finish, status, error, t0, usage: dict | None = None):
    usage = usage or {}
    try:
        db.record_request(
            {
                "api_key_id": api_key_info["id"] if api_key_info else None,
                "api_key_name": api_key_info["name"] if api_key_info else None,
                "account_id": account["id"] if account else None,
                "account_name": account.get("name") if account else None,
                "provider": CHANNEL_ID,
                # The alias the client sent, not the translated id: every other
                # provider reads `_log_model` at the call site, this one logs here.
                "model": (api_key_info.get("_log_model")
                          if isinstance(api_key_info, dict) else None) or model_name,
                "resolved_model": (api_key_info.get("_resolved_model")
                                   if isinstance(api_key_info, dict) else None) or model_name,
                "stream": 1 if stream else 0,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "credit": credit_amount(usage),
                "finish_reason": finish,
                "duration_ms": int((time.time() - t0) * 1000),
                "status_code": status,
                "error_msg": error,
                "increment_usage": True,
            }
        )
    except Exception:
        pass


async def _turn(account: dict, prompt: str, model: str, timeout: float = 90.0) -> dict:
    headers = auth_headers(account)
    session_url = f"{AGENT_API}{SESSIONS_PATH}"
    sid = ""
    async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT, timeout=timeout) as client:
        created = await client.post(
            session_url,
            headers=headers,
            json={"mode": SESSION_MODE, "auto_create_project": True, "origin": "web"},
        )
        if created.status_code >= 400:
            raise TraeWorkAuthError(f"create session HTTP {created.status_code}")
        data = created.json() if created.content else {}
        if data.get("code") not in (None, 0):
            raise TraeWorkAuthError(str(data.get("message") or data.get("code")))
        sid = str((data.get("data") or {}).get("chat_session_id") or "")
        if not sid:
            raise TraeWorkAuthError("create session missing chat_session_id")
        pieces: list[str] = []
        finished = asyncio.Event()

        async def read_events() -> None:
            event_name = "message"
            try:
                async with client.stream(
                    "GET",
                    f"{session_url}/{sid}/events",
                    headers={**headers, "Accept": "text/event-stream"},
                ) as response:
                    if response.status_code >= 400:
                        finished.set()
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event_name = line[6:].strip() or "message"
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        try:
                            event_payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        text = _text_from_event(event_name, event_payload if isinstance(event_payload, dict) else {})
                        if text:
                            pieces.append(text)
                        if event_name == "done":
                            finished.set()
                            return
            except httpx.HTTPError:
                finished.set()

        task = asyncio.create_task(read_events())
        try:
            await asyncio.sleep(0.35)
            query = json.dumps(
                [{"type": "text", "data": {"content": prompt}}],
                ensure_ascii=False,
            )
            sent = await client.post(
                f"{session_url}/{sid}/messages",
                headers=headers,
                json={
                    "chat_session_id": sid,
                    "content": [],
                    "query": query,
                    "model_name": model,
                    "agent_id": AGENT_ID,
                    "agent_type": AGENT_ID,
                },
            )
            payload = sent.json() if sent.content else {}
            if sent.status_code >= 400 or payload.get("code") not in (None, 0):
                raise TraeWorkAuthError(str(payload.get("message") or f"HTTP {sent.status_code}"))
            try:
                await asyncio.wait_for(finished.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            messages = await client.get(f"{session_url}/{sid}/messages", headers=headers)
            body = messages.json() if messages.content else {}
            items = ((body.get("data") or {}).get("items") or [])
            turn = extract_assistant_turn(items)
            text = turn["text"] or "\n".join(dict.fromkeys(pieces)).strip()
            if not text:
                raise TraeWorkAuthError("TraeWork turn finished without assistant text")
            return {
                "text": text,
                "reasoning": turn["reasoning"],
                "usage": turn["usage"],
            }
        finally:
            task.cancel()
            try:
                await client.delete(f"{session_url}/{sid}", headers=headers)
            except Exception:
                pass


async def _collect_llm(account: dict, payload: dict, model: str) -> dict:
    text = []
    reasoning = []
    calls: dict[int, dict] = {}
    usage = None
    finish = "stop"
    async for event in iter_llm_events(account, payload, model):
        if event["kind"] == "usage":
            usage = event["usage"]
        elif event["kind"] == "output":
            if event["text"]:
                text.append(event["text"])
            if event["reasoning"]:
                reasoning.append(event["reasoning"])
            for call in event["tool_calls"]:
                index = int(call.get("index") or 0)
                slot = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                if function.get("name"):
                    slot["function"]["name"] = function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += str(function["arguments"])
        elif event["kind"] == "done" and event.get("finish_reason"):
            finish = event["finish_reason"]
    return {
        "text": "".join(text),
        "reasoning": "".join(reasoning),
        "tool_calls": [calls[index] for index in sorted(calls)],
        "usage": usage,
        "finish": "tool_calls" if calls else finish,
    }


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    requested = str(payload.get("model") or "auto")
    model = translate_model(requested)
    stream = bool(payload.get("stream"))
    tried: set[int] = set()
    last_error = None
    refreshed: set[int] = set()
    max_attempts = auth_manager.account_attempt_limit(CHANNEL_ID, refresh_retry=True)
    for _ in range(max_attempts):
        account = await _pick(tried)
        if not account:
            break
        account_id = int(account["id"])
        tried.add(account_id)
        t0 = time.time()
        try:
            shown = requested
            if stream:
                events = iter_llm_events(account, payload, model)
                try:
                    first = await events.__anext__()
                except StopAsyncIteration:
                    first = {"kind": "done", "finish_reason": "stop"}

                async def _forward(current=account, opened=events, head=first, name=shown, started=t0):
                    saw_tools = False
                    finish = "stop"
                    usage = None
                    try:
                        pending = [head]
                        while pending or True:
                            event = pending.pop(0) if pending else await opened.__anext__()
                            if event["kind"] == "usage":
                                usage = event["usage"]
                            elif event["kind"] == "output":
                                calls = event["tool_calls"]
                                if calls:
                                    saw_tools = True
                                if event["text"] or event["reasoning"] or calls:
                                    yield _llm_chunk(name, text=event["text"], reasoning=event["reasoning"], tool_calls=calls or None)
                            elif event["kind"] == "done":
                                finish = "tool_calls" if saw_tools else (event.get("finish_reason") or "stop")
                                break
                    except StopAsyncIteration:
                        pass
                    except TraeWorkChatError as exc:
                        yield _llm_chunk(name, text=str(exc)[:240])
                        finish = "stop"
                    await close_stream(opened)
                    yield _llm_chunk(name, finish=finish, usage=usage)
                    auth_manager.mark_account_success(int(current["id"]))
                    _log(api_key_info, current, name, True, finish, 200, "", started, usage)
                    yield b"data: [DONE]\n\n"

                return ("stream", ClosingStream(_forward(), upstream=events))
            turn = await _collect_llm(account, payload, model)
            auth_manager.mark_account_success(account_id)
            _log(api_key_info, account, shown, False, turn["finish"], 200, "", t0, turn.get("usage"))
            message = {"role": "assistant", "content": turn["text"]}
            if turn["reasoning"]:
                message["reasoning_content"] = turn["reasoning"]
            if turn["tool_calls"]:
                message["tool_calls"] = turn["tool_calls"]
            return (
                "json",
                {
                    "id": f"chatcmpl-traework-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": shown,
                    "choices": [{"index": 0, "message": message, "finish_reason": turn["finish"]}],
                    "usage": turn.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                },
            )
        except TraeWorkChatError as exc:
            if exc.retry_auth and account_id not in refreshed:
                refreshed.add(account_id)
                tried.discard(account_id)
                try:
                    await refresh_account(account)
                except TraeWorkAuthError:
                    pass
            auth_manager.mark_account_failure(account_id, exc.status_code)
            last_error = ("error", (exc.status_code, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            _log(api_key_info, account, requested, stream, "error", exc.status_code, str(exc)[:240], t0)
            continue
        except (TraeWorkAuthError, httpx.HTTPError) as exc:
            auth_manager.mark_account_failure(account_id, 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            _log(api_key_info, account, requested, stream, "error", 503, str(exc)[:240], t0)
            continue
    return last_error or (
        "error",
        (
            503,
            {
                "error": {
                    "message": "No available accounts",
                    "type": "channel_unavailable",
                    "code": "channel_unavailable",
                }
            },
        ),
    )


async def _stream_once(
    text: str,
    model: str,
    reasoning: str = "",
    usage: dict | None = None,
) -> AsyncGenerator[bytes, None]:
    cid = f"traework-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    delta: dict = {"role": "assistant"}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if text:
        delta["content"] = text
    chunk = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
    done = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


async def test_chat(account: dict, model: str = "qwen-3.7-plus", prompt: str = "请回复：pong") -> dict:
    t0 = time.time()
    try:
        turn = await _collect_llm(
            account,
            {"model": model, "messages": [{"role": "user", "content": prompt or "请回复：pong"}]},
            translate_model(model or "auto"),
        )
        text = turn["text"]
    except (TraeWorkAuthError, TraeWorkChatError) as exc:
        return {
            "ok": False,
            "status_code": 503,
            "duration_ms": int((time.time() - t0) * 1000),
            "message": str(exc)[:400],
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "duration_ms": int((time.time() - t0) * 1000),
            "message": str(exc)[:400],
        }
    chosen = translate_model(model or "auto")
    return {
        "ok": True,
        "status_code": 200,
        "duration_ms": int((time.time() - t0) * 1000),
        "model": chosen,
        "message": text[:400],
    }
