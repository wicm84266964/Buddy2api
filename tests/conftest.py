"""Shared test isolation.

`auth_manager` keeps account cooldowns and stickiness in module-level dicts keyed
by account id, and `routing` keeps rate windows the same way. Every test gets a
fresh database whose first account is id 1, so without this reset a cooldown
recorded by one test silently hides account 1 from every later test.
"""

import pytest

import auth_manager
import routing
import rate_limiter


def _clear() -> None:
    with auth_manager._failure_lock:
        auth_manager._account_failures.clear()
    auth_manager._sticky_account_id.clear()
    routing.reset_rate_windows()
    rate_limiter.reset()
    # ``chat_after_name`` notes the request's 放行 channels in a ContextVar and
    # deliberately never unsets it (a request task outlives the dispatch call).
    # Tests share one context, so clear it here or one test's exemption leaks
    # into every later test.
    routing.note_quota_exempt(())


@pytest.fixture(autouse=True)
def _reset_shared_routing_state(monkeypatch, real_rate_limit_settings):
    # Transport/protocol unit tests must not read the developer's settings or
    # require a database schema. Limiter tests opt into their own configuration.
    monkeypatch.setattr(rate_limiter, "settings", lambda: dict(rate_limiter.DEFAULT_SETTINGS))
    _clear()
    yield
    _clear()


@pytest.fixture
def real_rate_limit_settings():
    """Expose the real reader to settings persistence tests before isolation."""
    return rate_limiter.settings
