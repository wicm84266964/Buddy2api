"""
proxy.py — 请求代理转发

功能：
  - 转发到 copilot.tencent.com/v2/chat/completions
  - 流式 SSE 原样转发
  - 非流式 SSE 聚合为单个 JSON
  - tool_calls 分片合并
  - usage 统计
  - 账号故障自动切换
"""

import json
import os
import time
from typing import AsyncGenerator, Optional

import httpx

import database as db
import auth_manager

BACKEND = "https://copilot.tencent.com"

PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

DEFAULT_MODELS = [
    {"id": "glm-5.2", "name": "GLM-5.2"},
    {"id": "glm-5.1", "name": "GLM-5.1"},
    {"id": "glm-5v-turbo", "name": "GLM-5V Turbo"},
    {"id": "kimi-k2.7", "name": "Kimi K2.7"},
    {"id": "kimi-k2.6", "name": "Kimi K2.6"},
    {"id": "kimi-k2.5", "name": "Kimi K2.5"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
    {"id": "minimax-m3-pay", "name": "MiniMax M3"},
    {"id": "hy3-preview-agent", "name": "HY3 Preview Agent"},
    {"id": "auto", "name": "Auto (auto routing)"},
]


def build_backend_body(payload: dict) -> dict:
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def _safe_err(raw: bytes, status: int) -> dict:
    try:
        detail = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        detail = {"error": {"message": raw.decode("utf-8", "replace")[:500],
                            "type": "upstream_error"}}
    return detail


def _err_sse_event(raw: bytes, status: int) -> bytes:
    msg = raw.decode("utf-8", "replace")[:500]
    payload = json.dumps({"error": {"message": msg, "type": "upstream_error", "code": status}})
    event = f"data: {payload}\n\ndata: [DONE]\n\n"
    return event.encode("utf-8")


def _log_request(api_key_info, account, model_name, stream,
                 prompt_t, completion_t, total_t, credit,
                 finish_reason, status_code, error_msg, t0):
    elapsed_ms = int((time.time() - t0) * 1000)
    log_data = {
        "api_key_id": api_key_info["id"] if api_key_info else None,
        "api_key_name": api_key_info["name"] if api_key_info else None,
        "account_id": account["id"] if account else None,
        "account_name": account.get("name") if account else None,
        "model": model_name,
        "stream": 1 if stream else 0,
        "prompt_tokens": prompt_t,
        "completion_tokens": completion_t,
        "total_tokens": total_t,
        "credit": credit,
        "finish_reason": finish_reason,
        "duration_ms": elapsed_ms,
        "status_code": status_code,
        "error_msg": error_msg,
    }
    try:
        db.add_log(log_data)
    except Exception:
        pass
    if account:
        try:
            db.account_increment_usage(account["id"], total_t, credit)
        except Exception:
            pass
    if api_key_info:
        try:
            db.api_key_increment_usage(api_key_info["id"], total_t)
        except Exception:
            pass


async def proxy_chat_completions(
    payload: dict,
    api_key_info: Optional[dict] = None,
) -> tuple:
    """
    主代理函数。

    返回:
      - ("stream", async_generator)  流式响应
      - ("json", dict)               非流式响应
      - ("error", (status_code, detail))  错误
    """
    client_wants_stream = bool(payload.get("stream"))
    body = build_backend_body(payload)
    model_name = payload.get("model", "auto")

    tried_ids: set[int] = set()
    max_retries = 3

    for attempt in range(max_retries):
        account = auth_manager.pick_account_with_fallback(tried_ids)
        if not account:
            return ("error", (503, {"error": {"message": "No available accounts", "type": "server_error"}}))

        tried_ids.add(account["id"])
        headers = auth_manager.get_valid_headers(account)
        if not headers:
            continue

        url = f"{BACKEND}/v2/chat/completions"
        t0 = time.time()

        if client_wants_stream:
            gen = _stream_upstream(url, headers, body, account, api_key_info, model_name, t0)
            return ("stream", gen)
        else:
            result = await _collect_stream(url, headers, body, account, api_key_info, model_name, t0)
            if result[0] == "error" and attempt < max_retries - 1:
                err_status = result[1][0]
                if err_status in (401, 403):
                    continue
            return result

    return ("error", (503, {"error": {"message": "All accounts failed", "type": "server_error"}}))


async def _stream_upstream(
    url: str, headers: dict, body: dict,
    account: dict, api_key_info: Optional[dict],
    model_name: str, t0: float,
) -> AsyncGenerator[bytes, None]:
    """流式转发后端 SSE，同时统计 usage。"""
    finish_reason = None
    usage: dict = {}
    buf = b""
    error_occurred = False

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10)) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    error_occurred = True
                    yield _err_sse_event(err, r.status_code)
                    _log_request(api_key_info, account, model_name, True,
                                 0, 0, 0, 0, "error", r.status_code,
                                 err.decode("utf-8", "replace")[:500], t0)
                    return

                async for chunk in r.aiter_bytes():
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.strip()
                            if not line.startswith(b"data:"):
                                continue
                            data = line[5:].strip()
                            if data == b"[DONE]":
                                continue
                            try:
                                obj = json.loads(data)
                            except Exception:
                                continue
                            if obj.get("usage"):
                                usage.update(obj["usage"])
                            for ch in obj.get("choices") or []:
                                if ch.get("finish_reason"):
                                    finish_reason = ch["finish_reason"]
                        yield chunk
    except httpx.HTTPError as e:
        error_occurred = True
        yield _err_sse_event(str(e).encode(), 502)
        _log_request(api_key_info, account, model_name, True,
                     0, 0, 0, 0, "network_error", 502, str(e)[:500], t0)
        return

    if not error_occurred:
        _log_request(
            api_key_info, account, model_name, True,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            usage.get("total_tokens", 0),
            usage.get("credit", 0),
            finish_reason or "stop", 200, "", t0,
        )


async def _collect_stream(
    url: str, headers: dict, body: dict,
    account: dict, api_key_info: Optional[dict],
    model_name: str, t0: float,
) -> tuple:
    """聚合 SSE 流为单个非流式 JSON。"""
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    try:
        async with httpx.AsyncClient(timeout=300) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    detail = _safe_err(raw, r.status_code)
                    _log_request(api_key_info, account, model_name, False,
                                 0, 0, 0, 0, "error", r.status_code,
                                 raw.decode("utf-8", "replace")[:500], t0)
                    return ("error", (r.status_code, detail))

                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    model = chunk.get("model") or model
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
    except httpx.HTTPError as e:
        _log_request(api_key_info, account, model_name, False,
                     0, 0, 0, 0, "network_error", 502, str(e)[:500], t0)
        return ("error", (502, {"error": {"message": f"upstream error: {e}", "type": "upstream_error"}}))

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    result = {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or model_name,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    u = usage or {}
    _log_request(
        api_key_info, account, model_name, False,
        u.get("prompt_tokens", 0),
        u.get("completion_tokens", 0),
        u.get("total_tokens", 0),
        u.get("credit", 0),
        finish_reason or "stop", 200, "", t0,
    )
    return ("json", result)
