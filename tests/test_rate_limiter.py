import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import auth_manager
import context_budget
import database as db
import rate_limiter
import router
import server


@pytest.fixture
def config(monkeypatch):
    values = dict(rate_limiter.DEFAULT_SETTINGS)
    monkeypatch.setattr(rate_limiter, "settings", lambda: dict(values))
    return values


def test_fifo_concurrency_and_accounts_are_independent(config):
    config["rate_limit_max_concurrent"] = 1

    async def main():
        seen = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        async def first():
            async with rate_limiter.account_slot({"id": 1}):
                first_entered.set()
                await release_first.wait()

        async def queued(n):
            async with rate_limiter.account_slot({"id": 1}):
                seen.append(n)
                assert rate_limiter._states[1].active == 1
                await asyncio.sleep(0)

        running = asyncio.create_task(first())
        await first_entered.wait()
        tasks = [asyncio.create_task(queued(n)) for n in range(3)]
        await asyncio.sleep(0)
        assert len(rate_limiter._states[1].waiting) == 3
        async with rate_limiter.account_slot({"id": 2}):
            assert seen == []
        release_first.set()
        await asyncio.gather(running, *tasks)
        assert seen == [0, 1, 2]
        assert rate_limiter._states[1].active == 0
        assert not rate_limiter._states[1].waiting

    asyncio.run(main())


def test_interval_and_live_disabling(config, monkeypatch):
    config["rate_limit_interval_seconds"] = 10
    now = [100.0]
    monkeypatch.setattr(rate_limiter, "_now", lambda: now[0])

    async def main():
        async with rate_limiter.account_slot({"id": 1}):
            pass
        entered = []

        async def second():
            async with rate_limiter.account_slot({"id": 1}):
                entered.append(now[0])

        task = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert not entered
        now[0] += 9
        await asyncio.sleep(0.12)
        assert not entered
        now[0] += 1
        await asyncio.wait_for(task, 1)
        assert entered == [110.0]
        # Disabling the interval wakes an already queued request.
        task = asyncio.create_task(second())
        await asyncio.sleep(0)
        assert len(entered) == 1
        config["rate_limit_interval_seconds"] = 0
        await asyncio.wait_for(task, 1)
        assert entered == [110.0, 110.0]

    asyncio.run(main())


def test_cancelled_waiter_and_active_stream_release_slots(config):
    config["rate_limit_max_concurrent"] = 1

    async def main():
        async def waiting():
            async with rate_limiter.account_slot({"id": 1}):
                pytest.fail("must remain queued")

        async with rate_limiter.account_slot({"id": 1}):
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not rate_limiter._states[1].waiting
            assert rate_limiter._states[1].active == 1

        async def source():
            async with rate_limiter.account_slot({"id": 1}):
                yield b"first"
                await asyncio.Event().wait()

        stream = rate_limiter.guard_stream(source())
        assert await anext(stream) == b"first"
        assert rate_limiter._states[1].active == 1
        # Closing from another task is supported (as in a client disconnect).
        await asyncio.create_task(stream.aclose())
        assert rate_limiter._states[1].active == 0
        async with rate_limiter.account_slot({"id": 1}):
            assert rate_limiter._states[1].active == 1

    asyncio.run(main())


def test_timeout_and_queue_capacity_do_not_leak_or_cool_account(config, monkeypatch):
    config["rate_limit_max_concurrent"] = 1
    now = [100.0]
    monkeypatch.setattr(rate_limiter, "_now", lambda: now[0])

    async def waiting():
        async with rate_limiter.account_slot({"id": 1}):
            pytest.fail("must remain queued")

    async def main():
        async with rate_limiter.account_slot({"id": 1}):
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            now[0] += 121
            with pytest.raises(rate_limiter.RateLimitError, match="Timed out"):
                await task
            assert not rate_limiter._states[1].waiting
            assert rate_limiter._states[1].active == 1
            monkeypatch.setattr(rate_limiter, "_MAX_WAITERS", 0)
            with pytest.raises(rate_limiter.RateLimitError, match="full"):
                async with rate_limiter.account_slot({"id": 1}):
                    pytest.fail("queue is full")
        assert rate_limiter._states[1].active == 0
        assert not auth_manager.account_is_cooling_down(1)

    asyncio.run(main())


def test_defaults_do_not_limit_overlapping_requests(config):
    async def main():
        async with rate_limiter.account_slot({"id": 1}):
            async with rate_limiter.account_slot({"id": 1}):
                assert rate_limiter._states[1].active == 2
    asyncio.run(main())


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cross_channel", [False, True])
def test_router_reports_local_queue_timeout_as_429(monkeypatch, streaming, cross_channel):
    async def failed_stream():
        raise rate_limiter.RateLimitError("local queue timeout")
        yield

    async def chat(payload, info):
        if streaming:
            return ("stream", failed_stream())
        raise rate_limiter.RateLimitError("local queue timeout")

    provider = SimpleNamespace(translate_model=lambda m: m, list_models=lambda: [], chat_completions=chat)
    monkeypatch.setattr(router.providers, "get_provider", lambda channel: provider)
    monkeypatch.setattr(router, "_cross_channel_failover_enabled", lambda: cross_channel)

    async def main():
        result = await router._walk_account_queue(
            "workbuddy/auto", {"model": "auto", "stream": streaming}, {},
            [("workbuddy", {"id": 1, "provider": "workbuddy"})])
        if result[0] == "stream":
            chunks = [c async for c in result[1]]
            error = json.loads(chunks[0].decode().removeprefix("data: "))["error"]
            assert chunks[-1] == b"data: [DONE]\n\n"
        else:
            assert result[0] == "error"
            assert result[1][0] == 429
            error = result[1][1]["error"]
        assert error["code"] == "local_rate_limit"
        assert not auth_manager.account_is_cooling_down(1)

    asyncio.run(main())


def test_context_retry_releases_original_and_replacement_slots(config):
    config["rate_limit_max_concurrent"] = 1
    attempts = []

    async def source(retry):
        async with rate_limiter.account_slot({"id": 1}):
            if retry:
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                await asyncio.Event().wait()
            else:
                yield b'data: {"error":{"code":11115,"message":"context length exceeded"}}\n\n'

    async def chat(payload, info):
        retry = info.get("_context_retried", False)
        attempts.append(retry)
        return ("stream", source(retry))

    async def main():
        payload = {"model": "auto", "messages": [
            {"role": "user", "content": "old" * 100},
            {"role": "assistant", "content": "answer" * 100},
            {"role": "user", "content": "latest"},
        ]}
        result = await context_budget.dispatch_with_context_policy(
            SimpleNamespace(chat_completions=chat), "workbuddy", payload, {})
        stream = result[1]
        assert b"ok" in await asyncio.wait_for(anext(stream), 1)
        assert attempts == [False, True]
        assert rate_limiter._states[1].active == 1
        await stream.aclose()
        assert rate_limiter._states[1].active == 0

    asyncio.run(main())


class Request:
    def __init__(self, data):
        self.data = data

    async def stream(self):
        yield json.dumps(self.data).encode()


@pytest.mark.parametrize("key,value", [
    ("rate_limit_interval_seconds", -1), ("rate_limit_interval_seconds", 601),
    ("rate_limit_interval_seconds", "NaN"), ("rate_limit_interval_seconds", True),
    ("rate_limit_interval_seconds", float("inf")),
    ("rate_limit_interval_seconds", 10 ** 400),
    ("rate_limit_max_concurrent", 1.5), ("rate_limit_max_concurrent", 101),
    ("rate_limit_queue_timeout_seconds", 0), ("rate_limit_queue_timeout_seconds", None),
])
def test_settings_reject_invalid_limits_without_partial_writes(monkeypatch, key, value):
    writes = []
    monkeypatch.setattr(server, "_check_admin", lambda _: None)
    monkeypatch.setattr(db, "set_setting", lambda *args: writes.append(args))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.admin_update_settings(Request({"rate_limit_max_concurrent": 2, key: value})))
    assert exc.value.status_code == 400
    assert writes == []


def test_saved_settings_roundtrip(monkeypatch, real_rate_limit_settings):
    saved = {}
    monkeypatch.setattr(server, "_check_admin", lambda _: None)
    monkeypatch.setattr(db, "set_setting", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(db, "get_all_settings", lambda: dict(saved))
    monkeypatch.setattr(db, "get_setting", lambda k, d=None: saved.get(k, d))
    assert asyncio.run(server.admin_get_settings())["rate_limit_interval_seconds"] == 0
    values = {"rate_limit_interval_seconds": 5.5, "rate_limit_max_concurrent": 1,
              "rate_limit_queue_timeout_seconds": 60}
    asyncio.run(server.admin_update_settings(Request(values)))
    assert asyncio.run(server.admin_get_settings()) == values
    assert real_rate_limit_settings() == values


@pytest.mark.parametrize("streaming", [False, True])
def test_responses_maps_local_limit_to_supported_error_code(streaming):
    import responses

    async def failing():
        raise rate_limiter.RateLimitError("local queue timeout")
        yield

    async def handler(payload, info):
        if streaming:
            return ("stream", rate_limiter.guard_stream(failing()))
        return ("error", (429, rate_limiter.RateLimitError("local queue timeout").payload()))

    async def main():
        result = await responses.proxy_responses(
            {"model": "auto", "input": "hi", "stream": streaming}, {}, chat_handler=handler)
        if streaming:
            events = [json.loads(line[6:]) async for chunk in result[1]
                      for line in chunk.splitlines() if line.startswith("data: ")]
            failed = next(e for e in events if e.get("type") == "response.failed")
            assert failed["response"]["error"]["code"] == "rate_limit_exceeded"
        else:
            assert result[1][0] == 429
            assert result[1][1]["error"]["code"] == "rate_limit_exceeded"
    asyncio.run(main())


def test_queued_request_does_not_retry_an_account_that_just_hit_quota(config):
    config["rate_limit_max_concurrent"] = 1

    async def queued():
        async with rate_limiter.account_slot({"id": 1}):
            pytest.fail("cooled-down account must not receive another request")

    async def main():
        async with rate_limiter.account_slot({"id": 1}):
            task = asyncio.create_task(queued())
            await asyncio.sleep(0)
            auth_manager.mark_account_failure(1, 429, cooldown_hint=900)
        with pytest.raises(rate_limiter.RateLimitError, match="cooldown"):
            await task
        assert rate_limiter._states[1].active == 0
    asyncio.run(main())


@pytest.mark.parametrize("streaming", [False, True])
def test_real_http_stream_cancel_releases_account_to_next_request(config, monkeypatch, streaming):
    import httpx
    import proxy

    config["rate_limit_max_concurrent"] = 1
    account = {"id": 1, "provider": "workbuddy", "name": "test"}

    async def pick(*args, **kwargs):
        return account

    async def headers(*args):
        return {"Authorization": "Bearer test-only"}

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", headers)
    monkeypatch.setattr(auth_manager, "backend_url", lambda *a: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda default: default)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda *a: None)
    monkeypatch.setattr(db, "list_accounts", lambda **kw: [account])
    monkeypatch.setattr(proxy, "_log_request", lambda *a, **kw: None)
    client_type = httpx.AsyncClient

    async def main():
        requests = []
        closed = []
        first_started = asyncio.Event()
        hold_first = asyncio.Event()

        class Body(httpx.AsyncByteStream):
            def __init__(self, n):
                self.n = n

            async def __aiter__(self):
                yield b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
                if self.n == 1:
                    first_started.set()
                    await hold_first.wait()
                yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

            async def aclose(self):
                closed.append(self.n)

        async def handle(request):
            requests.append(request)
            return httpx.Response(200, stream=Body(len(requests)))

        monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda *a, **kw: client_type(
            *a, **kw, transport=httpx.MockTransport(handle)))

        async def call():
            result = await proxy.proxy_chat_completions(
                {"model": "auto", "messages": [{"role": "user", "content": "hi"}], "stream": streaming})
            if result[0] == "stream":
                return b"".join([c async for c in result[1]])
            assert result[0] == "json"
            return result[1]

        first = asyncio.create_task(call())
        await asyncio.wait_for(first_started.wait(), 2)
        second = asyncio.create_task(call())
        await asyncio.sleep(0)
        assert len(requests) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        result = await asyncio.wait_for(second, 2)
        assert len(requests) == 2
        assert set(closed) == {1, 2}
        assert rate_limiter._states[1].active == 0
        if streaming:
            assert b"[DONE]" in result and b'"error"' not in result
        else:
            assert result["choices"][0]["message"]["content"] == "ok"
    asyncio.run(main())


@pytest.mark.parametrize("consume", ["unstarted", "partial", "complete"])
def test_traework_releases_peeked_stream_slot(config, monkeypatch, consume):
    import providers.traework.chat as chat
    config["rate_limit_max_concurrent"] = 1
    closed = []

    async def pick(*args):
        return {"id": 1, "provider": "traework"}

    async def events(account, *args):
        async with rate_limiter.account_slot(account):
            try:
                yield {"kind": "output", "text": "ok", "reasoning": "", "tool_calls": []}
                yield {"kind": "done", "finish_reason": "stop"}
                await asyncio.Event().wait()
            finally:
                closed.append(True)

    monkeypatch.setattr(chat, "_pick", pick)
    monkeypatch.setattr(chat, "iter_llm_events", events)
    monkeypatch.setattr(chat, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(auth_manager, "account_attempt_limit", lambda *a, **kw: 3)

    async def main():
        result = await chat.chat_completions({"model": "auto", "stream": True, "messages": []}, {})
        stream = result[1]
        assert rate_limiter._states[1].active == 1
        if consume == "partial":
            assert b"ok" in await anext(stream)
        elif consume == "complete":
            chunks = [c async for c in stream]
            assert chunks[-1] == b"data: [DONE]\n\n"
            assert rate_limiter._states[1].active == 0
        await stream.aclose()
        assert rate_limiter._states[1].active == 0
        assert closed == [True]

    asyncio.run(main())
