import asyncio
import json

import anyio
import httpx
import pytest
from starlette.requests import ClientDisconnect

import auth_manager
import responses
import router
from streaming import ClosingStreamingResponse


ACCOUNT = {"id": 77, "provider": "workbuddy"}


def test_replay_restores_consumer_pin_and_supports_other_task_close():
    async def main():
        seen = []

        async def source():
            try:
                for _ in range(2):
                    seen.append(auth_manager.pinned_for("workbuddy"))
                    yield b"x"
            finally:
                seen.append(auth_manager.pinned_for("workbuddy"))

        outer = {"id": 88, "provider": "workbuddy"}
        with auth_manager.pinned_account(outer):
            stream = router._replay([], source(), ACCOUNT)
            assert await anext(stream) == b"x"
            assert auth_manager.pinned_for("workbuddy") == outer
            assert await asyncio.create_task(anext(stream)) == b"x"
            await asyncio.create_task(stream.aclose())
            assert auth_manager.pinned_for("workbuddy") == outer
        assert seen == [ACCOUNT, ACCOUNT, ACCOUNT]

    asyncio.run(main())


@pytest.mark.parametrize("start_wrapper", [False, True])
def test_model_wrapper_closes_peeked_upstream(start_wrapper):
    async def main():
        closed = []

        async def source():
            try:
                yield b'data: {"model":"backend"}\n\n'
                yield b"tail"
            finally:
                closed.append(True)

        upstream = source()
        first = await anext(upstream)
        wrapped = router._rewrite_stream_model(router._replay([first], upstream, ACCOUNT), "alias")
        if start_wrapper:
            assert b'"model":"alias"' in await anext(wrapped)
        await wrapped.aclose()
        assert closed == [True]
        assert auth_manager.pinned_for("workbuddy") is None

    asyncio.run(main())


@pytest.mark.parametrize("tail", [b"data: [DONE]\n\n", b"data: invalid\n\n", b'data: {"error":{"message":"bad"}}\n\n'])
def test_responses_closes_upstream_before_terminal_event(tail):
    async def main():
        closed = []

        async def source():
            try:
                yield b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
                yield tail
                raise AssertionError("must not read after terminal data")
            finally:
                closed.append(True)

        converted = responses.chat_stream_to_responses_stream(source(), "alias")
        terminal = None
        async for raw in converted:
            data = json.loads(next(line[6:] for line in raw.splitlines() if line.startswith("data: ")))
            if data["type"] in {"response.completed", "response.failed"}:
                terminal = data["type"]
                assert closed == [True]
        assert terminal == ("response.completed" if b"[DONE]" in tail else "response.failed")

    asyncio.run(main())


@pytest.mark.parametrize("start_conversion", [False, True])
def test_responses_early_close_releases_already_peeked_stream(start_conversion):
    async def main():
        closed = []

        async def source():
            try:
                yield b"data: [DONE]\n\n"
            finally:
                closed.append(True)

        upstream = source()
        first = await anext(upstream)
        converted = responses.chat_stream_to_responses_stream(router._replay([first], upstream, ACCOUNT), "alias")
        if start_conversion:
            assert "response.created" in await anext(converted)
        await converted.aclose()
        assert closed == [True]

    asyncio.run(main())


@pytest.mark.parametrize("fail_at", ["http.response.start", "http.response.body"])
def test_send_disconnect_closes_entire_stream_chain(fail_at):
    async def main():
        closed = []

        async def source():
            try:
                yield b"data: [DONE]\n\n"
            finally:
                await asyncio.sleep(0)
                closed.append(True)

        upstream = source()
        first = await anext(upstream)
        stream = router._rewrite_stream_model(router._replay([first], upstream, ACCOUNT), "alias")
        response = ClosingStreamingResponse(stream)

        async def send(message):
            if message["type"] == fail_at:
                raise OSError("simulated disconnect")

        async def receive():
            raise AssertionError("ASGI 2.4 does not need receive here")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert closed == [True]
        assert auth_manager.pinned_for("workbuddy") is None

    asyncio.run(main())


def test_legacy_asgi_disconnect_shields_async_cleanup():
    async def main():
        sent = asyncio.Event()
        closed = []

        async def source():
            try:
                yield b"x"
            finally:
                await anyio.sleep(0)
                closed.append(True)

        async def send(message):
            if message["type"] == "http.response.body":
                sent.set()
                await anyio.sleep_forever()

        async def receive():
            await sent.wait()
            return {"type": "http.disconnect"}

        response = ClosingStreamingResponse(router._replay([], source(), ACCOUNT))
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
        assert closed == [True]

    asyncio.run(main())


def test_cancel_during_read_closes_upstream_and_propagates_cancel():
    async def main():
        reading = asyncio.Event()
        closed = []

        async def source():
            try:
                reading.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                await asyncio.sleep(0)
                closed.append(auth_manager.pinned_for("workbuddy"))

        stream = router._replay([], source(), ACCOUNT)
        task = asyncio.create_task(anext(stream))
        await reading.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [ACCOUNT]
        assert auth_manager.pinned_for("workbuddy") is None

    asyncio.run(main())


def test_dispatch_cancel_during_peek_closes_stream(monkeypatch):
    async def main():
        reading = asyncio.Event()
        closed = []

        class Source:
            def __aiter__(self):
                return self

            async def __anext__(self):
                reading.set()
                await asyncio.Event().wait()

            async def aclose(self):
                await asyncio.sleep(0)
                closed.append(auth_manager.pinned_for("workbuddy"))

        async def dispatch(*args):
            return "stream", Source()

        monkeypatch.setattr(router, "_cross_channel_failover_enabled", lambda: True)
        monkeypatch.setattr(router.providers, "get_provider", lambda _: object())
        monkeypatch.setattr(router, "_chat_after_bind_no_echo", dispatch)
        task = asyncio.create_task(router._walk_account_queue("model", {}, None, [("workbuddy", ACCOUNT)]))
        await reading.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [ACCOUNT]

    asyncio.run(main())


@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/responses"])
def test_http_endpoints_close_stream_and_preserve_protocol(monkeypatch, endpoint):
    import server

    async def main():
        closed = []

        async def source():
            try:
                assert auth_manager.pinned_for("workbuddy") == ACCOUNT
                yield b'data: {"model":"backend","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\n'
                assert auth_manager.pinned_for("workbuddy") == ACCOUNT
                yield b"data: [DONE]\n\n"
            finally:
                closed.append(auth_manager.pinned_for("workbuddy"))

        async def queue(*args):
            return [("workbuddy", ACCOUNT)]

        async def dispatch(*args):
            return "stream", source()

        monkeypatch.setattr(server, "_check_client_auth", lambda *args, **kwargs: None)
        monkeypatch.setattr(server, "_reserve_client_quota", lambda *args: None)
        monkeypatch.setattr(router, "admit_model", lambda *args: None)
        monkeypatch.setattr(router, "_request_queue", queue)
        monkeypatch.setattr(router, "_cross_channel_failover_enabled", lambda: True)
        monkeypatch.setattr(router, "_chat_after_bind_no_echo", dispatch)
        payload = {"model": "alias", "stream": True}
        if endpoint.endswith("responses"):
            payload["input"] = "hi"
        else:
            payload["messages"] = [{"role": "user", "content": "hi"}]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            result = await client.post(endpoint, json=payload)
        assert result.status_code == 200
        assert result.headers["content-type"].startswith("text/event-stream")
        assert "hello" in result.text
        assert '"model":"alias"' in result.text
        if endpoint.endswith("responses"):
            assert "response.completed" in result.text
            assert "response.failed" not in result.text
        else:
            assert "[DONE]" in result.text
        assert closed == [ACCOUNT]
        assert auth_manager.pinned_for("workbuddy") is None

    asyncio.run(main())
