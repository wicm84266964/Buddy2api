"""Usage collection for channels forwarding OpenAI Chat Completions SSE."""

import json


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
        if isinstance(event.get("usage"), dict):
            self.usage.update(event["usage"])
        for choice in event.get("choices") or []:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                self.finish = choice["finish_reason"]
        if event.get("error"):
            self.error = str(event["error"])[:400]

    @property
    def counts(self):
        return token_counts(self.usage)
