"""FIFO pacing and actual in-flight limits for upstream chat, per account.

State belongs to this gateway process. Authentication, billing and check-in do
not consume chat slots. No lock or database connection is held while waiting.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import database as db
from streaming import ClosingStream


DEFAULT_SETTINGS = {
    "rate_limit_interval_seconds": 0.0,
    "rate_limit_max_concurrent": 0,
    "rate_limit_queue_timeout_seconds": 120.0,
}
_lock = threading.Lock()
_states: dict[int, "_State"] = {}
_MAX_WAITERS = 256


@dataclass
class _State:
    active: int = 0
    last_start: float = float("-inf")
    waiting: deque = field(default_factory=deque)


class RateLimitError(Exception):
    def payload(self) -> dict:
        return {"error": {"message": str(self), "type": "rate_limit_error",
                          "code": "local_rate_limit"}}


def _now() -> float:
    return time.monotonic()


def settings() -> dict:
    values = {}
    for key, default in DEFAULT_SETTINGS.items():
        try:
            value = float(db.get_setting(key, default))
            if not math.isfinite(value) or value < 0:
                value = default
        except (TypeError, ValueError, OverflowError):
            value = default
        values[key] = value
    values["rate_limit_interval_seconds"] = min(600.0, values["rate_limit_interval_seconds"])
    values["rate_limit_max_concurrent"] = min(100, int(values["rate_limit_max_concurrent"]))
    values["rate_limit_queue_timeout_seconds"] = max(1.0, min(600.0, values["rate_limit_queue_timeout_seconds"]))
    return values


@asynccontextmanager
async def account_slot(account: dict):
    """Hold a slot through the entire upstream response; release on cancellation.

    Polling keeps this safe across tasks/event loops and lets saved settings
    take effect on existing waiters too. Only admission updates last_start, so
    a cancelled waiter never reserves a future request or leaks concurrency.
    """
    aid = int(account.get("id") or 0)
    if not aid:
        yield
        return
    cfg = settings()
    token = object()
    started = _now()
    admitted = False
    waited = False
    with _lock:
        # Drop long-idle entries; 600s is the largest supported pacing gap.
        for key, old in list(_states.items()):
            if not old.active and not old.waiting and started - old.last_start > 600:
                del _states[key]
        state = _states.setdefault(aid, _State())
        if len(state.waiting) >= _MAX_WAITERS:
            raise RateLimitError("Local account rate-limit queue is full. Please retry later.")
        state.waiting.append(token)
    try:
        while True:
            now = _now()
            if now - started >= cfg["rate_limit_queue_timeout_seconds"]:
                raise RateLimitError("Timed out waiting for the local account rate limiter. Please retry later.")
            with _lock:
                interval = cfg["rate_limit_interval_seconds"]
                cap = cfg["rate_limit_max_concurrent"]
                gap = state.last_start + interval - now
                if state.waiting[0] is token and gap <= 0 and (not cap or state.active < cap):
                    state.waiting.popleft()
                    state.active += 1
                    state.last_start = now
                    admitted = True
                    break
            remaining = cfg["rate_limit_queue_timeout_seconds"] - (now - started)
            waited = True
            await asyncio.sleep(min(0.1, remaining))
            cfg = settings()
        if waited:
            import auth_manager

            if auth_manager.account_is_cooling_down(aid):
                raise RateLimitError("Account entered cooldown while queued. Please retry with an available account.")
        yield
    finally:
        with _lock:
            if admitted:
                state.active -= 1
            else:
                state.waiting.remove(token)


def reset() -> None:
    """Test hook; only call when no requests are running."""
    with _lock:
        _states.clear()


def guard_stream(stream):
    """Translate a lazy provider's queue error without losing stream ownership."""
    async def chunks():
        try:
            async for chunk in stream:
                yield chunk
        except RateLimitError as exc:
            yield ("data: " + json.dumps(exc.payload()) + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"

    return ClosingStream(chunks(), upstream=stream)
