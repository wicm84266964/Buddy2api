"""Fill gaps in provider logs at the client API boundary."""

from contextvars import ContextVar
import json
import logging
import time


current_request = ContextVar("request_log_state", default=None)
logger = logging.getLogger(__name__)


def bind_request(api_key_info, payload):
    state = current_request.get()
    if state is not None:
        state["key"] = api_key_info or {}
        state["model"] = payload.get("model") or "auto"
        state["stream"] = bool(payload.get("stream"))


def note_record(data):
    state = current_request.get()
    if state is not None:
        state["last"] = dict(data)


def bind_dispatch(channel, account, resolved_model):
    state = current_request.get()
    if state is not None:
        state["last"] = None
        state["dispatch"] = {
            "provider": channel, "resolved_model": resolved_model,
            "account_id": account.get("id") if account else None,
            "account_name": account.get("name") if account else None,
        }


class FailureLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in {
            "/v1/chat/completions", "/v1/responses",
        }:
            return await self.app(scope, receive, send)
        state = {"last": None}
        token = current_request.set(state)
        started = time.monotonic()
        status = 200
        failure = ""
        is_sse = False
        pending = b""
        error_body = bytearray()

        async def observed_send(message):
            nonlocal status, failure, is_sse, pending
            if message["type"] == "http.response.start":
                status = message["status"]
                is_sse = any(k.lower() == b"content-type" and b"text/event-stream" in v
                             for k, v in message.get("headers", []))
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if status >= 400:
                    error_body.extend(body[:max(0, 4096 - len(error_body))])
                if is_sse:
                    pending += body
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if not line.startswith(b"data:"):
                            continue
                        try:
                            event = json.loads(line[5:])
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        response = event.get("response")
                        error = event.get("error")
                        if isinstance(response, dict) and response.get("status") == "failed":
                            error = response.get("error") or "response.failed"
                        if event.get("type") == "error":
                            error = error or event.get("message") or "stream error"
                        if error:
                            failure = json.dumps(error, ensure_ascii=False)[:1000]
                    # Bound memory for malformed streams without line breaks.
                    if len(pending) > 65536:
                        pending = b""
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception as exc:
            status = 500
            failure = f"gateway exception: {type(exc).__name__}: {exc}"[:1000]
            raise
        finally:
            try:
                if "key" in state and (status >= 400 or failure):
                    last = state["last"] or {}
                    # Providers own attempt logs. Only fill an absent terminal
                    # failure, including a bridge failure after a success row.
                    if not (last.get("finish_reason") != "retry" and
                            (last.get("status_code", 200) >= 400 or last.get("finish_reason") == "error")):
                        import database as db

                        key = state["key"]
                        target = {**state.get("dispatch", {}), **last}
                        db.record_request({
                            "api_key_id": key.get("id"), "api_key_name": key.get("name"),
                            "account_id": target.get("account_id"), "account_name": target.get("account_name"),
                            "provider": target.get("provider") or key.get("default_channel") or "workbuddy",
                            "model": state["model"], "resolved_model": target.get("resolved_model"),
                            "stream": int(state["stream"]), "finish_reason": "error",
                            "status_code": status if status >= 400 else 502,
                            "error_msg": "[gateway] " + (failure or error_body.decode("utf-8", "replace"))[:1000],
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "increment_usage": False,
                        })
            except Exception:
                logger.exception("Failed to record gateway request failure")
            finally:
                current_request.reset(token)
