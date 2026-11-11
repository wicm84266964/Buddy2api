"""Request logs must retain upstream usage without estimating missing data."""

import asyncio
import importlib
import json

import httpx
import pytest

from providers.usage import StreamUsage, token_counts


def patch_channel(monkeypatch, channel, *, lines=(), response=None):
    chat = importlib.import_module(f"providers.{channel}.chat")
    logs = []
    requests = []
    account = {"id": 9, "name": channel, "access_token": "test"}

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            for line in lines:
                if isinstance(line, Exception):
                    raise line
                yield line

        def json(self):
            return response

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **kwargs):
            requests.append(json.loads(kwargs["content"]))
            return Response()

        async def post(self, *_args, **kwargs):
            requests.append(json.loads(kwargs["content"]))
            return Response()

    async def ready(account):
        return account

    monkeypatch.setattr(chat.httpx, "AsyncClient", Client)
    monkeypatch.setattr(chat.auth_manager, "pick_account", lambda *_args, **_kwargs: account)
    monkeypatch.setattr(chat.auth_manager, "mark_account_success", lambda *_args: None)
    monkeypatch.setattr(chat.auth_manager, "mark_account_failure", lambda *_args: None)
    monkeypatch.setattr(chat.auth_manager, "account_attempt_limit", lambda *_args: 1)
    monkeypatch.setattr(chat.db, "record_request", lambda data: logs.append(data))
    if hasattr(chat, "_ready"):
        monkeypatch.setattr(chat, "_ready", ready)
    return chat, logs, requests


@pytest.mark.parametrize("channel", ["loomy", "qclaw", "xiaohuanxiong"])
def test_stream_logs_usage_and_requests_usage(monkeypatch, channel):
    lines = [
        'data: {"choices":[{"delta":{"content":"pong"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":12}}',
        'data: {"choices":[],"usage":{"completion_tokens":3}}',
        'data: {"choices":[],"usage":{}}',
        'data: [DONE]',
    ]
    chat, logs, requests = patch_channel(monkeypatch, channel, lines=lines)

    async def run():
        kind, stream = await chat.chat_completions({
            "model": "auto", "stream": True,
            "stream_options": {"include_usage": False},
            "messages": [{"role": "user", "content": "ping"}],
        }, {"id": 4, "name": "key"})
        return kind, b"".join([chunk async for chunk in stream])

    kind, output = asyncio.run(run())
    assert kind == "stream"
    assert "pong" in output.decode()
    assert requests[0]["stream_options"]["include_usage"] is True
    assert len(logs) == 1
    assert (logs[0]["prompt_tokens"], logs[0]["completion_tokens"], logs[0]["total_tokens"]) == (12, 3, 15)
    assert logs[0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("channel", ["loomy", "qclaw", "xiaohuanxiong"])
def test_nonstream_log_derives_missing_total(monkeypatch, channel):
    chat, logs, requests = patch_channel(monkeypatch, channel, response={
        "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    })
    asyncio.run(chat.chat_completions({"model": "auto", "stream": False}, None))
    assert logs[0]["total_tokens"] == 15
    assert "stream_options" not in requests[0]


def test_usage_parser_does_not_invent_missing_tokens():
    observed = StreamUsage()
    for line in [": ping", "data: [DONE]", "data: broken", "data: []", "data: null"]:
        observed.observe(line)
    assert observed.counts == (0, 0, 0)
    assert token_counts({"prompt_tokens": "12", "completion_tokens": "3"}) == (12, 3, 15)


@pytest.mark.parametrize("interrupted", [False, True])
def test_loomy_logs_partial_usage_on_disconnect(monkeypatch, interrupted):
    chat, logs, _ = patch_channel(monkeypatch, "loomy", lines=[
        'data: {"choices":[],"usage":{"prompt_tokens":12}}',
        httpx.ReadError("upstream disconnected"),
    ])

    async def run():
        _, stream = await chat.chat_completions({"model": "auto", "stream": True}, None)
        if interrupted:
            await anext(stream)
            await stream.aclose()
        else:
            _ = [chunk async for chunk in stream]

    asyncio.run(run())
    assert len(logs) == 1
    assert logs[0]["total_tokens"] == 12
    assert logs[0]["finish_reason"] == "error"
    assert logs[0]["status_code"] == 502


def test_loomy_logs_sse_error_as_failure(monkeypatch):
    chat, logs, _ = patch_channel(monkeypatch, "loomy", lines=[
        'data: {"error":{"message":"quota exhausted"}}',
    ])

    async def run():
        _, stream = await chat.chat_completions({"model": "auto", "stream": True}, None)
        return [chunk async for chunk in stream]

    asyncio.run(run())
    assert logs[0]["status_code"] == 502
    assert "quota exhausted" in logs[0]["error_msg"]
