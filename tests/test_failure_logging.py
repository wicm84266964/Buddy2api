"""Client-visible failures get a log even when no provider wrote one."""

import asyncio
import json

import httpx
import pytest

import credential_crypto
import database as db
import router
import server
from request_logging import bind_dispatch


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "test-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    monkeypatch.setattr(server, "_check_client_auth", lambda *_args, **_kwargs: {
        "id": 1, "name": "test-key", "default_channel": "loomy",
    })
    monkeypatch.setattr(router, "admit_model", lambda *_args: "test-model")
    yield
    credential_crypto.reset_cache()


def request(path, payload):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app, raise_app_exceptions=False),
            base_url="http://localhost",
        ) as client:
            return await client.post(path, json=payload)
    return asyncio.run(run())


@pytest.mark.parametrize("path,handler,payload", [
    ("/v1/chat/completions", "chat_after_name", {"messages": [{"role": "user", "content": "ping"}]}),
    ("/v1/responses", "responses_after_name", {"input": "ping"}),
])
@pytest.mark.parametrize("status", [429, 502, 503])
def test_unlogged_http_failure_is_persisted(isolated_db, monkeypatch, path, handler, payload, status):
    async def dispatch(*_args, **_kwargs):
        bind_dispatch("traework", {"id": 9, "name": "account"}, "actual-model")
        return "error", (status, {"error": {"message": "upstream unavailable"}})

    monkeypatch.setattr(router, handler, dispatch)
    response = request(path, {"model": "test-model", **payload})
    assert response.status_code == status
    logs = db.list_logs()
    assert len(logs) == 1
    log = logs[0]
    assert log["status_code"] == status
    assert log["model"] == "test-model"
    assert log["resolved_model"] == "actual-model"
    assert log["provider"] == "traework"
    assert log["account_id"] == 9
    assert log["api_key_name"] == "test-key"
    assert "upstream unavailable" in log["error_msg"]


def test_bridge_exception_is_logged(isolated_db, monkeypatch):
    async def dispatch(*_args, **_kwargs):
        raise ValueError("bridge broke")

    monkeypatch.setattr(router, "responses_after_name", dispatch)
    response = request("/v1/responses", {"model": "test-model", "input": "ping"})
    assert response.status_code == 502
    assert "bridge broke" in db.list_logs()[0]["error_msg"]


def test_uncaught_exception_is_logged(isolated_db, monkeypatch):
    async def dispatch(*_args, **_kwargs):
        raise RuntimeError("dispatch broke")

    monkeypatch.setattr(router, "chat_after_name", dispatch)
    response = request("/v1/chat/completions", {
        "model": "test-model", "messages": [{"role": "user", "content": "ping"}],
    })
    assert response.status_code == 500
    assert "dispatch broke" in db.list_logs()[0]["error_msg"]


@pytest.mark.parametrize("provider_logged", [False, True])
def test_provider_failure_is_not_duplicated(isolated_db, monkeypatch, provider_logged):
    async def dispatch(*_args, **_kwargs):
        if provider_logged:
            db.record_request({"model": "test-model", "provider": "loomy", "status_code": 502,
                               "finish_reason": "error", "increment_usage": False})
        return "error", (502, {"error": {"message": "failed"}})

    monkeypatch.setattr(router, "responses_after_name", dispatch)
    request("/v1/responses", {"model": "test-model", "input": "ping"})
    assert len(db.list_logs()) == 1


@pytest.mark.parametrize("event", [
    {"error": {"message": "stream broke"}},
    {"type": "response.failed", "response": {"status": "failed", "error": {"message": "stream broke"}}},
])
@pytest.mark.parametrize("prior_log", [None, "stop", "retry", "error"])
def test_stream_failure_is_logged_after_stream_closes(isolated_db, monkeypatch, event, prior_log):
    async def stream():
        encoded = ("data: " + json.dumps(event) + "\n\n").encode()
        yield encoded[:10]
        yield encoded[10:]
        if prior_log:
            db.record_request({"model": "test-model", "provider": "loomy",
                               "status_code": 502 if prior_log in {"retry", "error"} else 200,
                               "finish_reason": prior_log, "increment_usage": False})

    async def dispatch(*_args, **_kwargs):
        return "stream", stream()

    monkeypatch.setattr(router, "responses_after_name", dispatch)
    response = request("/v1/responses", {"model": "test-model", "input": "ping", "stream": True})
    assert response.status_code == 200
    logs = db.list_logs()
    assert len(logs) == (2 if prior_log in {"stop", "retry"} else 1)
    assert logs[0]["status_code"] == 502
    assert logs[0]["finish_reason"] == "error"


def test_fallback_does_not_double_count_usage(isolated_db, monkeypatch):
    aid = db.add_account({"provider": "loomy", "name": "account", "uid": "uid"})

    async def dispatch(*_args, **_kwargs):
        db.record_request({"account_id": aid, "model": "test-model", "provider": "loomy",
                           "status_code": 200, "finish_reason": "stop", "total_tokens": 12})
        raise ValueError("bridge failed after upstream success")

    monkeypatch.setattr(router, "responses_after_name", dispatch)
    request("/v1/responses", {"model": "test-model", "input": "ping"})
    assert len(db.list_logs()) == 2
    account = db.get_account(aid)
    assert account["total_requests"] == 1
    assert account["total_tokens"] == 12


def test_success_needs_no_fallback_log(isolated_db, monkeypatch):
    async def dispatch(*_args, **_kwargs):
        return "json", {"choices": []}

    monkeypatch.setattr(router, "responses_after_name", dispatch)
    assert request("/v1/responses", {"model": "test-model", "input": "ping"}).status_code == 200
    assert db.list_logs() == []
