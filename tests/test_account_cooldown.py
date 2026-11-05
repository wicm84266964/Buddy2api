"""Account cooldown backoff and quota-reset hints.

Regression cover for two bugs that showed up on a live gateway: a drained
account was retried roughly every 30 seconds for nine hours, because

  * ``account_is_cooling_down`` deleted the consecutive-failure counter along
    with the expired deadline, so the backoff never escalated past its first
    step, and
  * the 6004 envelope advertises the exact reset moment and nobody read it.
"""

from datetime import datetime, timedelta, timezone

import pytest

import auth_manager
import proxy
import workbuddy_outbound

QUOTA_MSG = {
    "code": 6004,
    "msg": "您的使用量已超出频率限制，将在 2026-11-05 20:41:47 UTC+8 重置，您也可以切换其他模型继续使用。",
    "requestId": "090afc85330d4ae3b08863230c743912",
}
RESET_TS = datetime(2026, 11, 5, 20, 41, 47, tzinfo=timezone(timedelta(hours=8))).timestamp()


def _freeze(monkeypatch, clock):
    monkeypatch.setattr(auth_manager, "_now", lambda: clock[0])


def _cooldown_of(aid, now):
    return round(auth_manager._account_failures[aid][1] - now)


def test_backoff_keeps_escalating_after_the_cooldown_expires(monkeypatch):
    clock = [1000.0]
    _freeze(monkeypatch, clock)

    steps = []
    for _ in range(6):
        auth_manager.mark_account_failure(7, 429)
        steps.append(_cooldown_of(7, clock[0]))
        assert auth_manager.account_is_cooling_down(7) is True
        clock[0] += steps[-1] + 0.1        # the cooldown elapses, nothing else happens
        assert auth_manager.account_is_cooling_down(7) is False

    assert steps == [30, 60, 120, 240, 300, 300]


def test_success_resets_the_streak():
    auth_manager.mark_account_failure(9, 429)
    assert auth_manager.account_is_cooling_down(9) is True

    auth_manager.mark_account_success(9)
    assert 9 not in auth_manager._account_failures
    assert auth_manager.account_is_cooling_down(9) is False


def test_failure_streak_decays_after_a_quiet_window(monkeypatch):
    clock = [1000.0]
    _freeze(monkeypatch, clock)

    steps = []
    for _ in range(5):
        auth_manager.mark_account_failure(8, 429)
        steps.append(_cooldown_of(8, clock[0]))
        clock[0] += 400

    assert steps == [30, 60, 120, 240, 300]

    clock[0] += auth_manager._FAILURE_DECAY_SECONDS
    auth_manager.mark_account_failure(8, 429)
    assert _cooldown_of(8, clock[0]) == 30


def test_quota_hint_raises_the_cooldown_and_is_capped(monkeypatch):
    clock = [1000.0]
    _freeze(monkeypatch, clock)

    auth_manager.mark_account_failure(11, 429, cooldown_hint=900)
    assert _cooldown_of(11, clock[0]) == 900

    auth_manager.mark_account_failure(12, 429, cooldown_hint=86400)
    assert _cooldown_of(12, clock[0]) == auth_manager._QUOTA_HINT_MAX_SECONDS


def test_quota_hint_never_shortens_the_backoff(monkeypatch):
    clock = [1000.0]
    _freeze(monkeypatch, clock)

    auth_manager.mark_account_failure(13, 429, cooldown_hint=5)
    assert _cooldown_of(13, clock[0]) == 30


def test_quota_reset_seconds_reads_the_envelope():
    now = datetime(2026, 11, 5, 11, 35, 16, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert workbuddy_outbound.quota_reset_seconds(QUOTA_MSG, now=now) == pytest.approx(9 * 3600 + 6 * 60 + 31)


def test_quota_reset_seconds_assumes_utc_plus_8_without_an_offset():
    now = datetime(2026, 11, 5, 20, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
    detail = {"code": 6004, "msg": "使用量已超出频率限制，将在 2026-11-05 20:41:47 重置"}
    assert workbuddy_outbound.quota_reset_seconds(detail, now=now) == pytest.approx(41 * 60 + 47)


@pytest.mark.parametrize("detail", [
    None,
    "",
    {"code": 6004, "msg": "您的使用量已超出频率限制"},                              # no timestamp
    {"code": 6004, "msg": "将在 2026-11-05 20:41:47 UTC+8 恢复"},                    # no quota wording
    {"code": 14003, "msg": "Model busy, reset at 2026-13-45 99:99:99 UTC+8"},        # unparsable
])
def test_quota_reset_seconds_returns_none_when_unusable(detail):
    assert workbuddy_outbound.quota_reset_seconds(detail) is None


def test_quota_reset_seconds_ignores_a_moment_in_the_past():
    past = datetime(2026, 11, 5, 21, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert workbuddy_outbound.quota_reset_seconds(QUOTA_MSG, now=past) is None


def test_rotate_on_a_quota_envelope_feeds_the_reset_hint(monkeypatch):
    seen = []
    monkeypatch.setattr(
        auth_manager, "mark_account_failure",
        lambda *args, **kwargs: seen.append((args, kwargs)),
    )

    # Build the envelope from a moment two hours out, so the assertion holds
    # whenever the suite runs.
    deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    stamp = deadline.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    detail = {
        "code": 6004,
        "msg": f"您的使用量已超出频率限制，将在 {stamp} UTC+8 重置，您也可以切换其他模型继续使用。",
    }

    before = datetime.now().timestamp()
    proxy._apply_account_rule(5, 200, detail)
    after = datetime.now().timestamp()

    (args, kwargs), = seen
    assert args == (5, 429)
    expected = deadline.timestamp()
    # The stamp is truncated to whole seconds.
    assert expected - 1 - after <= kwargs["cooldown_hint"] <= expected + 1 - before


def test_rotate_without_a_timestamp_still_records_the_failure(monkeypatch):
    seen = []
    monkeypatch.setattr(
        auth_manager, "mark_account_failure",
        lambda *args, **kwargs: seen.append((args, kwargs)),
    )

    proxy._apply_account_rule(5, 429, {"code": 14003, "msg": "当前模型繁忙，请稍后重试"})

    (args, kwargs), = seen
    assert args == (5, 429)
    assert kwargs["cooldown_hint"] is None
