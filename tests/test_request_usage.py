"""Request logs must retain upstream usage without estimating missing data."""

import asyncio
import importlib
import json

import httpx
import pytest

from providers.usage import StreamUsage, token_counts, credit_amount


def patch_channel(monkeypatch, channel, *, lines=(), response=None):
    chat = importlib.import_module(f"providers.{channel}.chat")
    logs = []
    requests = []
    account = {"id": 9, "name": channel, "access_token": "test"}

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"} if response is not None else {}

        async def aread(self):
            return b""

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
    if channel == "loomy":
        assert requests[0]["stream"] is True
        assert requests[0]["stream_options"]["include_usage"] is True
    else:
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


@pytest.mark.parametrize("channel", ["loomy", "xiaohuanxiong"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("amount", [0, 1.25, None])
def test_request_credit_is_retained_and_missing_is_unknown(monkeypatch, channel, stream, amount):
    usage = {"prompt_tokens": 12, "completion_tokens": 3}
    if amount is not None:
        usage["points_consumed"] = amount
    chat, logs, _ = patch_channel(monkeypatch, channel, response={
        "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}], "usage": usage,
    }, lines=[
        'data: {"choices":[{"delta":{"content":"pong"}}]}',
        'data: ' + json.dumps({"choices": [], "usage": usage}),
        'data: {"usage":{"total_tokens":15}}',
        'data: [DONE]',
    ])

    async def run():
        kind, result = await chat.chat_completions({"model": "auto", "stream": stream}, None)
        if kind == "stream":
            _ = [chunk async for chunk in result]

    asyncio.run(run())
    assert len(logs) == 1
    assert logs[0]["credit"] == amount


@pytest.mark.parametrize("value", [None, True, -1, "NaN", float("inf"), "invalid", {}, []])
def test_invalid_credits_are_not_recorded_as_spending(value):
    assert credit_amount({"points_consumed": value}) is None


def test_credit_parsing_does_not_confuse_balances_or_token_counts_with_spend():
    assert credit_amount({"balance": 100, "total_tokens": 400, "cost": 0.01}) is None
    assert credit_amount({"credit": 0, "points_consumed": 4}) == 0
    assert credit_amount({"points_consumed": "1.25"}) == 1.25


def test_qwenwork_partial_usage_keeps_credits(monkeypatch):
    from providers.qwenwork import chat
    logs = []
    monkeypatch.setattr(chat.db, "record_request", logs.append)
    result = chat._aggregate([
        {"usage": {"credit": 2.5}},
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    ], "auto")
    chat._log(None, {"id": 1}, "auto", False, 10, 2, 12, "stop", 200, "", 0, usage=result["usage"])
    assert logs[0]["credit"] == 2.5


def test_traework_usage_retains_explicit_request_credit(monkeypatch):
    from providers.traework import chat
    logs = []
    monkeypatch.setattr(chat.db, "record_request", logs.append)
    usage = chat._parse_usage('{"prompt_tokens":10,"completion_tokens":2,"credit":0.25}')
    chat._log(None, {"id": 1}, "auto", False, "stop", 200, "", 0, usage)
    assert logs[0]["credit"] == 0.25
    assert chat._parse_usage({"credit": 0})["credit"] == 0


def test_responses_conversion_preserves_explicit_credit_usage():
    import responses
    usage = responses._responses_usage({"prompt_tokens": 10, "completion_tokens": 2, "points_consumed": 1.5})
    assert usage["input_tokens"] == 10
    assert usage["points_consumed"] == 1.5
    assert "credit" not in usage


@pytest.mark.parametrize("channel", ["loomy", "xiaohuanxiong", "qwenwork"])
def test_partial_credit_survives_stream_read_failure(monkeypatch, channel):
    chat, logs, _ = patch_channel(monkeypatch, channel, lines=[
        'data: {"choices":[{"delta":{"content":"OK"}}],"usage":{"prompt_tokens":12,"points_consumed":1.25}}',
        httpx.ReadError("connection lost"),
    ])

    async def run():
        if channel == "qwenwork":
            async def pick(_tried):
                return {"id": 9, "name": "qwenwork", "access_token": "test"}
            monkeypatch.setattr(chat, "_pick", pick)
            monkeypatch.setattr(chat, "_packed_request", lambda *a: ("https://upstream.test", {}, "{}"))
            stream = chat._stream("{}", "test", "auto", None, "auto")
        else:
            _, stream = await chat.chat_completions({"model": "auto", "stream": True}, None)
        _ = [part async for part in stream]

    asyncio.run(run())
    assert len(logs) == 1
    assert logs[0]["credit"] == 1.25
    assert logs[0]["finish_reason"] == "error"


@pytest.mark.parametrize("channel", ["loomy", "xiaohuanxiong", "qwenwork"])
def test_responses_style_close_at_done_keeps_successful_credit_log(monkeypatch, channel):
    chat, logs, _ = patch_channel(monkeypatch, channel, lines=[
        'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}],"usage":{"points_consumed":1.25}}',
        'data: [DONE]',
    ])

    async def run():
        if channel == "qwenwork":
            async def pick(_tried):
                return {"id": 9, "name": channel, "access_token": "test"}
            monkeypatch.setattr(chat, "_pick", pick)
            monkeypatch.setattr(chat, "_packed_request", lambda *a: ("https://upstream.test", {}, "{}"))
            stream = chat._stream("{}", "test", "auto", None, "auto")
        else:
            _, stream = await chat.chat_completions({"model": "auto", "stream": True}, None)
        try:
            async for part in stream:
                if b"data: [DONE]" in part:
                    break
        finally:
            await stream.aclose()

    asyncio.run(run())
    assert len(logs) == 1
    assert logs[0]["credit"] == 1.25
    assert logs[0]["status_code"] == 200
    assert logs[0]["finish_reason"] == "stop"


def test_traework_close_at_done_retains_usage_log(monkeypatch):
    from providers.traework import chat
    logs = []

    async def pick(_tried):
        return {"id": 9, "name": "traework"}

    async def events(*args):
        yield {"kind": "usage", "usage": {"credit": 0.25}}
        yield {"kind": "done", "finish_reason": "stop"}

    monkeypatch.setattr(chat, "_pick", pick)
    monkeypatch.setattr(chat, "iter_llm_events", events)
    monkeypatch.setattr(chat.auth_manager, "mark_account_success", lambda *a: None)
    monkeypatch.setattr(chat.db, "record_request", logs.append)

    async def run():
        _, stream = await chat.chat_completions({"model": "auto", "stream": True}, None)
        try:
            async for part in stream:
                if b"data: [DONE]" in part:
                    break
        finally:
            await stream.aclose()

    asyncio.run(run())
    assert len(logs) == 1
    assert logs[0]["credit"] == 0.25
    assert logs[0]["status_code"] == 200
