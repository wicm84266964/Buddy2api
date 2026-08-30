"""TraeWork chat via remote chat_sessions. Isolated HTTP client."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

import httpx

import auth_manager
import database as db
from providers.traework.constants import (
    AGENT_API,
    AGENT_ID,
    ALIASES,
    CHANNEL_ID,
    SESSION_MODE,
    SESSIONS_PATH,
    STATIC_MODELS,
)
from providers.traework.token import TraeWorkAuthError, auth_headers, is_token_expired, refresh_account


def translate_model(model: str) -> str:
    inner = (model or "auto").strip() or "auto"
    return ALIASES.get(inner, inner)


def accepts_model(inner: str) -> bool:
    import catalog

    value = (inner or "").strip()
    if value in ALIASES:
        return True
    models = catalog.models_for(CHANNEL_ID, [{"id": item} for item in STATIC_MODELS])
    ids = {str(item.get("id")) for item in models if isinstance(item, dict)}
    return value in ids


def _last_user_text(payload: dict) -> str:
    for item in reversed(payload.get("messages") or []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    parts.append(str(part["text"]))
                elif isinstance(part, str):
                    parts.append(part)
            text = "".join(parts).strip()
            if text:
                return text
    return ""


def _walk_text(value, bucket: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                _walk_text(json.loads(text), bucket)
            except json.JSONDecodeError:
                pass
        return
    if isinstance(value, dict):
        kind = str(value.get("type") or "")
        if kind in {"status", "tool", "tool_call"}:
            return
        for key in ("text_content", "text", "markdown", "plain_text", "reasoning_content"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                bucket.append(item.strip())
        content = value.get("content")
        if isinstance(content, str) and content.strip():
            if content.startswith("{") or content.startswith("["):
                _walk_text(content, bucket)
            elif content.strip() not in bucket:
                bucket.append(content.strip())
        elif isinstance(content, (dict, list)):
            _walk_text(content, bucket)
        messages = value.get("messages")
        if isinstance(messages, list):
            _walk_text(messages, bucket)
        return
    if isinstance(value, list):
        for item in value:
            _walk_text(item, bucket)


def _text_from_event(event: str, payload: dict) -> str:
    if event in {
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
    }:
        return ""
    bucket: list[str] = []
    _walk_text(payload, bucket)
    return "\n".join(dict.fromkeys(bucket)).strip()


def extract_assistant_text(items: list) -> str:
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("role") not in {"assistant", "system"} and item.get("message_type") != "task":
            continue
        _walk_text(item.get("content"), chunks)
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in chunks:
        if chunk not in seen:
            seen.add(chunk)
            ordered.append(chunk)
    return "\n".join(ordered).strip()


def _openai_json(model: str, text: str, finish: str = "stop") -> dict:
    return {
        "id": f"traework-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
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


def _log(api_key_info, account, model_name, stream, finish, status, error, t0):
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
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "credit": 0,
                "finish_reason": finish,
                "duration_ms": int((time.time() - t0) * 1000),
                "status_code": status,
                "error_msg": error,
                "increment_usage": True,
            }
        )
    except Exception:
        pass


async def _turn(account: dict, prompt: str, model: str, timeout: float = 90.0) -> str:
    headers = auth_headers(account)
    session_url = f"{AGENT_API}{SESSIONS_PATH}"
    sid = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
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
            text = extract_assistant_text(items) or "\n".join(dict.fromkeys(pieces)).strip()
            if not text:
                raise TraeWorkAuthError("TraeWork turn finished without assistant text")
            return text
        finally:
            task.cancel()
            try:
                await client.delete(f"{session_url}/{sid}", headers=headers)
            except Exception:
                pass


async def chat_completions(payload: dict, api_key_info: dict | None) -> tuple:
    model = translate_model(str(payload.get("model") or "auto"))
    prompt = _last_user_text(payload)
    if not prompt:
        return (
            "error",
            (400, {"error": {"message": "messages must include a user turn", "type": "invalid_request_error"}}),
        )
    stream = bool(payload.get("stream"))
    tried: set[int] = set()
    last_error = None
    for _ in range(3):
        account = await _pick(tried)
        if not account:
            break
        tried.add(int(account["id"]))
        t0 = time.time()
        try:
            text = await _turn(account, prompt, model)
            auth_manager.mark_account_success(account["id"])
            _log(api_key_info, account, payload.get("model") or model, stream, "stop", 200, "", t0)
            if stream:
                return ("stream", _stream_once(text, str(payload.get("model") or model)))
            return ("json", _openai_json(str(payload.get("model") or model), text))
        except TraeWorkAuthError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
            _log(api_key_info, account, payload.get("model") or model, stream, "error", 503, str(exc)[:240], t0)
            continue
        except httpx.HTTPError as exc:
            auth_manager.mark_account_failure(account["id"], 503)
            last_error = ("error", (503, {"error": {"message": str(exc)[:240], "type": "server_error"}}))
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


async def _stream_once(text: str, model: str) -> AsyncGenerator[str, None]:
    chunk = {
        "id": f"traework-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    done = {
        "id": chunk["id"],
        "object": "chat.completion.chunk",
        "created": chunk["created"],
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def test_chat(account: dict, model: str = "qwen-3.7-plus", prompt: str = "请回复：pong") -> dict:
    t0 = time.time()
    try:
        text = await _turn(account, prompt or "请回复：pong", translate_model(model or "auto"), timeout=90.0)
    except TraeWorkAuthError as exc:
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
