"""Usage collection for channels forwarding OpenAI Chat Completions SSE."""

import json
import math


def credit_amount(usage: dict | None) -> float | None:
    """Read explicit request credits, never infer them from tokens or balances."""
    if not isinstance(usage, dict):
        return None
    for key in ("credit", "points_consumed"):
        value = usage.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            amount = float(value)
        except (ValueError, TypeError, OverflowError):
            continue
        if math.isfinite(amount) and amount >= 0:
            return amount
    return None


def event_usage(event: dict) -> dict:
    """Unwrap COSY's body envelope without treating balances as request usage."""
    usage = event.get("usage")
    result = dict(usage) if isinstance(usage, dict) else {}
    inner = event.get("body")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except (ValueError, TypeError):
            inner = None
    if isinstance(inner, dict):
        result.update(event_usage(inner))
    return result


def request_stream_usage(body: dict) -> None:
    if body.get("stream"):
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}


def token_counts(usage: dict) -> tuple[int, int, int]:
    def count(value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    prompt = count(usage.get("prompt_tokens"))
    completion = count(usage.get("completion_tokens"))
    return prompt, completion, count(usage.get("total_tokens")) or prompt + completion


class StreamUsage:
    def __init__(self):
        self.usage = {}
        self.finish = "stop"
        self.error = ""

    def observe(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        self.usage.update(event_usage(event))
        for choice in event.get("choices") or []:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
        if event.get("error"):
            self.error = str(event["error"])[:400]

    @property
    def counts(self):
        return token_counts(self.usage)
