import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from providers.loomy import chat


@pytest.mark.parametrize('first', [502, 503, 'disconnect'])
def test_open_retries_once_and_closes_failed_response(first, monkeypatch):
    events = []
    calls = []
    logs = []

    async def sleep(delay):
        events.append('sleep')

    monkeypatch.setattr(chat.asyncio, 'sleep', sleep)

    class Client:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            calls.append(kwargs['content'])
            n = len(calls)
            try:
                if n == 1 and first == 'disconnect':
                    raise httpx.RemoteProtocolError('no response')
                yield httpx.Response(first if n == 1 else 200, text='proxy error' if n == 1 else 'OK')
            finally:
                events.append(f'closed-{n}')

    async def run():
        async with chat._open_stream(Client(), {'access_token': 'test'}, 'same body', lambda *a: logs.append(a)) as response:
            assert response.status_code == 200

    asyncio.run(run())
    assert calls == ['same body', 'same body']
    assert events == ['closed-1', 'sleep', 'closed-2']
    assert len(logs) == 1
    assert logs[0][0] == (503 if first == 'disconnect' else first)


@pytest.mark.parametrize('status', [400, 401, 429, 504])
def test_open_does_not_retry_other_statuses(status):
    calls = []

    class Client:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            calls.append(1)
            yield httpx.Response(status)

    async def run():
        async with chat._open_stream(Client(), {}, '{}') as response:
            assert response.status_code == status

    asyncio.run(run())
    assert calls == [1]


def test_accepted_stream_disconnect_is_not_replayed():
    calls = []

    class Client:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            calls.append(1)
            yield httpx.Response(200)

    async def run():
        async with chat._open_stream(Client(), {}, '{}'):
            raise httpx.RemoteProtocolError('interrupted after headers')

    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(run())
    assert calls == [1]


@pytest.mark.parametrize('disconnect', [False, True])
def test_open_retry_is_bounded(disconnect, monkeypatch):
    calls = []

    async def sleep(delay):
        pass

    monkeypatch.setattr(chat.asyncio, 'sleep', sleep)

    class Client:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            calls.append(1)
            if disconnect:
                raise httpx.RemoteProtocolError('no response')
            yield httpx.Response(502)

    async def run():
        async with chat._open_stream(Client(), {}, '{}') as response:
            assert response.status_code == 502

    if disconnect:
        with pytest.raises(httpx.RemoteProtocolError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert len(calls) == 2
