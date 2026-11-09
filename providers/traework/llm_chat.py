"""TraeWork chat through llm_utils_chat.

This is the local-agent path: the model returns tool calls, and the caller
runs them on this machine. The remote chat_sessions workspace is not used.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from tls_context import TLS_CONTEXT_NO_ENV

import rate_limiter

from providers.traework.constants import (
    AGENT_API,
    APP_ID,
    IDE_VERSION,
    IDE_VERSION_CODE,
    LLM_CHAT_PATH,
    LLM_FUNCTION,
)
from providers.traework.token import auth_headers

DEFAULT_MAX_TOKENS = 1_000_000
_SAMPLING = (
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "n",
)


class TraeWorkChatError(Exception):
    def __init__(self, message: str, *, status_code: int = 502, retry_auth: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retry_auth = retry_auth


def _text(value) -> str:
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
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    return str(value)


def _call_name(call: dict) -> str:
    function = call.get("function_call") if isinstance(call.get("function_call"), dict) else {}
    if not function and isinstance(call.get("function"), dict):
        function = call["function"]
    return str(function.get("name") or call.get("name") or "").strip()


def _normalize_outbound_call(call: dict) -> dict | None:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or call.get("name") or "").strip()
    if not name:
        return None
    arguments = function.get("arguments", call.get("arguments", "{}"))
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False, separators=(",", ":"))
    item = dict(call)
    item.pop("function", None)
    item["function_call"] = {"name": name, "arguments": arguments or "{}"}
    if not item.get("id"):
        item.pop("id", None)
    item["type"] = item.get("type") or "function"
    return item


def _normalize_message(message: dict) -> dict | None:
    role = str(message.get("role") or "")
    if role == "developer":
        role = "system"
    if role not in {"system", "user", "assistant", "tool"}:
        return None
    item = {"role": role}
    if role == "assistant":
        calls = []
        raw_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        for call in raw_calls:
            if isinstance(call, dict):
                normalized = _normalize_outbound_call(call)
                if normalized:
                    calls.append(normalized)
        text = _text(message.get("content"))
        if calls:
            item["tool_calls"] = calls
            if text:
                item["content"] = [{"type": "text", "text": text}]
        elif text:
            item["content"] = [{"type": "text", "text": text}]
        else:
            return None
        return item
    if role == "tool":
        tool_id = str(message.get("tool_call_id") or message.get("toolCallId") or "")
        if not tool_id:
            return None
        return {
            "role": "tool",
            "tool_call_id": tool_id,
            "content": [{"type": "text", "text": _text(message.get("content"))}],
        }
    text = _text(message.get("content"))
    if not text and role != "user":
        return None
    item["content"] = [{"type": "text", "text": text}] if text else []
    return item


def _known_call_ids(messages: list[dict]) -> set[str]:
    known = set()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("id"):
                known.add(str(call["id"]))
    return known


def prepare_messages(messages: list) -> list[dict]:
    prepared = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_message(item)
        if normalized:
            prepared.append(normalized)
    known = _known_call_ids(prepared)
    return [
        item
        for item in prepared
        if item.get("role") != "tool" or str(item.get("tool_call_id") or "") in known
    ]


def _tool_choice(value):
    if isinstance(value, str):
        name = value.strip()
        if not name or name.lower() == "none":
            return None, True
        return name, False
    if not isinstance(value, dict):
        return None, False
    kind = str(value.get("type") or "").strip().lower()
    if kind == "none":
        return None, True
    if kind in {"auto", "required"}:
        return kind, False
    if kind == "function":
        function = value.get("function") if isinstance(value.get("function"), dict) else {}
        name = str(function.get("name") or value.get("name") or "").strip()
        return name or "auto", False
    return None, False


def _tools(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    kept = []
    for tool in value:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if not function or not str(function.get("name") or "").strip():
            continue
        copied = dict(function)
        parameters = copied.get("parameters")
        if isinstance(parameters, (dict, list)):
            copied["parameters"] = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
        elif parameters is None:
            copied["parameters"] = "{}"
        kept.append({"type": "function", "function": copied})
    return kept


def build_llm_body(payload: dict, model: str) -> dict:
    messages = prepare_messages(payload.get("messages") if isinstance(payload.get("messages"), list) else [])
    if not any(item.get("role") == "user" for item in messages):
        raise TraeWorkChatError("messages must include a user turn", status_code=400)
    body = {
        "messages": messages,
        "function": LLM_FUNCTION,
        "stream": True,
        "config_name": model,
        "model": model,
    }
    choice, drop_tools = _tool_choice(payload.get("tool_choice"))
    tools = [] if drop_tools else _tools(payload.get("tools"))
    if tools:
        body["tools"] = tools
    if choice:
        body["tool_choice"] = choice
    for key in _SAMPLING:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            body[key] = value
    if "max_tokens" not in body:
        body["max_tokens"] = DEFAULT_MAX_TOKENS
    effort = str(payload.get("reasoning_effort") or "").strip().lower()
    if effort and effort not in {"auto", "none", "off"}:
        body["reasoning_effort"] = payload.get("reasoning_effort")
    stop = payload.get("stop")
    if isinstance(stop, (str, list)):
        body["stop"] = stop
    return body


def llm_headers(account: dict) -> dict[str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    headers = auth_headers(account)
    token = str(account.get("access_token") or "")
    headers["Accept"] = "text/event-stream"
    headers["X-Cloudide-Token"] = token
    headers["X-Ide-Token"] = token
    headers["X-App-Id"] = APP_ID
    headers["X-App-Version"] = "default"
    headers["X-Ide-Version"] = IDE_VERSION
    headers["X-Ide-Version-Code"] = IDE_VERSION_CODE
    headers["X-App-Version-Code"] = IDE_VERSION_CODE
    headers["X-Ide-Version-Type"] = "stable"
    headers["X-Device-Type"] = "windows"
    headers["Request-Traffic-Type"] = "prod"
    uid = str(account.get("uid") or "")
    machine_id = str(extra.get("machine_id") or "")
    if uid:
        headers["X-Uid"] = uid
    if machine_id:
        headers["X-Machine-Id"] = machine_id
    return headers


def _inbound_call(call: dict, index: int) -> dict:
    function = call.get("function_call") if isinstance(call.get("function_call"), dict) else {}
    if not function and isinstance(call.get("function"), dict):
        function = dict(call["function"])
    else:
        function = dict(function)
    function.pop("namespace", None)
    function.pop("partial_arguments", None)
    arguments = function.get("arguments", "")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False, separators=(",", ":"))
    function["arguments"] = arguments
    function["name"] = str(function.get("name") or "")
    item = {
        "index": int(call.get("index") if call.get("index") is not None else index),
        "type": "function",
        "function": function,
    }
    if call.get("id"):
        item["id"] = str(call["id"])
    return item


class SseAssembler:
    def __init__(self):
        self.event = ""
        self.data = ""

    def feed(self, line: str) -> dict | None:
        line = line.rstrip("\r")
        if line:
            if line.startswith("event:"):
                self.event = line[6:].strip()
            elif line.startswith("data:"):
                self.data += line[5:]
            return None
        if not self.event and not self.data:
            return None
        event, data = self.event, self.data
        self.event = ""
        self.data = ""
        return {"event": event or "message", "data": data}


def parse_event(event: str, data: str) -> dict:
    if not data.strip():
        if event == "done":
            return {"kind": "done", "finish_reason": "stop"}
        return {"kind": "ignore"}
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return {"kind": "ignore"}
    if not isinstance(payload, dict):
        return {"kind": "ignore"}
    if event == "output":
        calls = payload.get("tool_calls")
        if isinstance(calls, dict):
            calls = [calls]
        if not isinstance(calls, list):
            calls = []
        return {
            "kind": "output",
            "text": str(payload.get("response") or ""),
            "reasoning": str(payload.get("reasoning_content") or ""),
            "tool_calls": [_inbound_call(call, index) for index, call in enumerate(calls) if isinstance(call, dict)],
        }
    if event == "token_usage":
        return {"kind": "usage", "usage": payload}
    if event == "done":
        return {"kind": "done", "finish_reason": str(payload.get("finish_reason") or "stop")}
    if event == "error":
        code = payload.get("code")
        message = str(payload.get("message") or payload.get("msg") or "TraeWork upstream error")
        status = 502
        retry_auth = False
        if code in {1005, 4008, 429, 9074}:
            status = 429
        elif code in {401, 4001}:
            status = 401 if code == 401 else 404
            retry_auth = code == 401
        elif code == 413:
            status = 413
        return {"kind": "error", "message": f"{message} ({code})", "status_code": status, "retry_auth": retry_auth}
    return {"kind": "ignore"}


def _chunk(model: str, *, text: str = "", reasoning: str = "", tool_calls: list | None = None, finish: str | None = None, usage: dict | None = None) -> bytes:
    delta = {}
    if text:
        delta["content"] = text
    if reasoning:
        delta["reasoning_content"] = reasoning
    if tool_calls:
        delta["tool_calls"] = tool_calls
    payload = {
        "id": "chatcmpl-traework",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def iter_llm_events(account: dict, payload: dict, model: str) -> AsyncIterator[dict]:
    body = build_llm_body(payload, model)
    url = f"{AGENT_API}{LLM_CHAT_PATH}"
    timeout = httpx.Timeout(30.0, read=180.0)
    async with rate_limiter.account_slot(account), httpx.AsyncClient(verify=TLS_CONTEXT_NO_ENV, timeout=timeout, trust_env=False) as client:
        async with client.stream("POST", url, headers=llm_headers(account), json=body) as response:
            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", errors="replace")[:240]
                raise TraeWorkChatError(raw or f"HTTP {response.status_code}", status_code=response.status_code, retry_auth=response.status_code == 401)
            parser = SseAssembler()
            async for line in response.aiter_lines():
                found = parser.feed(line)
                if not found:
                    continue
                parsed = parse_event(found["event"], found["data"])
                if parsed["kind"] == "error":
                    raise TraeWorkChatError(parsed["message"], status_code=parsed["status_code"], retry_auth=parsed["retry_auth"])
                yield parsed


async def stream_llm(account: dict, payload: dict, model: str, shown: str) -> AsyncIterator[bytes]:
    usage = None
    saw_tools = False
    finish = "stop"
    async for event in iter_llm_events(account, payload, model):
        if event["kind"] == "usage":
            usage = event["usage"]
            continue
        if event["kind"] == "output":
            calls = event["tool_calls"]
            if calls:
                saw_tools = True
            if event["text"] or event["reasoning"] or calls:
                yield _chunk(shown, text=event["text"], reasoning=event["reasoning"], tool_calls=calls or None)
            continue
        if event["kind"] == "done":
            finish = "tool_calls" if saw_tools else (event.get("finish_reason") or "stop")
    yield _chunk(shown, finish=finish, usage=usage)
    yield b"data: [DONE]\n\n"
