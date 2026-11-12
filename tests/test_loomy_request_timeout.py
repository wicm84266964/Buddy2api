import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from providers.loomy import chat


@pytest.fixture
def request_env(monkeypatch):
    account = {"id": 13, "access_token": "test-session"}
    state = {"timeouts": [], "failures": [], "logs": [], "error": None, "settings": {}, "response": None}

    async def ready(value):
        return value

    @asynccontextmanager
    async def slot(value):
        yield

    class Client:
        def __init__(self, **kwargs):
            state["timeouts"].append(kwargs["timeout"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            if state["error"]:
                raise state["error"]
            if state["response"] is not None:
                return state["response"]
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            yield await self.post(*args, **kwargs)

    monkeypatch.setattr(chat, "_ready", ready)
    monkeypatch.setattr(chat, "_body", lambda body: (body, "{}"))
    monkeypatch.setattr(chat.rate_limiter, "account_slot", slot)
    monkeypatch.setattr(chat.httpx, "AsyncClient", Client)
    monkeypatch.setattr(chat.auth_manager, "pick_account", lambda tried, **kw: account if 13 not in tried else None)
    monkeypatch.setattr(chat.auth_manager, "account_attempt_limit", lambda *a: 1)
    monkeypatch.setattr(chat.auth_manager, "mark_account_success", lambda *a: None)
    monkeypatch.setattr(chat.auth_manager, "mark_account_failure", lambda *a: state["failures"].append(a))
    monkeypatch.setattr(chat.db, "get_setting", lambda key, default=None: state["settings"].get(key, default))
    monkeypatch.setattr(chat, "_log", lambda *a, **kw: state["logs"].append(a))
    state["account"] = account
    return state


@pytest.mark.parametrize("configured,expected", [(None, 300), (480, 480), (900, 600)])
def test_nonstream_and_account_probe_use_configured_timeout(request_env, configured, expected):
    if configured is not None:
        request_env["settings"]["timeout"] = configured
    kind, _ = asyncio.run(chat.chat_completions({"model": "qwen3.8-flash"}, None))
    probe = asyncio.run(chat.test_chat(request_env["account"]))
    assert kind == "json"
    assert probe["ok"]
    assert request_env["timeouts"] == [expected, expected]


def test_read_timeout_reports_cause_without_cooling_down_account(request_env):
    request_env["settings"]["timeout"] = 480
    request_env["error"] = httpx.ReadTimeout("")
    kind, (status, body) = asyncio.run(chat.chat_completions({"model": "qwen3.8-flash"}, None))
    assert kind == "error"
    assert status == 504
    assert body["error"]["type"] == "upstream_timeout"
    assert "480s" in body["error"]["message"]
    assert "ReadTimeout" in body["error"]["message"]
    assert request_env["failures"] == []
    log = request_env["logs"][-1]
    assert log[8] == 504
    assert log[9] == body["error"]["message"]
    request_env["error"] = None
    assert asyncio.run(chat.chat_completions({"model": "qwen3.8-flash"}, None))[0] == "json"


def test_connection_failure_still_cools_down_account_and_logs_error_class(request_env):
    request_env["error"] = httpx.ConnectError("")
    _, (status, body) = asyncio.run(chat.chat_completions({"model": "qwen3.8-flash"}, None))
    assert status == 503
    assert "ConnectError" in body["error"]["message"]
    assert request_env["failures"] == [(13, 503)]


def test_upstream_504_does_not_block_next_request(request_env):
    request_env["response"] = httpx.Response(504, text="<html>504 Gateway Time-out openresty</html>")
    _, (status, body) = asyncio.run(chat.chat_completions({"model": "qwen3.8-flash"}, None))
    assert status == 504
    assert body["error"]["type"] == "upstream_timeout"
    assert "<html>" not in body["error"]["message"]
    assert request_env["failures"] == []
    request_env["response"] = None
    assert asyncio.run(chat.chat_completions({"model": "qwen3.8-flash"}, None))[0] == "json"


def test_nonstream_collects_sse_reasoning_tools_and_split_usage():
    import json
    events = [
        {"id": "compaction", "model": "qwen3.8-flash", "choices": [{"index": 0, "delta": {"reasoning_content": "think", "content": "sum"}}]},
        {"choices": [{"index": 0, "delta": {"content": "mary", "tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "look", "arguments": "{"}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"name": "up", "arguments": "}"}}]}, "finish_reason": "tool_calls"}], "usage": {"points_consumed": 2}},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
    ]
    wire = "\n\n".join("data: " + json.dumps(e) for e in events) + "\n\ndata: [DONE]\n\n"
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=wire, headers={"content-type": "text/event-stream"}))) as client:
            return await chat._post_collected(client, {"access_token": "test"}, "{}")
    data = asyncio.run(run()).json()
    message = data["choices"][0]["message"]
    assert data["object"] == "chat.completion"
    assert message["content"] == "summary"
    assert message["reasoning_content"] == "think"
    assert message["tool_calls"][0]["function"] == {"name": "lookup", "arguments": "{}"}
    assert data["usage"]["points_consumed"] == 2
    assert data["usage"]["total_tokens"] == 120


@pytest.mark.parametrize("wire", [
    'data: {"choices":[{"delta":{"content":"unfinished"}}]}\n\n',
    'data: invalid-json\n\n',
])
def test_incomplete_compaction_stream_is_not_returned_as_success(wire):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=wire))) as client:
            return await chat._post_collected(client, {"access_token": "test"}, "{}")
    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(run())

@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('failure', [502, 'disconnect'])
def test_proxy_retry_recovers_without_account_cooldown(request_env, monkeypatch, stream, failure):
    attempts = []
    logs = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            attempts.append(kwargs['content'])
            if len(attempts) == 1:
                if failure == 'disconnect':
                    raise httpx.RemoteProtocolError('no response headers')
                yield httpx.Response(502, text='<html>502 xunfei</html>')
            else:
                yield httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=(
                    'data: {"choices":[{"index":0,"delta":{"content":"OK"},"finish_reason":"stop"}],'
                    '"usage":{"prompt_tokens":7,"completion_tokens":1,"total_tokens":8,"points_consumed":2}}\n\n'
                    'data: [DONE]\n\n'
                ))

    async def sleep(delay):
        pass

    monkeypatch.setattr(chat.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(chat.asyncio, 'sleep', sleep)
    monkeypatch.setattr(chat, '_log', lambda *a, **kw: logs.append((a, kw)))

    async def run():
        kind, result = await chat.chat_completions({'model': 'qwen3.8-flash', 'stream': stream}, None)
        if stream:
            text = b''.join([chunk async for chunk in result]).decode()
            assert text.count('OK') == 1
            assert '[DONE]' in text
        else:
            assert kind == 'json'
            assert result['choices'][0]['message']['content'] == 'OK'

    asyncio.run(run())
    assert len(attempts) == 2
    assert request_env['failures'] == []
    assert len(logs) == 2
    assert logs[0][0][7] == 'retry'
    assert logs[0][1]['increment_usage'] is False
    assert logs[1][0][8] == 200
    assert logs[1][0][4:7] == (7, 1, 8)
    assert logs[1][1]['usage']['points_consumed'] == 2
