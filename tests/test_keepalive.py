import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import auth_manager
import control_plane
import credential_crypto
import database as db
import server
from providers.workbuddy import oauth


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    monkeypatch.delenv("CB_GATEWAY_KEEPALIVE", raising=False)
    monkeypatch.delenv("CB_GATEWAY_KEEPALIVE_HOUR", raising=False)
    credential_crypto.reset_cache()
    db.init_db()
    auth_manager.reset_session_dead_counts()
    oauth.reset_sessions()
    control_plane._keepalive_running = False
    yield path
    auth_manager.reset_session_dead_counts()
    oauth.reset_sessions()
    control_plane._keepalive_running = False
    credential_crypto.reset_cache()


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class QueueClient:
    queue = []
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def _next(self, method, url, headers=None, content=None):
        QueueClient.calls.append({"method": method, "url": url, "headers": headers or {}, "content": content})
        item = QueueClient.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return FakeResponse(status, payload)

    async def request(self, method, url, headers=None, content=None):
        return self._next(method, url, headers, content)

    async def post(self, url, headers=None, json=None):
        return self._next("POST", url, headers, json)


def _install_queue(monkeypatch, target):
    QueueClient.queue = []
    QueueClient.calls = []
    monkeypatch.setattr(target, "AsyncClient", QueueClient)


def _add_account(**extra):
    payload = {
        "name": "Ada",
        "nickname": "Ada",
        "uid": "uid-1",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "status": "active",
        "provider": "workbuddy",
        "domain": "www.codebuddy.cn",
        "expires_at": int(time.time() * 1000) + 3_600_000,
        "refresh_expires_at": int(time.time() * 1000) + 86_400_000,
    }
    payload.update(extra)
    return db.get_account(db.add_account(payload))


def test_transient_refresh_does_not_expire(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account()
    QueueClient.queue = [(500, {"code": 500, "msg": "upstream busy"})]

    result = asyncio.run(auth_manager.refresh_token_result(account))

    fresh = db.get_account(account["id"])
    assert result["ok"] is False
    assert result["reason"] == "transient"
    assert fresh["status"] == "active"
    assert fresh["access_token"] == "old-access"


def test_session_dead_expires_only_after_three_failures(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account()
    dead = (401, {"code": 12153, "msg": "Offline user session not found"})

    QueueClient.queue = [dead]
    first = asyncio.run(auth_manager.refresh_token_result(account))
    assert first["reason"] == "session_dead"
    assert first["session_dead_count"] == 1
    assert db.get_account(account["id"])["status"] == "active"

    QueueClient.queue = [dead, dead]
    asyncio.run(auth_manager.refresh_token_result(db.get_account(account["id"])))
    third = asyncio.run(auth_manager.refresh_token_result(db.get_account(account["id"])))
    assert third["session_dead_count"] == 3
    assert db.get_account(account["id"])["status"] == "expired"


def test_successful_refresh_keeps_old_refresh_token_and_revives(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account(status="expired")
    auth_manager._note_session_dead(account["id"])
    QueueClient.queue = [(200, {"code": 0, "data": {"accessToken": "new-access", "expiresIn": 120}})]

    result = asyncio.run(auth_manager.refresh_token_result(account))

    fresh = db.get_account(account["id"])
    assert result["ok"] is True
    assert fresh["status"] == "active"
    assert fresh["access_token"] == "new-access"
    assert fresh["refresh_token"] == "old-refresh"
    assert fresh["expires_at"] > int(time.time() * 1000)
    assert auth_manager.session_dead_count(account["id"]) == 0
    assert "old-refresh" not in json.dumps(result)
    assert "new-access" not in json.dumps(result)


def test_network_error_keeps_status(isolated_db, monkeypatch):
    _install_queue(monkeypatch, auth_manager.httpx)
    account = _add_account()
    QueueClient.queue = [httpx.ConnectError("down")]

    result = asyncio.run(auth_manager.refresh_token_result(account))

    assert result["reason"] == "transient"
    assert db.get_account(account["id"])["status"] == "active"


def test_keepalive_skips_inactive_and_reports_running(isolated_db, monkeypatch):
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)
    active = _add_account(uid="active-user", name="active")
    inactive = _add_account(uid="inactive-user", name="inactive", status="inactive")
    called = []

    async def fake_refresh(account):
        called.append(account["id"])
        return {
            "ok": True,
            "account_id": account["id"],
            "account_name": account["name"],
            "provider": "workbuddy",
            "reason": "refreshed",
            "message": "已续期",
            "status": "active",
            "skipped": False,
        }

    monkeypatch.setattr(auth_manager, "refresh_token_result", fake_refresh)
    summary = asyncio.run(control_plane.keepalive_all())
    assert called == [active["id"]]
    assert summary["refreshed"] == 1
    assert summary["skipped"] == 1
    assert any(row["account_id"] == inactive["id"] and row["skipped"] for row in summary["results"])

    control_plane._keepalive_running = True
    busy = asyncio.run(control_plane.keepalive_all())
    assert busy["running"] is True
    control_plane._keepalive_running = False


def test_seconds_until_hour_rolls_to_next_day():
    assert control_plane.seconds_until_hour(22, datetime(2026, 10, 13, 21, 30, 0)) == 1800
    assert control_plane.seconds_until_hour(22, datetime(2026, 10, 13, 22, 0, 0)) == 86400


def test_browser_login_stores_tokens_without_returning_them(isolated_db, monkeypatch):
    _install_queue(monkeypatch, oauth.httpx)
    state = "state-token-1"
    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": state, "authUrl": "https://www.codebuddy.cn/login?state=" + state}})
    ]
    started = asyncio.run(oauth.start_login("cn"))
    assert started["url"].startswith("https://www.codebuddy.cn/")
    assert started["realm"] == "cn"
    assert QueueClient.calls[0]["url"].startswith("https://copilot.tencent.com/v2/plugin/auth/state")

    QueueClient.queue = [(200, {"code": 9, "msg": "logging in"})]
    pending = asyncio.run(oauth.poll_login(started["state"]))
    assert pending == {"done": False, "message": "logging in"}

    QueueClient.queue = [
        (
            200,
            {
                "code": 0,
                "data": {
                    "accessToken": "access-secret",
                    "refreshToken": "refresh-secret",
                    "expiresIn": 3600,
                    "domain": "www.codebuddy.cn",
                },
            },
        ),
        (
            200,
            {
                "code": 0,
                "data": {
                    "uid": "452a1489-03b5-4af3-a64c-5688c03f1e43",
                    "nickname": "Ada",
                    "enterpriseId": "ent-1",
                },
            },
        ),
    ]
    done = asyncio.run(oauth.poll_login(started["state"]))
    encoded = json.dumps(done)
    assert done["done"] is True
    assert done["updated"] is False
    assert "access-secret" not in encoded
    assert "refresh-secret" not in encoded
    saved = db.get_account(done["id"])
    assert saved["access_token"] == "access-secret"
    assert saved["refresh_token"] == "refresh-secret"
    assert saved["nickname"] == "Ada"
    assert saved["enterprise_id"] == "ent-1"
    assert saved["provider"] == "workbuddy"

    QueueClient.queue = []
    again = asyncio.run(oauth.poll_login(started["state"]))
    assert again["id"] == done["id"]
    assert QueueClient.calls[-1]["url"].endswith("/login/account?state=" + state)


def test_browser_login_rejects_unexpected_host_and_unknown_state(isolated_db, monkeypatch):
    _install_queue(monkeypatch, oauth.httpx)
    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": "abcdefghij", "authUrl": "https://evil.example/login"}})
    ]
    with pytest.raises(ValueError, match="rejected"):
        asyncio.run(oauth.start_login("cn"))

    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": "globalstate1", "authUrl": "https://www.workbuddy.ai/login"}})
    ]
    started = asyncio.run(oauth.start_login("global"))
    assert started["realm"] == "global"
    assert QueueClient.calls[-1]["url"].startswith("https://www.workbuddy.ai/v2/plugin/auth/state")
    with pytest.raises(LookupError):
        asyncio.run(oauth.poll_login("missing-state"))


def test_repeat_browser_login_updates_same_uid(isolated_db, monkeypatch):
    _install_queue(monkeypatch, oauth.httpx)
    existing = _add_account(uid="452a1489-03b5-4af3-a64c-5688c03f1e43", weight=7, priority=3)
    state = "state-token-2"
    QueueClient.queue = [
        (200, {"code": 0, "data": {"state": state, "authUrl": "https://copilot.tencent.com/oauth"}}),
        (
            200,
            {"code": 0, "data": {"accessToken": "access-2", "refreshToken": "refresh-2", "expiresIn": 60}},
        ),
        (200, {"code": 0, "data": {"uid": existing["uid"], "nickname": "Ada 2"}}),
    ]
    asyncio.run(oauth.start_login("cn"))
    done = asyncio.run(oauth.poll_login(state))
    assert done["updated"] is True
    assert done["id"] == existing["id"]
    fresh = db.get_account(existing["id"])
    assert fresh["access_token"] == "access-2"
    assert fresh["weight"] == 7
    assert fresh["priority"] == 3
    assert fresh["status"] == "active"


def _settings_request(payload):
    body = json.dumps(payload).encode()
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "PUT", "path": "/admin/settings", "headers": [], "query_string": b""}
    return Request(scope, receive)


def test_automation_defaults_off(isolated_db):
    assert control_plane.keepalive_enabled() is False
    assert control_plane.auto_checkin_enabled() is False
    assert control_plane.automation_due(datetime(2026, 10, 13, 9, 5), 9) is True
    assert control_plane.automation_due(datetime(2026, 10, 13, 9, 10), 9) is False


def test_one_click_keepalive_also_claims(isolated_db, monkeypatch):
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)

    async def fake_refresh(account, allow_inactive=True):
        return {
            "ok": True,
            "account_id": account["id"],
            "account_name": account["name"],
            "provider": "workbuddy",
            "reason": "refreshed",
            "message": "已续期",
            "status": "active",
            "skipped": False,
        }

    async def fake_checkin(channel_filter=None, source="manual"):
        return {"total": 1, "claimed": 1, "already_claimed": 0, "failed": 0, "credit": 150, "results": [{"account_id": 1, "ok": True, "claimed": True}]}

    monkeypatch.setattr(control_plane, "refresh_one", fake_refresh)
    monkeypatch.setattr(control_plane, "checkin_all", fake_checkin)
    _add_account()
    summary = asyncio.run(control_plane.keepalive_all(claim=True))
    assert summary["refreshed"] == 1
    assert summary["claimed"] == 1
    assert summary["claim"]["claimed"] == 1


def test_settings_save_keepalive(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_check_admin", lambda _authorization: None)
    asyncio.run(
        server.admin_update_settings(
            _settings_request(
                {
                    "keepalive_enabled": True,
                    "keepalive_hour": 7,
                    "auto_checkin_enabled": True,
                    "auto_checkin_hour": 8,
                }
            )
        )
    )
    assert control_plane.keepalive_enabled() is True
    assert control_plane.keepalive_hour() == 7
    assert control_plane.auto_checkin_enabled() is True
    assert control_plane.auto_checkin_hour() == 8

    with pytest.raises(HTTPException) as err:
        asyncio.run(server.admin_update_settings(_settings_request({"keepalive_hour": 24})))
    assert err.value.status_code == 400


def test_account_page_has_keepalive_and_login_controls():
    text = Path("web/index.html").read_text(encoding="utf-8")
    assert "一键续期并领取" in text
    assert "定时续期" in text
    assert "浏览器登录" in text
    assert "/admin/accounts/keepalive-all" in text
    assert "/admin/workbuddy/login/start" in text
    assert "自动化日志" in text
    assert "/admin/automation-logs" in text
    assert "refreshLife" in text
    assert "keepalive_enabled" in text
    assert "auto_checkin_enabled" in text
    assert "默认关闭" in text


def test_checkin_failure_is_kept_without_secrets(isolated_db, monkeypatch):
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)
    monkeypatch.setattr(control_plane.providers, "enabled_provider_ids", lambda: ["traework"])
    monkeypatch.setattr(
        control_plane.providers,
        "get_provider",
        lambda channel: type("Provider", (), {"checkin_supported": True})(),
    )

    async def accounts(channel, status="active"):
        return [
            {
                "id": 4,
                "nickname": "用户甲",
                "provider": "traework",
                "status": "active",
                "access_token": "super-secret-token",
            },
            {
                "id": 5,
                "nickname": "用户乙",
                "provider": "traework",
                "status": "active",
                "access_token": "another-secret-token",
            },
        ]

    async def claim(account):
        if account["id"] == 4:
            raise RuntimeError("登录失败 Bearer super-secret-token")
        return {
            "ok": True,
            "claimed": True,
            "account_id": account["id"],
            "account_name": account["nickname"],
            "credit": 100,
            "message": "领取成功",
            "access_token": account["access_token"],
        }

    monkeypatch.setattr(control_plane, "_channel_accounts", accounts)
    monkeypatch.setattr(control_plane, "claim_one", claim)
    summary = asyncio.run(control_plane.checkin_all(source="schedule"))
    assert summary["failed"] == 1
    assert summary["claimed"] == 1

    failed = db.list_automation_events(failures_only=True)
    assert len(failed) == 1
    assert failed[0]["source"] == "schedule"
    assert failed[0]["job"] == "checkin"
    assert failed[0]["channel"] == "traework"
    assert failed[0]["account_name"] == "用户甲"
    assert failed[0]["outcome"] == "failed"
    assert failed[0]["status_code"] == 0
    assert "super-secret-token" not in failed[0]["message"]
    assert failed[0]["message"] == "登录失败 [redacted]"

    stored = json.dumps(db.list_automation_events(failures_only=False), ensure_ascii=False)
    assert "super-secret-token" not in stored
    assert "another-secret-token" not in stored
    latest = db.latest_automation_runs()
    assert latest[0]["job"] == "checkin"
    assert latest[0]["source"] == "schedule"
    assert latest[0]["failed"] == 1
    assert latest[0]["total"] == 2


def test_old_automation_events_are_pruned(isolated_db):
    db.add_automation_events(
        [
            {
                "created_at": int(time.time()) - 40 * 86400,
                "source": "schedule",
                "job": "checkin",
                "run_id": "old",
                "outcome": "failed",
                "ok": False,
                "message": "old failure",
            }
        ]
    )
    assert db.list_automation_events() == []


def test_automation_logs_endpoint_returns_failures(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_check_admin", lambda _authorization: None)
    db.add_automation_events(
        [
            {
                "source": "schedule",
                "job": "checkin",
                "run_id": "run",
                "channel": "qwenwork",
                "account_name": "书虫",
                "outcome": "failed",
                "ok": False,
                "status_code": 401,
                "message": "登录已失效",
            }
        ]
    )
    payload = asyncio.run(server.admin_automation_logs(limit=20, failures_only=1, authorization=None))
    assert payload["items"][0]["message"] == "登录已失效"
    assert payload["items"][0]["channel"] == "qwenwork"
    assert payload["latest"][0]["failed"] == 1


def test_automation_text_redacts_non_bearer_credentials():
    samples = [
        "Cloud-IDE-JWT fake-header-value",
        "authorization: fake-header-value",
        "cookie=fake-header-value",
        "access_token=fake-header-value",
        "https://example.test/callback?token=fake-header-value",
        "eyJaaaaaaaaaaaaaaa.bbbbbbbbbbbb.cccccccc",
        "A" * 40,
    ]
    for sample in samples:
        cleaned = db.public_automation_text(sample)
        assert "fake-header-value" not in cleaned
        assert "A" * 40 not in cleaned
        assert "eyJaaaaaaaaaaaaaaa" not in cleaned
    assert db.public_automation_text("登录已失效") == "登录已失效"
    assert db.public_automation_text("token refresh failed") == "token refresh failed"


def test_scheduled_checkin_busy_does_not_release_other_job(isolated_db, monkeypatch):
    monkeypatch.setattr(control_plane, "auto_checkin_enabled", lambda: True)
    control_plane._keepalive_running = True
    asyncio.run(control_plane._run_checkin_safe("schedule"))
    assert control_plane._keepalive_running is True
    rows = db.list_automation_events(failures_only=True)
    assert rows[0]["message"] == "已有保活或领取任务在进行，本次未执行"
    assert rows[0]["job"] == "checkin"
    control_plane._keepalive_running = False
    acquired = asyncio.run(control_plane._begin_job("manual"))
    assert acquired is None
    asyncio.run(control_plane._end_job())
    assert control_plane._keepalive_running is False


def test_keepalive_account_error_is_recorded_and_continues(isolated_db, monkeypatch):
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)
    monkeypatch.setattr(control_plane.providers, "enabled_provider_ids", lambda: ["workbuddy"])

    async def accounts(channel, status="active"):
        return [
            {"id": 1, "nickname": "甲", "provider": "workbuddy", "status": "active"},
            {"id": 2, "nickname": "乙", "provider": "workbuddy", "status": "active"},
        ]

    async def refresh(account, allow_inactive=True):
        if account["id"] == 1:
            raise RuntimeError("Cloud-IDE-JWT fake-header-value")
        return {
            "ok": True,
            "account_id": 2,
            "account_name": "乙",
            "provider": "workbuddy",
            "message": "已续期",
            "status": "active",
        }

    monkeypatch.setattr(control_plane, "_channel_accounts", accounts)
    monkeypatch.setattr(control_plane, "refresh_one", refresh)
    summary = asyncio.run(control_plane.keepalive_all(source="schedule"))
    assert summary["refreshed"] == 1
    assert summary["failed"] == 1
    failed = db.list_automation_events(failures_only=True)
    assert failed[0]["account_name"] == "甲"
    assert failed[0]["job"] == "keepalive"
    assert "fake-header-value" not in failed[0]["message"]


def test_channel_budget_settings_persist_and_reload(isolated_db, monkeypatch):
    import context_budget
    monkeypatch.setattr(server, "_check_admin", lambda _authorization: None)
    monkeypatch.setattr(context_budget, "_saved_channel_budgets", {})
    monkeypatch.delenv("CB_PRETRIM_CHANNELS", raising=False)
    budgets = {"workbuddy": 500, "workbuddy-i": 0, "qwenwork": 800}
    asyncio.run(server.admin_update_settings(_settings_request({"context_channel_budgets": budgets, "loomy_context_budget": 900})))
    assert db.get_setting("context_channel_budgets") == budgets
    assert context_budget.channel_input_budget("workbuddy") == 500
    assert context_budget.channel_input_budget("workbuddy-i") == 0
    context_budget.set_saved_channel_budgets({})
    context_budget.load_saved_channel_budgets()
    assert context_budget.channel_input_budget("qwenwork") == 800
    asyncio.run(server.admin_update_settings(_settings_request({"context_channel_budgets": {}})))
    monkeypatch.delenv("CB_PRETRIM_BUDGET", raising=False)
    assert context_budget.channel_input_budget("workbuddy") == 0


@pytest.mark.parametrize("budgets", [[], {"unknown": 100}, {"loomy": 100}, {"workbuddy": -1}, {"workbuddy": 2000001}, {"workbuddy": 1.5}, {"workbuddy": True}])
def test_channel_budget_settings_reject_invalid_values(isolated_db, monkeypatch, budgets):
    monkeypatch.setattr(server, "_check_admin", lambda _authorization: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(server.admin_update_settings(_settings_request({"context_channel_budgets": budgets})))
    assert exc.value.status_code == 400
    assert db.get_setting("context_channel_budgets") is None
