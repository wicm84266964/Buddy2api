"""
responses.py — Responses API → Chat Completions 协议转换层

Codex 从 2026.2 起强制要求 wire_api="responses"，不再支持 chat/completions。
此模块在 Buddy2api 内部把 /v1/responses 请求转换为 Chat Completions 格式转发，
再映射响应/流事件回 Responses API 格式。
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import proxy
from reasoning_controls import normalize_chat_reasoning
from streaming import ClosingStream, close_stream
from providers.usage import credit_amount


_DEBUG_SECRET_KEYS = {
    "access_token", "accesstoken", "refresh_token", "refreshtoken",
    "api_key", "authorization", "session_state", "sessionstate",
}
_DEBUG_CONTENT_KEYS = {"content", "input", "instructions", "output"}
_RESPONSE_ERROR_CODES = frozenset({
    "server_error",
    "rate_limit_exceeded",
    "invalid_prompt",
    "vector_store_timeout",
    "invalid_image",
    "invalid_image_format",
    "invalid_base64_image",
    "invalid_image_url",
    "image_too_large",
    "image_too_small",
    "image_parse_error",
    "image_content_policy_violation",
    "invalid_image_mode",
    "image_file_too_large",
    "unsupported_image_media_type",
    "empty_image_file",
    "failed_to_download_image",
    "image_file_not_found",
})


def _redact_debug_value(value, *, include_content: bool = False):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).replace("-", "").replace("_", "").lower()
            if normalized in {name.replace("_", "") for name in _DEBUG_SECRET_KEYS}:
                result[key] = "<redacted>"
            elif key in _DEBUG_CONTENT_KEYS and not include_content:
                result[key] = "<content redacted>"
            else:
                result[key] = _redact_debug_value(item, include_content=include_content)
        return result
    if isinstance(value, list):
        return [_redact_debug_value(item, include_content=include_content) for item in value]
    return value


def _maybe_dump(label: str, obj) -> None:
    """Write an opt-in, redacted debug dump for protocol troubleshooting."""
    if os.environ.get("CB_DEBUG_DUMP", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        d = Path(__file__).parent / ".debug"
        d.mkdir(exist_ok=True)
        if os.name != "nt":
            os.chmod(d, 0o700)
        ts = int(time.time() * 1000)
        include_content = os.environ.get("CB_DEBUG_DUMP_INCLUDE_CONTENT", "") == "1"
        target = d / f"{label}_{ts}.json"
        target.write_text(
            json.dumps(
                _redact_debug_value(obj, include_content=include_content),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(target, 0o600)
    except Exception:
        pass


def _structured_response_format(resp_payload: dict) -> Optional[dict]:
    """Map Responses text.format onto Chat response_format.

    Only json_schema and json_object are forwarded. text and ordinary requests
    stay unchanged so existing Codex conversations are not forced into JSON.
    """
    text = resp_payload.get("text")
    if not isinstance(text, dict):
        return None
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return None
    fmt_type = fmt.get("type")
    if fmt_type == "json_object":
        return {"type": "json_object"}
    if fmt_type != "json_schema":
        return None

    nested = fmt.get("json_schema") if isinstance(fmt.get("json_schema"), dict) else {}
    schema = nested.get("schema", fmt.get("schema"))
    schema_obj = {
        "name": nested.get("name") or fmt.get("name") or "response",
        "schema": schema if isinstance(schema, dict) else {},
    }
    description = nested.get("description", fmt.get("description"))
    if description is not None:
        schema_obj["description"] = description
    if "strict" in nested:
        schema_obj["strict"] = nested["strict"]
    elif "strict" in fmt:
        schema_obj["strict"] = fmt["strict"]
    return {"type": "json_schema", "json_schema": schema_obj}


def _structured_output_instruction(response_format: dict) -> Optional[str]:
    """Ask for schema JSON in its own system message, separate from instructions."""
    if not isinstance(response_format, dict):
        return None
    fmt_type = response_format.get("type")
    if fmt_type == "json_object":
        return (
            "Respond with a single JSON object only. "
            "Do not include markdown, commentary, or code fences."
        )
    if fmt_type != "json_schema":
        return None

    schema_obj = response_format.get("json_schema")
    schema = schema_obj.get("schema") if isinstance(schema_obj, dict) else None
    parts = [
        "Respond with a single JSON object that matches the required schema. "
        "Do not include markdown, commentary, or code fences."
    ]
    if isinstance(schema, dict) and schema:
        try:
            parts.append("Schema: " + json.dumps(schema, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    return "\n".join(parts)


def responses_to_chat(resp_payload: dict) -> dict:
    """
    将 Responses API 请求转换为 Chat Completions 请求。
    
    Responses 请求结构:
      model, input[], instructions, tools[], stream, temperature,
      max_output_tokens, reasoning.effort, text.format
    
    Chat 请求结构:
      model, messages[], tools[], stream, temperature, max_tokens,
      response_format
    """
    messages = []

    # instructions → system message
    instructions = resp_payload.get("instructions")
    if instructions:
        inst_content = _flatten_content(instructions)
        if inst_content:
            messages.append({"role": "system", "content": inst_content})

    # Keep the schema constraint in its own message so a long instruction
    # cannot bury it.
    response_format = _structured_response_format(resp_payload)
    if response_format:
        constraint = _structured_output_instruction(response_format)
        if constraint:
            messages.append({"role": "system", "content": constraint})

    # input[] → messages[]
    inp = resp_payload.get("input")
    if isinstance(inp, list):
        pending_tool_calls = []
        for item in inp:
            chat_msg = _input_item_to_chat_message(item)
            if not chat_msg:
                continue

            # Responses represents parallel calls as adjacent function_call items.
            # Chat Completions requires those calls on one assistant message so the
            # following tool results all belong to the same turn.
            if chat_msg.get("role") == "assistant" and chat_msg.get("tool_calls"):
                pending_tool_calls.extend(chat_msg["tool_calls"])
                continue

            if pending_tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": pending_tool_calls,
                })
                pending_tool_calls = []

            if chat_msg.get("role") == "developer":
                chat_msg["role"] = "system"
            messages.append(chat_msg)

        if pending_tool_calls:
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": pending_tool_calls,
            })
    elif isinstance(inp, str) and inp.strip():
        messages.append({"role": "user", "content": inp})

    # tools[]: Responses 格式 {type:"function", name:..., parameters:...}
    #          → Chat 格式 {type:"function", function: {name:..., parameters:...}}
    # 非 function 类型工具（web_search, file_search 等）跳过，Chat API 不支持
    raw_tools = resp_payload.get("tools") or []
    tools = []
    for t in raw_tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            name = t.get("name", "")
            if not name:
                continue  # 没有函数名的跳过
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": t.get("description"),
                    "parameters": t.get("parameters"),
                    "strict": t.get("strict"),
                },
            })
        else:
            # web_search, file_search, code_interpreter 等后端不支持：跳过，
            # 但记录日志，便于诊断"模型因缺少工具而用文字代替"的退化（issue #31）。
            print(
                "[responses] dropping unsupported tool type "
                f"{t.get('type')!r}"
                + (f" name={t.get('name')!r}" if t.get('name') else ""),
                file=sys.stderr,
            )

    # tool_choice
    tool_choice = resp_payload.get("tool_choice")
    if tool_choice and isinstance(tool_choice, str):
        # Responses uses simple string "auto"/"none"/"required"
        # Chat uses "auto"/"none"/"required" or {"type":"function","function":{"name":"x"}}
        pass

    chat_payload = {
        "model": resp_payload.get("model", "auto"),
        "messages": messages,
        "stream": resp_payload.get("stream", False),
    }
    if tools:
        chat_payload["tools"] = tools
    if tool_choice is not None:
        chat_payload["tool_choice"] = tool_choice
    if resp_payload.get("temperature") is not None:
        chat_payload["temperature"] = resp_payload["temperature"]
    if resp_payload.get("max_output_tokens"):
        chat_payload["max_tokens"] = resp_payload["max_output_tokens"]
    if resp_payload.get("top_p") is not None:
        chat_payload["top_p"] = resp_payload["top_p"]
    if response_format is not None:
        chat_payload["response_format"] = response_format

    # Responses prefers reasoning.effort. Compatibility forms used by
    # OpenCode, DSH, Cherry Studio, and Claude-style clients are normalized
    # to the Chat reasoning_effort field here.
    normalized_reasoning = normalize_chat_reasoning(resp_payload, prefer_nested=True)
    for key in (
        "reasoning_effort",
        "reasoning_summary",
        "reasoning",
        "thinking",
        "output_config",
    ):
        if key in normalized_reasoning:
            chat_payload[key] = normalized_reasoning[key]

    return chat_payload


def _input_item_to_chat_message(item) -> Optional[dict]:
    """将单个 input item 转换为 Chat message。"""
    if not isinstance(item, dict):
        return None

    kind = item.get("type", "")

    if kind == "message":
        role = item.get("role", "user")
        # Codex 可能发送 "developer" 角色，映射为 system
        if role == "developer":
            role = "system"
        content = _flatten_content(item.get("content"))
        msg = {"role": role, "content": content}
        if item.get("name"):
            msg["name"] = item["name"]
        return msg

    elif kind == "function_call_output":
        call_id = item.get("call_id", "")
        output = item.get("output", "")
        if isinstance(output, dict):
            output = json.dumps(output)
        return {"role": "tool", "tool_call_id": call_id, "content": str(output)}

    elif kind == "function_call":
        # 历史 function_call in input（罕见，保留）
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": item.get("call_id", item.get("id", "")),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json.dumps(item.get("arguments", {})) if isinstance(item.get("arguments"), dict) else str(item.get("arguments", "")),
                },
            }],
        }

    elif kind == "reasoning":
        # reasoning items 没有直接的 chat 对等物，跳过
        return None

    else:
        # 未知类型：尝试作为 user message 处理
        content = _flatten_content(item.get("content"))
        if content:
            return {"role": "user", "content": content}
        return None


def _flatten_content(content) -> str:
    """展平 content 字段：字符串直接返回，数组取 text，对象取字符串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                pt = p.get("type", "")
                if pt in ("input_text", "output_text", "text"):
                    parts.append(str(p.get("text", "")))
                elif pt == "image_url" or pt == "input_image":
                    url = p.get("image_url", "")
                    url_str = url.get("url", "") if isinstance(url, dict) else str(url)
                    parts.append(f"[image: {url_str}]")
                elif pt == "file":
                    parts.append("[file]")
                else:
                    parts.append(str(p.get("text", p)))
        return "\n".join(parts)
    if isinstance(content, dict):
        return str(content.get("text", str(content)))
    return str(content)

# Chat → Responses 响应映射（非流式）
# ============================================================

def _responses_usage(usage: dict) -> dict:
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
    input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    result = {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": input_details.get("cached_tokens", 0),
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": output_details.get("reasoning_tokens", 0),
        },
        "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
    }
    for key in ("credit", "points_consumed"):
        amount = credit_amount({key: usage.get(key)})
        if amount is not None:
            result[key] = amount
    return result


def _response_error(error: Optional[dict]) -> Optional[dict]:
    if not error:
        return None
    code = str(error.get("code") or "server_error")
    if code == "local_rate_limit" or error.get("type") == "rate_limit_error":
        code = "rate_limit_exceeded"
    return {
        "code": code if code in _RESPONSE_ERROR_CODES else "server_error",
        "message": str(error.get("message") or "The upstream response failed."),
    }


def _response_request_fields(resp_payload: Optional[dict]) -> dict:
    payload = resp_payload if isinstance(resp_payload, dict) else {}
    parallel_tool_calls = payload.get("parallel_tool_calls")
    if not isinstance(parallel_tool_calls, bool):
        parallel_tool_calls = True
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = []
    tool_choice = payload.get("tool_choice")
    if tool_choice is None:
        tool_choice = "auto"
    return {
        "parallel_tool_calls": parallel_tool_calls,
        "tool_choice": tool_choice,
        "tools": tools,
    }


def chat_response_to_responses(
    chat_resp: dict,
    model: str,
    resp_payload: Optional[dict] = None,
) -> dict:
    """将 Chat Completions 非流式响应转换为 Responses API 响应。"""
    resp_id = "resp_" + chat_resp.get("id", os.urandom(12).hex())
    output = []

    for choice in chat_resp.get("choices") or []:
        msg = choice.get("message") or {}

        # reasoning 内容
        reasoning = msg.get("reasoning_content")
        if reasoning:
            output.append({
                "type": "reasoning",
                "id": _gen_item_id("rsn"),
                "status": "completed",
                "summary": [{"type": "summary_text", "text": reasoning}],
            })

        # 文本内容。部分兼容上游会把 reasoning_content 复制进空 content，
        # 相同文本只作为 reasoning 输出，避免客户端重复显示。
        content = msg.get("content")
        if content and content != reasoning:
            output.append({
                "type": "message",
                "id": _gen_item_id("msg"),
                "role": "assistant",
                "status": "completed",
                "content": [{
                    "type": "output_text",
                    "text": content,
                    "annotations": [],
                }],
            })

        # tool_calls
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            output.append({
                "type": "function_call",
                "id": tc.get("id") or _gen_item_id("fc"),
                "call_id": tc.get("id") or _gen_item_id("fc"),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", ""),
                "status": "completed",
            })

    finish_reasons = {
        choice.get("finish_reason")
        for choice in chat_resp.get("choices") or []
        if choice.get("finish_reason")
    }
    status = "completed"
    incomplete_details = None
    error = None
    if "length" in finish_reasons:
        status = "incomplete"
        incomplete_details = {"reason": "max_output_tokens"}
    elif "content_filter" in finish_reasons:
        status = "incomplete"
        incomplete_details = {"reason": "content_filter"}
    elif not output:
        status = "failed"
        error = {
            "code": "empty_upstream_response",
            "message": "The upstream response contained no displayable content or tool call.",
        }

    usage = chat_resp.get("usage") or {}
    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": chat_resp.get("model") or model,
        "output": output,
        "error": _response_error(error),
        "incomplete_details": incomplete_details,
        **_response_request_fields(resp_payload),
        "usage": _responses_usage(usage),
    }


# ============================================================
# 流式 SSE: Chat delta → Responses events
# ============================================================


async def _iter_chat_sse_data(
    chat_stream: AsyncGenerator[bytes | bytearray | str, None],
) -> AsyncGenerator[str, None]:
    """Yield complete SSE data events from HTTP byte chunks or text SSE frames."""
    buffer = b""
    data_lines: list[bytes] = []
    event_bytes = 0
    max_event_bytes = 8 * 1024 * 1024

    def take_line(*, final: bool = False) -> Optional[bytes]:
        nonlocal buffer
        for index, value in enumerate(buffer):
            if value == 0x0A:
                line = buffer[:index]
                buffer = buffer[index + 1:]
                return line[:-1] if line.endswith(b"\r") else line
            if value == 0x0D:
                if index + 1 == len(buffer) and not final:
                    return None
                end = index + 2 if buffer[index + 1:index + 2] == b"\n" else index + 1
                line = buffer[:index]
                buffer = buffer[end:]
                return line
        if final and buffer:
            line = buffer
            buffer = b""
            return line
        return None

    def consume_line(line: bytes) -> Optional[str]:
        nonlocal data_lines, event_bytes
        if len(line) > max_event_bytes:
            raise ValueError("upstream SSE line exceeds the size limit")
        if not line:
            if not data_lines:
                return None
            data = b"\n".join(data_lines)
            data_lines = []
            event_bytes = 0
            return data.decode("utf-8")
        if line.startswith(b"data:"):
            data = line[5:]
            if data.startswith(b" "):
                data = data[1:]
            event_bytes += len(data) + 1
            if event_bytes > max_event_bytes:
                raise ValueError("upstream SSE event exceeds the size limit")
            data_lines.append(data)
        return None

    async for chunk in chat_stream:
        if not chunk:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("upstream stream chunks must be bytes")
        buffer += bytes(chunk)
        if len(buffer) > max_event_bytes and b"\n" not in buffer and b"\r" not in buffer:
            raise ValueError("upstream SSE line exceeds the size limit")

        while True:
            line = take_line()
            if line is None:
                break
            data = consume_line(line)
            if data is not None:
                yield data

    while True:
        line = take_line(final=True)
        if line is None:
            break
        data = consume_line(line)
        if data is not None:
            yield data
    if data_lines:
        yield b"\n".join(data_lines).decode("utf-8")


def chat_stream_to_responses_stream(
    chat_stream: AsyncGenerator[bytes | bytearray | str, None],
    model: str,
    resp_payload: Optional[dict] = None,
) -> ClosingStream:
    """Convert a Chat Completions SSE stream into Responses API events."""
    data_stream = ClosingStream(_iter_chat_sse_data(chat_stream), upstream=chat_stream)
    return ClosingStream(
        _chat_stream_to_responses_stream(data_stream, model, resp_payload),
        upstream=data_stream,
    )


async def _chat_stream_to_responses_stream(data_stream, model, resp_payload):
    resp_id = "resp_" + os.urandom(12).hex()
    created_at = int(time.time())
    response_model = model
    seq = -1
    output_items: list[dict] = []
    item_states: list[dict] = []
    reasoning_states: dict[int, dict] = {}
    text_states: dict[int, dict] = {}
    tool_states: dict[tuple[int, int], dict] = {}
    usage: dict = {}
    seen_choices: set[int] = set()
    finished_choices: dict[int, str] = {}
    productive_choices: set[int] = set()
    saw_done = False
    stream_error: Optional[dict] = None
    request_fields = _response_request_fields(resp_payload)

    def event(event_name: str, **data) -> str:
        nonlocal seq
        seq += 1
        return _make_sse_event(event_name, {
            "type": event_name,
            "sequence_number": seq,
            **data,
        })

    def response_usage() -> dict:
        return _responses_usage(usage)

    def response_snapshot(status: str, **extra) -> dict:
        snapshot = {
            "id": resp_id,
            "object": "response",
            "created_at": created_at,
            "status": status,
            "model": response_model,
            "output": output_items,
            "error": None,
            "incomplete_details": None,
            "usage": None if status == "in_progress" else response_usage(),
            **request_fields,
        }
        if extra.get("error") is not None:
            extra["error"] = _response_error(extra["error"])
        snapshot.update(extra)
        return snapshot

    def close_item(state: dict, status: str) -> list[str]:
        if state.get("closed"):
            return []
        item = state["item"]
        output_index = state["output_index"]
        item["status"] = status
        events = []
        if state["kind"] == "reasoning":
            item["summary"] = [{
                "type": "summary_text",
                "text": state["text"],
            }]
            events.append(event(
                "response.reasoning_summary_text.done",
                item_id=item["id"],
                output_index=output_index,
                summary_index=0,
                text=state["text"],
            ))
            events.append(event(
                "response.reasoning_summary_part.done",
                item_id=item["id"],
                output_index=output_index,
                summary_index=0,
                part={
                    "type": "summary_text",
                    "text": state["text"],
                },
            ))
        elif state["kind"] == "text":
            events.append(event(
                "response.output_text.done",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                text=state["text"],
                logprobs=[],
            ))
            events.append(event(
                "response.content_part.done",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                part={
                    "type": "output_text",
                    "text": state["text"],
                    "annotations": [],
                    "logprobs": [],
                },
            ))
        elif status == "completed":
            events.append(event(
                "response.function_call_arguments.done",
                item_id=item["id"],
                output_index=output_index,
                arguments=state["arguments"],
            ))
        events.append(event(
            "response.output_item.done",
            output_index=output_index,
            item=item,
        ))
        state["closed"] = True
        return events

    try:
        yield event("response.created", response=response_snapshot("in_progress"))
        yield event("response.in_progress", response=response_snapshot("in_progress"))
        async for data_str in data_stream:
            data_str = data_str.strip()
            if not data_str:
                continue
            if data_str == "[DONE]":
                saw_done = True
                break

            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                stream_error = {
                    "code": "invalid_upstream_event",
                    "message": "The upstream returned a malformed SSE JSON event.",
                }
                break
            if not isinstance(chunk, dict):
                stream_error = {
                    "code": "invalid_upstream_event",
                    "message": "The upstream returned a non-object SSE event.",
                }
                break
            if chunk.get("error"):
                upstream_error = chunk["error"]
                if isinstance(upstream_error, dict):
                    message = upstream_error.get("message") or "The upstream stream failed."
                    code = upstream_error.get("code") or upstream_error.get("type") or "upstream_error"
                else:
                    message = str(upstream_error)
                    code = "upstream_error"
                stream_error = {"code": str(code), "message": str(message)[:500]}
                break

            response_model = chunk.get("model") or response_model
            if chunk.get("usage"):
                usage.update(chunk["usage"])

            for choice in chunk.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                try:
                    choice_index = int(choice.get("index", 0))
                except (TypeError, ValueError):
                    choice_index = 0
                seen_choices.add(choice_index)
                delta = choice.get("delta") or {}
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    finished_choices[choice_index] = str(finish_reason)

                reasoning_delta = delta.get("reasoning_content", "")
                if reasoning_delta:
                    has_open_tool = any(
                        current_choice == choice_index and not state.get("closed")
                        for (current_choice, _), state in tool_states.items()
                    )
                    if choice_index in text_states or has_open_tool:
                        stream_error = {
                            "code": "invalid_upstream_event",
                            "message": (
                                "The upstream emitted reasoning after answer or tool output "
                                "had already started."
                            ),
                        }
                        break
                    if not isinstance(reasoning_delta, str):
                        reasoning_delta = str(reasoning_delta)
                    productive_choices.add(choice_index)
                    state = reasoning_states.get(choice_index)
                    if state is None:
                        text_state = text_states.pop(choice_index, None)
                        if text_state:
                            for pending_event in close_item(text_state, "completed"):
                                yield pending_event
                        item = {
                            "type": "reasoning",
                            "id": _gen_item_id("rsn"),
                            "status": "in_progress",
                            "summary": [],
                        }
                        output_index = len(output_items)
                        output_items.append(item)
                        state = {
                            "kind": "reasoning",
                            "item": item,
                            "output_index": output_index,
                            "text": "",
                            "closed": False,
                        }
                        reasoning_states[choice_index] = state
                        item_states.append(state)
                        yield event(
                            "response.output_item.added",
                            output_index=output_index,
                            item=item,
                        )
                        yield event(
                            "response.reasoning_summary_part.added",
                            item_id=item["id"],
                            output_index=output_index,
                            summary_index=0,
                            part={"type": "summary_text", "text": ""},
                        )
                    state["text"] += reasoning_delta
                    state["item"]["summary"] = [{
                        "type": "summary_text",
                        "text": state["text"],
                    }]
                    yield event(
                        "response.reasoning_summary_text.delta",
                        item_id=state["item"]["id"],
                        output_index=state["output_index"],
                        summary_index=0,
                        delta=reasoning_delta,
                    )

                text = delta.get("content", "")
                if reasoning_delta and text == reasoning_delta:
                    text = ""
                reasoning_state = reasoning_states.get(choice_index)
                if text and reasoning_state and text == reasoning_state["text"]:
                    text = ""
                if text:
                    if not isinstance(text, str):
                        text = str(text)
                    productive_choices.add(choice_index)
                    state = text_states.get(choice_index)
                    if state is None:
                        reasoning_state = reasoning_states.pop(choice_index, None)
                        if reasoning_state:
                            for pending_event in close_item(reasoning_state, "completed"):
                                yield pending_event
                        item = {
                            "type": "message",
                            "id": _gen_item_id("msg"),
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        }
                        output_index = len(output_items)
                        output_items.append(item)
                        state = {
                            "kind": "text",
                            "item": item,
                            "output_index": output_index,
                            "text": "",
                            "closed": False,
                        }
                        text_states[choice_index] = state
                        item_states.append(state)
                        yield event(
                            "response.output_item.added",
                            output_index=output_index,
                            item=item,
                        )
                        yield event(
                            "response.content_part.added",
                            item_id=item["id"],
                            output_index=output_index,
                            content_index=0,
                            part={
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                                "logprobs": [],
                            },
                        )
                    state["text"] += text
                    state["item"]["content"] = [{
                        "type": "output_text",
                        "text": state["text"],
                        "annotations": [],
                        "logprobs": [],
                    }]
                    yield event(
                        "response.output_text.delta",
                        item_id=state["item"]["id"],
                        output_index=state["output_index"],
                        content_index=0,
                        delta=text,
                        logprobs=[],
                    )

                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    productive_choices.add(choice_index)
                    try:
                        tool_index = int(tc.get("index", 0))
                    except (TypeError, ValueError):
                        tool_index = 0
                    key = (choice_index, tool_index)
                    state = tool_states.get(key)
                    fn = tc.get("function") or {}

                    if state is None:
                        reasoning_state = reasoning_states.pop(choice_index, None)
                        if reasoning_state:
                            for pending_event in close_item(reasoning_state, "completed"):
                                yield pending_event
                        text_state = text_states.pop(choice_index, None)
                        if text_state:
                            for pending_event in close_item(text_state, "completed"):
                                yield pending_event
                        call_id = tc.get("id") or _gen_item_id("call")
                        item = {
                            "type": "function_call",
                            "id": _gen_item_id("fc"),
                            "call_id": call_id,
                            "name": str(fn.get("name") or ""),
                            "arguments": "",
                            "status": "in_progress",
                        }
                        output_index = len(output_items)
                        output_items.append(item)
                        state = {
                            "kind": "tool",
                            "item": item,
                            "output_index": output_index,
                            "arguments": "",
                            "closed": False,
                        }
                        tool_states[key] = state
                        item_states.append(state)
                        yield event(
                            "response.output_item.added",
                            output_index=output_index,
                            item=item,
                        )

                    name = fn.get("name")
                    if name:
                        name = str(name)
                        current_name = state["item"]["name"]
                        if not current_name or name.startswith(current_name):
                            state["item"]["name"] = name
                        elif not current_name.endswith(name):
                            state["item"]["name"] += name

                    if fn.get("arguments") is not None and fn.get("arguments") != "":
                        args = fn["arguments"]
                        if not isinstance(args, str):
                            args = json.dumps(args, ensure_ascii=False)
                        state["arguments"] += args
                        state["item"]["arguments"] = state["arguments"]
                        yield event(
                            "response.function_call_arguments.delta",
                            item_id=state["item"]["id"],
                            output_index=state["output_index"],
                            delta=args,
                        )
            if stream_error:
                break
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        stream_error = {
            "code": "upstream_stream_error",
            "message": f"Unable to decode the upstream SSE stream: {exc}",
        }
    except Exception as exc:
        stream_error = {
            "code": "upstream_stream_error",
            "message": f"The upstream SSE stream failed: {exc}",
        }
    finally:
        await close_stream(data_stream)

    terminal_status = "completed"
    incomplete_reason = None
    finish_reasons = set(finished_choices.values())
    unfinished_choices = seen_choices.difference(finished_choices)
    if stream_error:
        terminal_status = "failed"
    elif "length" in finish_reasons:
        terminal_status = "incomplete"
        incomplete_reason = "max_output_tokens"
    elif "content_filter" in finish_reasons:
        terminal_status = "incomplete"
        incomplete_reason = "content_filter"
    elif (
        (not saw_done and (not seen_choices or unfinished_choices))
        or (saw_done and finished_choices and unfinished_choices)
    ):
        terminal_status = "failed"
        stream_error = {
            "code": "upstream_stream_ended",
            "message": "The upstream stream ended before a terminal event.",
        }
    elif not seen_choices or not output_items or any(
        reason not in {"length", "content_filter"}
        and choice_index not in productive_choices
        for choice_index, reason in finished_choices.items()
    ):
        terminal_status = "failed"
        stream_error = {
            "code": "empty_upstream_response",
            "message": "The upstream response contained no displayable content or tool call.",
        }

    if terminal_status == "completed":
        for state in tool_states.values():
            if state.get("closed"):
                continue
            try:
                parsed_arguments = json.loads(state["arguments"])
            except (json.JSONDecodeError, TypeError):
                parsed_arguments = None
            if not state["item"]["name"] or not isinstance(parsed_arguments, dict):
                terminal_status = "failed"
                stream_error = {
                    "code": "invalid_tool_arguments",
                    "message": "The upstream returned incomplete or invalid JSON tool arguments.",
                }
                break

    item_status = "completed" if terminal_status == "completed" else "incomplete"
    for state in sorted(item_states, key=lambda value: value["output_index"]):
        for pending_event in close_item(state, item_status):
            yield pending_event

    if terminal_status == "completed":
        yield event(
            "response.completed",
            response=response_snapshot("completed"),
        )
    elif terminal_status == "incomplete":
        yield event(
            "response.incomplete",
            response=response_snapshot(
                "incomplete",
                incomplete_details={"reason": incomplete_reason},
            ),
        )
    else:
        yield event(
            "response.failed",
            response=response_snapshot(
                "failed",
                error=stream_error or {
                    "code": "upstream_stream_error",
                    "message": "The upstream stream failed.",
                },
            ),
        )


def _make_sse_event(event_name: str, data: dict) -> str:
    """构建 Responses API 风格的 SSE 事件字符串。"""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _gen_item_id(prefix: str) -> str:
    return f"{prefix}_{os.urandom(8).hex()}"


async def proxy_responses(
    resp_payload: dict,
    api_key_info: Optional[dict] = None,
    *,
    chat_handler=None,
) -> tuple:
    """
    主代理函数 — Responses API 版本。
    
    返回:
      - ("stream", async_generator)  流式响应
      - ("json", dict)               非流式响应
      - ("error", (status_code, detail)) 错误
    """
    # 1. 转换为 Chat Completions 格式
    chat_payload = responses_to_chat(resp_payload)
    _maybe_dump("responses_request_raw", resp_payload)
    _maybe_dump("responses_request_chat", chat_payload)

    # 2. 调用现有 proxy
    handler = chat_handler or proxy.proxy_chat_completions
    result = await handler(chat_payload, api_key_info)
    
    if result[0] == "error":
        status, detail = result[1]
        if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
            if detail["error"].get("code") == "local_rate_limit":
                return ("error", (status, {**detail, "error": {
                    **detail["error"], "code": "rate_limit_exceeded",
                }}))
        return result
    
    model = chat_payload.get("model", "auto")
    
    if result[0] == "json":
        # 非流式: 映射响应
        chat_resp = result[1]
        resp = chat_response_to_responses(chat_resp, model, resp_payload)
        return ("json", resp)
    
    elif result[0] == "stream":
        # 流式: 映射事件
        chat_gen = result[1]
        resp_gen = chat_stream_to_responses_stream(chat_gen, model, resp_payload)
        return ("stream", resp_gen)
