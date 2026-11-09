import asyncio
from types import SimpleNamespace

import pytest

import control_plane as cp
from providers.protocol import QuotaSnapshot


@pytest.fixture(autouse=True)
def clean_cache():
    cp._quota_snapshots.clear()
    yield
    cp._quota_snapshots.clear()


@pytest.mark.parametrize('as_dict', [True, False])
def test_cache_hit_force_expiry_and_failure_retry(monkeypatch, as_dict):
    now = [1000.0]
    monkeypatch.setattr(cp.time, 'time', lambda: now[0])
    calls = []
    ok = [True]

    async def fetch(account):
        calls.append(account['id'])
        data = dict(ok=ok[0], channel='qoder', account_id=account['id'], unit='credit', remaining=5)
        return data if as_dict else QuotaSnapshot(**data)

    async def run():
        account = {'id': 1}
        await cp._fetch_quota_cached('qoder', account, fetch, False)
        await cp._fetch_quota_cached('qoder', account, fetch, False)
        assert len(calls) == 1
        await cp._fetch_quota_cached('qoder', account, fetch, True)
        assert len(calls) == 2
        now[0] += 301
        await cp._fetch_quota_cached('qoder', account, fetch, False)
        assert len(calls) == 3
        ok[0] = False
        await cp._fetch_quota_cached('loomy', account, fetch, False)
        await cp._fetch_quota_cached('loomy', account, fetch, False)
        assert len(calls) == 5

    asyncio.run(run())


def test_cache_hit_does_not_restamp_routing(monkeypatch):
    calls = []
    monkeypatch.setattr(cp, '_cache_quota_reading', lambda account, value: calls.append((account['id'], value)))

    async def fetch(account):
        return QuotaSnapshot(True, 'qoder', account['id'], 'credit', 0)

    async def run():
        provider = SimpleNamespace(fetch_quota=fetch)
        accounts = [{'id': 1}]
        await cp._summarize_quota_channel('qoder', provider, accounts)
        await cp._summarize_quota_channel('qoder', provider, accounts)
        assert calls == [(1, 0)]
        await cp._summarize_quota_channel('qoder', provider, accounts, force=True)
        assert calls == [(1, 0), (1, 0)]

    asyncio.run(run())


@pytest.mark.parametrize('as_dict', [True, False])
def test_concurrent_cache_hit_preserves_first_measurement(monkeypatch, as_dict):
    calls = []
    monkeypatch.setattr(cp, '_cache_quota_reading', lambda account, value: calls.append((account['id'], value)))

    async def run():
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()

        async def fetch(account):
            if account['id'] == 2 and not slow_started.is_set():
                slow_started.set()
                await release_slow.wait()
            snapshot = QuotaSnapshot(True, 'qoder', account['id'], 'credit', 0)
            return vars(snapshot) if as_dict else snapshot

        provider = SimpleNamespace(fetch_quota=fetch)
        first = asyncio.create_task(cp._summarize_quota_channel('qoder', provider, [{'id': 1}, {'id': 2}]))
        await asyncio.wait_for(slow_started.wait(), timeout=2)
        assert ('qoder', 1) in cp._quota_snapshots
        try:
            await cp._summarize_quota_channel('qoder', provider, [{'id': 1}, {'id': 2}])
        finally:
            release_slow.set()
            await first
        assert (1, 0) in calls, f'Account 1 zero measurement lost; routing writes: {calls}'

    asyncio.run(run())
