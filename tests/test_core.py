import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException

import credential_crypto
import database as db
import proxy
import reasoning_controls
import responses
import server
import auth_manager


def _chat_sse(payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n".encode("utf-8")


def _collect_response_events(
    chunks: list[bytes],
    resp_payload: dict | None = None,
) -> list[tuple[str, dict]]:
    async def source():
        for chunk in chunks:
            yield chunk

    async def collect():
        return [
            event
            async for event in responses.chat_stream_to_responses_stream(
                source(),
                "test-model",
                resp_payload,
            )
        ]

    parsed = []
    for raw_event in asyncio.run(collect()):
        assert raw_event.endswith("\n\n")
        lines = raw_event.strip().splitlines()
        event_name = next(line[7:] for line in lines if line.startswith("event: "))
        data = "\n".join(line[6:] for line in lines if line.startswith("data: "))
        payload = json.loads(data)
        assert payload["type"] == event_name
        parsed.append((event_name, payload))
    return parsed


def _events_of_type(events: list[tuple[str, dict]], event_name: str) -> list[dict]:
    return [payload for name, payload in events if name == event_name]


def test_build_backend_body_adds_configured_reasoning_default_for_deepseek(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "high")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda model: model)

    body = proxy.build_backend_body({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "hello"}],
    })

    assert body["reasoning_effort"] == "high"


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_build_backend_body_uses_high_reasoning_default_when_unset(monkeypatch, model):
    monkeypatch.delenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)

    body = proxy.build_backend_body({"model": model, "messages": []})

    assert body["reasoning_effort"] == "high"


def test_build_backend_body_allows_reasoning_default_to_be_disabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "off")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)

    body = proxy.build_backend_body({"model": "deepseek-v4-pro", "messages": []})

    assert "reasoning_effort" not in body


def test_build_backend_body_maps_developer_messages_to_system(monkeypatch):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda model: model)
    messages = [
        {"role": "developer", "content": "developer instructions"},
        {"role": "user", "content": "hello"},
    ]

    body = proxy.build_backend_body({
        "model": "deepseek-v4-pro",
        "messages": messages,
    })

    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert messages[0]["role"] == "developer"


def test_audit_detector_requires_a_short_refusal_response():
    refusal = "系统检测到您当前输入的信息存在敏感内容，无法响应您的请求，请检查后重新输入。"
    quoted_in_normal_answer = (
        "这是对 issue 的分析。上游曾返回“系统检测到您当前输入的信息存在敏感内容，"
        "无法响应您的请求，请检查后重新输入”，但当前请求本身返回了正常结果。"
    )

    assert proxy._looks_like_audit_block(refusal)
    assert not proxy._looks_like_audit_block(quoted_in_normal_answer)


@pytest.mark.parametrize("model", ["glm-5.2", "auto", "unknown-model"])
def test_build_backend_body_does_not_add_reasoning_default_to_other_models(monkeypatch, model):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "high")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)

    body = proxy.build_backend_body({"model": model, "messages": []})

    assert "reasoning_effort" not in body


@pytest.mark.parametrize(
    ("explicit", "expected"),
    [
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "high"),
        ("high", "high"),
        ("xhigh", "max"),
        ("max", "max"),
        ("ultra", "max"),
    ],
)
def test_build_backend_body_projects_standard_reasoning_effort(
    monkeypatch,
    explicit,
    expected,
):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "high")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda model: model)

    body = proxy.build_backend_body({
        "model": "deepseek-v4-flash",
        "messages": [],
        "reasoning_effort": explicit,
    })

    assert body["reasoning_effort"] == expected


@pytest.mark.parametrize("explicit", ["none", "off"])
def test_build_backend_body_disables_reasoning_without_reinjecting_default(
    monkeypatch,
    explicit,
):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "high")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda model: model)

    body = proxy.build_backend_body({
        "model": "deepseek-v4-flash",
        "messages": [],
        "reasoning_effort": explicit,
    })

    assert "reasoning_effort" not in body


@pytest.mark.parametrize("explicit", ["none", "minimal", "medium", "xhigh", "ultra"])
def test_build_backend_body_preserves_explicit_effort_for_other_models(
    monkeypatch,
    explicit,
):
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)

    body = proxy.build_backend_body({
        "model": "glm-5.2",
        "messages": [],
        "reasoning_effort": explicit,
    })

    assert body["reasoning_effort"] == explicit


def test_build_backend_body_maps_disabled_thinking_without_injecting_default(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "high")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda model: model)
    thinking = {"type": "disabled"}

    body = proxy.build_backend_body({
        "model": "deepseek-v4-pro",
        "messages": [],
        "thinking": thinking,
    })

    assert "thinking" not in body
    assert "reasoning_effort" not in body


@pytest.mark.parametrize("thinking", [{"type": "enabled"}, {"type": "adaptive"}])
def test_build_backend_body_maps_enabled_thinking_to_high(monkeypatch, thinking):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "off")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda model: model)

    body = proxy.build_backend_body({
        "model": "deepseek-v4-pro",
        "messages": [],
        "thinking": thinking,
    })

    assert body["reasoning_effort"] == "high"


def test_chat_reasoning_effort_overrides_lower_priority_disable_switch():
    normalized = reasoning_controls.normalize_chat_reasoning({
        "reasoning_effort": "high",
        "thinking": {"type": "disabled"},
    })

    assert normalized["reasoning_effort"] == "high"


def test_output_config_effort_overrides_cross_object_thinking_disable():
    normalized = reasoning_controls.normalize_chat_reasoning({
        "output_config": {"effort": "high"},
        "thinking": {"type": "disabled"},
    })

    assert normalized["reasoning_effort"] == "high"


def test_higher_priority_switch_overrides_cross_dialect_switch():
    normalized = reasoning_controls.normalize_chat_reasoning({
        "thinking": {"type": "enabled"},
        "enable_thinking": False,
    })

    assert normalized["reasoning_effort"] == "high"


def test_reasoning_control_rejects_conflict_inside_native_object():
    with pytest.raises(reasoning_controls.InvalidReasoningControl):
        reasoning_controls.normalize_chat_reasoning({
            "reasoning": {"effort": "high", "enabled": False},
        })


def test_default_reasoning_value_defers_to_lower_priority_explicit_switch():
    normalized = reasoning_controls.normalize_chat_reasoning({
        "reasoning_effort": "default",
        "thinking": {"type": "disabled"},
    })

    assert normalized["reasoning_effort"] == "none"


def test_reasoning_normalization_preserves_extensions_and_budget_idempotently():
    payload = {
        "thinking": {
            "type": "enabled",
            "budget_tokens": 4096,
            "display": "hidden",
        },
        "reasoning": {
            "exclude": True,
            "max_tokens": 8192,
        },
    }

    normalized = reasoning_controls.normalize_chat_reasoning(payload)
    normalized_again = reasoning_controls.normalize_chat_reasoning(normalized)

    assert normalized["reasoning_effort"] == "high"
    assert normalized["thinking"] == {
        "budget_tokens": 4096,
        "display": "hidden",
    }
    assert normalized["reasoning"] == {
        "exclude": True,
        "max_tokens": 8192,
    }
    assert normalized_again == normalized
    control = reasoning_controls.resolve_reasoning_control(payload)
    assert control.budget_tokens == 4096


def _collect_chat_proxy_stream(
    chunks: list[bytes],
    monkeypatch,
    body: dict | None = None,
    stream_error: Exception | None = None,
) -> bytes:
    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def aiter_bytes(self):
            for chunk in chunks:
                yield chunk
            if stream_error is not None:
                raise stream_error

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, *args, **kwargs):
            return FakeResponse()

    account = {"id": 1, "name": "test-account"}

    async def pick_account(_excluded):
        return account

    async def valid_headers(_account):
        return {"Authorization": "Bearer test"}

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick_account)
    monkeypatch.setattr(auth_manager, "get_valid_headers", valid_headers)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda _account_id: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_args: None)
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda _default: 30)
    monkeypatch.setattr(proxy, "_log_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeAsyncClient)

    async def collect():
        return b"".join([
            chunk
            async for chunk in proxy._stream_upstream(
                body or {"model": "test-model", "stream": True},
                None,
                "test-model",
            )
        ])

    return asyncio.run(collect())


def _parse_chat_proxy_sse(raw: bytes) -> tuple[list[dict], int]:
    payloads = []
    done_count = 0
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done_count += 1
        elif data:
            payloads.append(json.loads(data))
    return payloads, done_count


def _assert_chat_proxy_error_only(raw: bytes) -> None:
    payloads, done_count = _parse_chat_proxy_sse(raw)
    assert len(payloads) == 1
    assert payloads[0].get("error")
    assert done_count == 1


def _install_chat_account_stream_fakes(
    monkeypatch,
    accounts: list[dict],
    streams: dict[int, list[bytes]],
) -> dict:
    calls = {
        "picks": [],
        "failures": [],
        "successes": [],
        "logs": [],
        "delays": [],
    }

    class FakeResponse:
        status_code = 200

        def __init__(self, chunks):
            self.chunks = chunks

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def aiter_bytes(self):
            for chunk in self.chunks:
                yield chunk

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, *args, headers, **kwargs):
            account_id = int(headers["X-Test-Account"])
            return FakeResponse(streams[account_id])

    async def pick_account(excluded):
        calls["picks"].append(set(excluded))
        return next((account for account in accounts if account["id"] not in excluded), None)

    async def valid_headers(account):
        return {"X-Test-Account": str(account["id"])}

    async def retry_delay(attempt):
        calls["delays"].append(attempt)

    def record_log(*args, **kwargs):
        calls["logs"].append((args, kwargs))

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick_account)
    monkeypatch.setattr(auth_manager, "get_valid_headers", valid_headers)
    monkeypatch.setattr(
        auth_manager,
        "mark_account_failure",
        lambda account_id, status=0: calls["failures"].append((account_id, status)),
    )
    monkeypatch.setattr(
        auth_manager,
        "mark_account_success",
        lambda account_id: calls["successes"].append(account_id),
    )
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda _default: 30)
    monkeypatch.setattr(proxy, "_retry_delay", retry_delay)
    monkeypatch.setattr(proxy, "_log_request", record_log)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeAsyncClient)
    return calls


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_encrypt_without_master_key_uses_fernet_key_file(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.delenv("CB_GATEWAY_MASTER_KEY", raising=False)
    credential_crypto.reset_cache()
    db.init_db()
    account_id = db.add_account({"name": "portable", "access_token": "access-secret"})
    with sqlite3.connect(path) as conn:
        raw = conn.execute("SELECT access_token FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    assert raw.startswith("enc:v1:fernet:")
    assert "access-secret" not in raw
    assert db.get_account(account_id)["access_token"] == "access-secret"
    credential_crypto.reset_cache()


def test_account_credentials_are_encrypted_at_rest(isolated_db):
    account_id = db.add_account(
        {
            "name": "test",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "session_state": "session-secret",
        }
    )

    assert db.get_account(account_id)["access_token"] == "access-secret"
    with sqlite3.connect(isolated_db) as conn:
        raw = conn.execute("SELECT access_token, refresh_token FROM accounts WHERE id=?", (account_id,)).fetchone()
    assert raw[0].startswith("enc:v1:")
    assert "access-secret" not in raw[0]
    assert raw[1].startswith("enc:v1:")


def test_plaintext_credentials_are_migrated_on_startup(isolated_db):
    account_id = db.add_account({"name": "test", "access_token": "old", "refresh_token": "old-refresh"})
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE accounts SET access_token='legacy-access', refresh_token='legacy-refresh' WHERE id=?",
            (account_id,),
        )
        conn.commit()
    db.init_db()
    assert db.get_account(account_id)["access_token"] == "legacy-access"
    with sqlite3.connect(isolated_db) as conn:
        raw = conn.execute("SELECT access_token FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    assert raw.startswith("enc:v1:")


def test_daily_limit_reservation_is_atomic(isolated_db):
    key_id = db.add_api_key("sk-cb-test", "test", daily_limit=5)

    def reserve():
        return db.reserve_api_key_request(key_id, 5)

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: reserve(), range(24)))

    assert sum(results) == 5
    assert db.get_api_key_daily_requests(key_id) == 5


def test_api_keys_are_encrypted_and_recoverable_for_admin(isolated_db):
    raw_key = "sk-cb-recoverable-test-key"
    db.add_api_key(raw_key, "test")

    with sqlite3.connect(isolated_db) as conn:
        stored_hash, stored_secret = conn.execute(
            "SELECT key_hash, key_secret FROM api_keys"
        ).fetchone()

    assert raw_key not in stored_hash
    assert stored_secret.startswith("enc:v1:")
    assert raw_key not in stored_secret
    assert "key" not in db.list_api_keys()[0]
    assert db.list_api_keys(include_secret=True)[0]["key"] == raw_key
    assert "key" not in db.get_api_key_by_key(raw_key)


def test_hash_only_legacy_api_key_is_reported_as_unrecoverable(isolated_db):
    db.add_api_key("sk-cb-legacy-test-key", "legacy")
    with sqlite3.connect(isolated_db) as conn:
        conn.execute("UPDATE api_keys SET key_secret=NULL")
        conn.commit()

    assert db.list_api_keys(include_secret=True)[0]["key"] is None


def test_plaintext_legacy_api_key_is_migrated_to_encrypted_storage(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "legacy-keys.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY,
                key TEXT,
                name TEXT,
                status TEXT,
                allowed_models TEXT,
                daily_limit INTEGER,
                total_requests INTEGER,
                total_tokens INTEGER,
                created_at INTEGER,
                last_used_at INTEGER
            )
        """)
        conn.execute(
            "INSERT INTO api_keys VALUES (1,?,?,?,?,?,?,?,?,?)",
            (
                "sk-cb-plaintext-legacy",
                "legacy",
                "active",
                None,
                0,
                0,
                0,
                1,
                None,
            ),
        )

    db.init_db()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
        stored = conn.execute("SELECT key_hash, key_secret FROM api_keys").fetchone()
    assert "key" not in columns
    assert stored[1].startswith("enc:v1:")
    assert db.list_api_keys(include_secret=True)[0]["key"] == "sk-cb-plaintext-legacy"
    credential_crypto.reset_cache()


def test_windows_start_script_bootstraps_portable_buddy2api_environment():
    script = (Path(__file__).parents[1] / "start.bat").read_bytes().decode("ascii")

    assert "create -n buddy2api python=3.12 -y" in script
    assert "run -n buddy2api python -m pip install -r requirements.txt" in script
    assert "run --no-capture-output -n buddy2api python server.py" in script
    assert "-m venv .venv" in script


def test_record_request_updates_log_and_counters_once(isolated_db):
    account_id = db.add_account({"name": "account", "access_token": "a", "refresh_token": "r"})
    key_id = db.add_api_key("sk-cb-test", "key")
    db.record_request(
        {
            "api_key_id": key_id,
            "api_key_name": "key",
            "account_id": account_id,
            "account_name": "account",
            "model": "auto",
            "total_tokens": 7,
            "credit": 0.25,
            "status_code": 200,
            "finish_reason": "stop",
        }
    )
    account = db.get_account(account_id)
    key = db.get_api_key_by_key("sk-cb-test")
    assert account["total_requests"] == 1
    assert account["total_tokens"] == 7
    assert key["total_requests"] == 1
    assert len(db.list_logs()) == 1
    hourly = db.get_stats()["today"]["hourly"]
    assert len(hourly) == 24
    assert sum(bucket["requests"] for bucket in hourly) == 1
    assert sum(bucket["tokens"] for bucket in hourly) == 7
    assert sum(bucket["credit"] for bucket in hourly) == 0.25


def test_api_auth_fails_closed_without_keys(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "ALLOW_UNAUTHENTICATED_API", False)
    with pytest.raises(HTTPException) as error:
        server._check_client_auth(None, None)
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        (
            server.chat_completions,
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": {"level": "high"},
            },
        ),
        (
            server.resp_responses,
            {
                "model": "auto",
                "input": "hello",
                "reasoning": {"effort": 42},
            },
        ),
    ],
)
def test_http_endpoints_reject_invalid_reasoning_controls(
    monkeypatch,
    endpoint,
    payload,
):
    class FakeRequest:
        async def stream(self):
            yield json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        server,
        "_check_client_auth",
        lambda *_args, **_kwargs: {"default_channel": "workbuddy"},
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(FakeRequest(), "Bearer test", None))

    assert error.value.status_code == 400
    assert error.value.detail["error"]["code"] == "invalid_reasoning_control"


def test_responses_input_image_string_is_preserved():
    flattened = responses._flatten_content(
        [{"type": "input_image", "image_url": "data:image/png;base64,abc"}]
    )
    assert "data:image/png;base64,abc" in flattened


@pytest.mark.parametrize(
    "effort",
    ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
)
def test_responses_to_chat_maps_reasoning_effort_without_mutating_input(effort):
    payload = {
        "model": "deepseek-v4-pro",
        "input": "hello",
        "reasoning": {"effort": effort},
    }
    original = json.loads(json.dumps(payload))

    chat_payload = responses.responses_to_chat(payload)

    assert chat_payload["reasoning_effort"] == effort
    assert payload == original


@pytest.mark.parametrize(
    ("compatibility_fields", "expected"),
    [
        ({"reasoningEffort": "medium"}, "medium"),
        ({"thinking": {"type": "enabled"}}, "high"),
        ({"thinking": {"type": "disabled"}}, "none"),
        ({"enable_thinking": True}, "high"),
        ({"enable_thinking": False}, "none"),
        (
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "xhigh"}},
            "xhigh",
        ),
    ],
)
def test_responses_to_chat_accepts_agent_reasoning_compatibility_forms(
    compatibility_fields,
    expected,
):
    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-pro",
        "input": "hello",
        **compatibility_fields,
    })

    assert chat_payload["reasoning_effort"] == expected


def test_responses_to_chat_maps_reasoning_summary():
    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-pro",
        "input": "hello",
        "reasoning": {"effort": "high", "summary": "detailed"},
    })

    assert chat_payload["reasoning_summary"] == "detailed"


def test_responses_to_chat_preserves_reasoning_budget_extensions():
    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-pro",
        "input": "hello",
        "thinking": {
            "type": "enabled",
            "budget_tokens": 4096,
            "display": "hidden",
        },
    })

    assert chat_payload["reasoning_effort"] == "high"
    assert chat_payload["thinking"] == {
        "budget_tokens": 4096,
        "display": "hidden",
    }


def test_responses_reasoning_effort_overrides_backend_default(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", "high")
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)

    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-pro",
        "input": "hello",
        "reasoning": {"effort": "low"},
    })
    body = proxy.build_backend_body(chat_payload)

    assert body["reasoning_effort"] == "low"


def test_responses_uses_backend_reasoning_default_when_effort_is_omitted(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_DEFAULT_REASONING_EFFORT", raising=False)
    monkeypatch.setattr(proxy, "resolve_model_alias", lambda value: value)

    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-flash",
        "input": "hello",
    })
    body = proxy.build_backend_body(chat_payload)

    assert body["reasoning_effort"] == "high"


def test_responses_to_chat_accepts_top_level_reasoning_effort():
    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-pro",
        "input": "hello",
        "reasoning_effort": "max",
    })

    assert chat_payload["reasoning_effort"] == "max"


def test_responses_nested_reasoning_effort_takes_precedence():
    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-pro",
        "input": "hello",
        "reasoning": {"effort": "low"},
        "reasoning_effort": "max",
    })

    assert chat_payload["reasoning_effort"] == "low"


def test_responses_nested_disable_overrides_top_level_enable():
    chat_payload = responses.responses_to_chat({
        "model": "deepseek-v4-pro",
        "input": "hello",
        "reasoning": {"effort": "none"},
        "reasoning_effort": "high",
    })

    assert chat_payload["reasoning_effort"] == "none"


def test_chat_top_level_disable_overrides_nested_enable():
    chat_payload = reasoning_controls.normalize_chat_reasoning({
        "reasoning_effort": "none",
        "reasoning": {"effort": "high"},
    })

    assert chat_payload["reasoning_effort"] == "none"


def test_responses_to_chat_groups_parallel_function_calls_across_reasoning_items():
    payload = {
        "model": "test-model",
        "input": [
            {"type": "message", "role": "user", "content": "Run both tools"},
            {
                "type": "function_call",
                "call_id": "call_first",
                "name": "first_tool",
                "arguments": '{"value":1}',
            },
            {"type": "reasoning", "summary": []},
            {
                "type": "function_call",
                "call_id": "call_second",
                "name": "second_tool",
                "arguments": {"value": 2},
            },
            {
                "type": "function_call_output",
                "call_id": "call_first",
                "output": {"ok": True},
            },
            {
                "type": "function_call_output",
                "call_id": "call_second",
                "output": "done",
            },
        ],
    }

    messages = responses.responses_to_chat(payload)["messages"]

    assert [message["role"] for message in messages] == ["user", "assistant", "tool", "tool"]
    assert [call["id"] for call in messages[1]["tool_calls"]] == ["call_first", "call_second"]
    assert messages[1]["tool_calls"][1]["function"]["arguments"] == '{"value": 2}'
    assert [message["tool_call_id"] for message in messages[2:]] == ["call_first", "call_second"]


def test_responses_to_chat_keeps_sequential_function_call_turns_separate():
    payload = {
        "input": [
            {
                "type": "function_call",
                "call_id": "call_first",
                "name": "first_tool",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_first",
                "output": "first result",
            },
            {
                "type": "function_call",
                "call_id": "call_second",
                "name": "second_tool",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_second",
                "output": "second result",
            },
        ],
    }

    messages = responses.responses_to_chat(payload)["messages"]

    assert [message["role"] for message in messages] == ["assistant", "tool", "assistant", "tool"]
    assert [messages[0]["tool_calls"][0]["id"], messages[2]["tool_calls"][0]["id"]] == [
        "call_first",
        "call_second",
    ]


def test_responses_stream_reassembles_byte_split_tool_arguments():
    first_args = '{"command":"echo 中'
    second_args = '文"}'
    wire = b"".join([
        _chat_sse({
            "model": "upstream-model",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_shell",
                    "type": "function",
                    "function": {
                        "name": "shell_command",
                        "arguments": first_args,
                    },
                }]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"arguments": second_args},
                }]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }),
        b"data: [DONE]\n\n",
    ])

    events = _collect_response_events([bytes([value]) for value in wire])
    names = [name for name, _ in events]
    deltas = _events_of_type(events, "response.function_call_arguments.delta")
    done = _events_of_type(events, "response.function_call_arguments.done")
    added = _events_of_type(events, "response.output_item.added")
    completed = _events_of_type(events, "response.completed")

    assert [delta["delta"] for delta in deltas] == [first_args, second_args]
    assert not _events_of_type(events, "response.output_text.delta")
    assert len(added) == len(done) == len(completed) == 1
    item_id = added[0]["item"]["id"]
    output_index = added[0]["output_index"]
    assert added[0]["item"]["call_id"] == "call_shell"
    assert added[0]["item"]["name"] == "shell_command"
    assert all(delta["item_id"] == item_id for delta in deltas)
    assert all(delta["output_index"] == output_index for delta in deltas)
    assert done[0]["item_id"] == item_id
    assert json.loads(done[0]["arguments"]) == {"command": "echo 中文"}
    assert completed[0]["response"]["model"] == "upstream-model"
    assert completed[0]["response"]["usage"]["total_tokens"] == 7
    assert names.index("response.function_call_arguments.done") < names.index("response.output_item.done")
    assert names[-1] == "response.completed"
    sequence_numbers = [payload["sequence_number"] for _, payload in events]
    assert sequence_numbers == sorted(sequence_numbers)
    assert len(sequence_numbers) == len(set(sequence_numbers))


def test_responses_stream_snapshots_include_request_fields_and_usage_details():
    request_tool = {
        "type": "function",
        "name": "lookup",
        "description": "Look up a value",
        "parameters": {"type": "object", "properties": {}},
    }
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"content": "done"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 5},
                "completion_tokens_details": {"reasoning_tokens": 6},
            },
        }),
        b"data: [DONE]\n\n",
    ], {
        "parallel_tool_calls": False,
        "tool_choice": "required",
        "tools": [request_tool],
    })

    for event_name in ("response.created", "response.in_progress", "response.completed"):
        snapshot = _events_of_type(events, event_name)[0]["response"]
        assert snapshot["parallel_tool_calls"] is False
        assert snapshot["tool_choice"] == "required"
        assert snapshot["tools"] == [request_tool]
    usage = _events_of_type(events, "response.completed")[0]["response"]["usage"]
    assert usage["input_tokens_details"] == {"cached_tokens": 5}
    assert usage["output_tokens_details"] == {"reasoning_tokens": 6}


def test_responses_stream_keeps_parallel_tool_calls_separate():
    chunks = [
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {
                        "index": 0,
                        "id": "call_one",
                        "function": {"name": "shell", "arguments": '{"command":"one'},
                    },
                    {
                        "index": 1,
                        "id": "call_two",
                        "function": {"name": "read_file", "arguments": '{"path":"a'},
                    },
                ]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [
                    {"index": 1, "function": {"arguments": '.txt"}'}},
                    {"index": 0, "function": {"arguments": '"}'}},
                ]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }),
        b"data: [DONE]\n\n",
    ]

    events = _collect_response_events(chunks)
    added = _events_of_type(events, "response.output_item.added")
    done = _events_of_type(events, "response.function_call_arguments.done")
    completed = _events_of_type(events, "response.completed")[0]["response"]

    assert len(added) == len(done) == 2
    added_by_call = {event["item"]["call_id"]: event for event in added}
    assert set(added_by_call) == {"call_one", "call_two"}
    assert len({event["item"]["id"] for event in added}) == 2
    assert len({event["output_index"] for event in added}) == 2
    done_by_item = {event["item_id"]: json.loads(event["arguments"]) for event in done}
    assert done_by_item[added_by_call["call_one"]["item"]["id"]] == {"command": "one"}
    assert done_by_item[added_by_call["call_two"]["item"]["id"]] == {"path": "a.txt"}
    output_by_call = {item["call_id"]: item for item in completed["output"]}
    assert json.loads(output_by_call["call_one"]["arguments"]) == {"command": "one"}
    assert json.loads(output_by_call["call_two"]["arguments"]) == {"path": "a.txt"}
    assert all(item["status"] == "completed" for item in completed["output"])


def test_responses_stream_completes_on_terminal_finish_reason_at_eof():
    tool_event = _chat_sse({
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_eof",
                "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
            }]},
            "finish_reason": None,
        }],
    })
    finish_event = _chat_sse({
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }).rstrip(b"\n")

    events = _collect_response_events([tool_event, finish_event])
    assert len(_events_of_type(events, "response.function_call_arguments.done")) == 1
    assert len(_events_of_type(events, "response.completed")) == 1
    assert not _events_of_type(events, "response.failed")


def test_responses_stream_fails_on_unexpected_eof_with_partial_tool_call():
    events = _collect_response_events([_chat_sse({
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_partial",
                "function": {"name": "shell", "arguments": '{"command":"git'},
            }]},
            "finish_reason": None,
        }],
    })])

    failed = _events_of_type(events, "response.failed")
    output_done = _events_of_type(events, "response.output_item.done")
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "server_error"
    assert output_done[0]["item"]["status"] == "incomplete"
    assert not _events_of_type(events, "response.function_call_arguments.done")
    assert not _events_of_type(events, "response.completed")


def test_responses_stream_propagates_upstream_error_as_failed():
    events = _collect_response_events([
        _chat_sse({"error": {
            "message": "rate limited",
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }}),
        b"data: [DONE]\n\n",
    ])

    failed = _events_of_type(events, "response.failed")
    assert len(failed) == 1
    assert failed[0]["response"]["status"] == "failed"
    assert failed[0]["response"]["error"] == {
        "code": "rate_limit_exceeded",
        "message": "rate limited",
    }
    assert not _events_of_type(events, "response.completed")


@pytest.mark.parametrize(
    ("finish_reason", "incomplete_reason"),
    [("length", "max_output_tokens"), ("content_filter", "content_filter")],
)
def test_responses_stream_marks_truncated_tool_call_incomplete(
    finish_reason,
    incomplete_reason,
):
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_truncated",
                    "function": {"name": "shell", "arguments": '{"command":"git'},
                }]},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }),
        b"data: [DONE]\n\n",
    ])

    incomplete = _events_of_type(events, "response.incomplete")
    output_done = _events_of_type(events, "response.output_item.done")
    assert len(incomplete) == 1
    assert incomplete[0]["response"]["incomplete_details"] == {
        "reason": incomplete_reason,
    }
    assert output_done[0]["item"]["status"] == "incomplete"
    assert not _events_of_type(events, "response.function_call_arguments.done")
    assert not _events_of_type(events, "response.completed")


def test_responses_stream_emits_complete_text_lifecycle():
    events = _collect_response_events([
        _chat_sse({
            "choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}],
        }),
        _chat_sse({
            "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}],
        }),
        b"data: [DO",
        b"NE]\n\n",
    ])

    names = [name for name, _ in events]
    assert names.count("response.output_text.done") == 1
    assert names.count("response.content_part.done") == 1
    assert names.count("response.output_item.done") == 1
    assert names.count("response.completed") == 1
    assert names[-1] == "response.completed"
    completed = _events_of_type(events, "response.completed")[0]["response"]
    assert completed["output"][0]["content"][0]["text"] == "hello"


def test_responses_stream_orders_reasoning_before_text_and_tool():
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "think-"},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "more", "content": "answer"},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_test",
                    "function": {"name": "test_tool", "arguments": '{"ok":true}'},
                }]},
                "finish_reason": "tool_calls",
            }],
        }),
        b"data: [DONE]\n\n",
    ])

    names = [name for name, _ in events]
    added = _events_of_type(events, "response.output_item.added")
    assert [item["item"]["type"] for item in added] == [
        "reasoning",
        "message",
        "function_call",
    ]
    assert [item["output_index"] for item in added] == [0, 1, 2]

    reasoning_id = added[0]["item"]["id"]
    reasoning_events = [
        payload
        for name, payload in events
        if name.startswith("response.reasoning_summary_")
    ]
    assert all(event["item_id"] == reasoning_id for event in reasoning_events)
    assert all(event["output_index"] == 0 for event in reasoning_events)
    assert all(event["summary_index"] == 0 for event in reasoning_events)
    assert [
        event["delta"]
        for event in _events_of_type(events, "response.reasoning_summary_text.delta")
    ] == ["think-", "more"]
    assert _events_of_type(events, "response.reasoning_summary_text.done")[0]["text"] == "think-more"

    assert names.index("response.reasoning_summary_text.done") < names.index(
        "response.reasoning_summary_part.done"
    )
    reasoning_done_index = next(
        index
        for index, (name, payload) in enumerate(events)
        if name == "response.output_item.done" and payload["item"]["type"] == "reasoning"
    )
    message_added_index = next(
        index
        for index, (name, payload) in enumerate(events)
        if name == "response.output_item.added" and payload["item"]["type"] == "message"
    )
    message_done_index = next(
        index
        for index, (name, payload) in enumerate(events)
        if name == "response.output_item.done" and payload["item"]["type"] == "message"
    )
    tool_added_index = next(
        index
        for index, (name, payload) in enumerate(events)
        if name == "response.output_item.added" and payload["item"]["type"] == "function_call"
    )
    assert reasoning_done_index < message_added_index
    assert message_done_index < tool_added_index

    completed = _events_of_type(events, "response.completed")[0]["response"]
    assert [item["type"] for item in completed["output"]] == [
        "reasoning",
        "message",
        "function_call",
    ]
    assert completed["output"][0]["summary"][0]["text"] == "think-more"
    sequence_numbers = [payload["sequence_number"] for _, payload in events]
    assert sequence_numbers == list(range(len(events)))


def test_responses_stream_accepts_reasoning_only_completed_choice():
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "internal "},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "only"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
    ])

    assert not _events_of_type(events, "response.failed")
    assert not _events_of_type(events, "response.output_text.delta")
    completed = _events_of_type(events, "response.completed")[0]["response"]
    assert [item["type"] for item in completed["output"]] == ["reasoning"]
    assert completed["output"][0]["summary"] == [
        {"type": "summary_text", "text": "internal only"}
    ]
    names = [name for name, _ in events]
    assert names.count("response.reasoning_summary_text.done") == 1
    assert names.count("response.reasoning_summary_part.done") == 1
    assert names.count("response.output_item.done") == 1
    assert names.index("response.reasoning_summary_text.done") < names.index(
        "response.reasoning_summary_part.done"
    ) < names.index("response.output_item.done") < names.index("response.completed")


def test_responses_stream_deduplicates_cross_chunk_reasoning_fallback():
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "same text"},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"content": "same text"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
    ])

    completed = _events_of_type(events, "response.completed")[0]["response"]
    assert [item["type"] for item in completed["output"]] == ["reasoning"]
    assert not _events_of_type(events, "response.output_text.delta")


@pytest.mark.parametrize(
    ("finish_reason", "incomplete_reason"),
    [("length", "max_output_tokens"), ("content_filter", "content_filter")],
)
def test_responses_stream_closes_reasoning_when_incomplete(
    finish_reason,
    incomplete_reason,
):
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "partial reasoning"},
                "finish_reason": finish_reason,
            }],
        }),
        b"data: [DONE]\n\n",
    ])

    output_done = _events_of_type(events, "response.output_item.done")[0]
    incomplete = _events_of_type(events, "response.incomplete")[0]["response"]
    names = [name for name, _ in events]
    assert output_done["item"]["status"] == "incomplete"
    assert output_done["item"]["summary"][0]["text"] == "partial reasoning"
    assert incomplete["incomplete_details"] == {"reason": incomplete_reason}
    assert _events_of_type(events, "response.reasoning_summary_text.done")[0]["text"] == (
        "partial reasoning"
    )
    assert names.index("response.reasoning_summary_text.done") < names.index(
        "response.reasoning_summary_part.done"
    ) < names.index("response.output_item.done") < names.index("response.incomplete")
    assert not _events_of_type(events, "response.failed")


@pytest.mark.parametrize(
    "tail",
    [
        _chat_sse({"error": {"message": "failed", "code": "upstream_failed"}}),
        b"",
    ],
)
def test_responses_stream_closes_reasoning_before_failure(tail):
    chunks = [_chat_sse({
        "choices": [{
            "index": 0,
            "delta": {"reasoning_content": "partial reasoning"},
            "finish_reason": None,
        }],
    })]
    if tail:
        chunks.append(tail)
    events = _collect_response_events(chunks)

    names = [name for name, _ in events]
    output_done = _events_of_type(events, "response.output_item.done")[0]
    failed = _events_of_type(events, "response.failed")[0]["response"]
    assert output_done["item"]["status"] == "incomplete"
    assert failed["error"]["code"] == "server_error"
    assert names.index("response.reasoning_summary_text.done") < names.index(
        "response.reasoning_summary_part.done"
    ) < names.index("response.output_item.done") < names.index("response.failed")


def test_responses_stream_keeps_reasoning_choices_separate():
    events = _collect_response_events([
        _chat_sse({
            "choices": [
                {"index": 1, "delta": {"reasoning_content": "one"}, "finish_reason": "stop"},
                {"index": 0, "delta": {"reasoning_content": "zero-a"}, "finish_reason": None},
            ],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "zero-b"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
    ])

    added = [
        payload
        for payload in _events_of_type(events, "response.output_item.added")
        if payload["item"]["type"] == "reasoning"
    ]
    assert len(added) == 2
    assert len({payload["item"]["id"] for payload in added}) == 2
    assert [payload["output_index"] for payload in added] == [0, 1]
    completed = _events_of_type(events, "response.completed")[0]["response"]
    assert [item["summary"][0]["text"] for item in completed["output"]] == [
        "one",
        "zero-azero-b",
    ]
    assert len(_events_of_type(events, "response.reasoning_summary_text.done")) == 2
    assert len(_events_of_type(events, "response.reasoning_summary_part.done")) == 2


@pytest.mark.parametrize(
    ("first_delta", "output_type"),
    [
        ({"content": "answer started"}, "message"),
        ({
            "tool_calls": [{
                "index": 0,
                "id": "call_started",
                "function": {"name": "tool", "arguments": "{}"},
            }],
        }, "function_call"),
    ],
)
def test_responses_stream_rejects_reasoning_after_output_started(
    first_delta,
    output_type,
):
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": first_delta,
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "too late"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
    ])

    failed = _events_of_type(events, "response.failed")[0]["response"]
    assert failed["error"]["code"] == "server_error"
    assert "reasoning after answer or tool output" in failed["error"]["message"]
    assert [item["type"] for item in failed["output"]] == [output_type]
    assert failed["output"][0]["status"] == "incomplete"
    assert not _events_of_type(events, "response.reasoning_summary_text.delta")


def test_responses_stream_coerces_truthy_non_string_reasoning_delta():
    events = _collect_response_events([
        _chat_sse({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": 42},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
    ])

    delta = _events_of_type(events, "response.reasoning_summary_text.delta")[0]
    completed = _events_of_type(events, "response.completed")[0]["response"]
    assert delta["delta"] == "42"
    assert completed["output"][0]["summary"][0]["text"] == "42"


@pytest.mark.parametrize(
    ("delta", "finish_reason"),
    [
        ({}, "stop"),
        ({}, None),
    ],
)
def test_responses_stream_rejects_completed_choice_without_output(delta, finish_reason):
    events = _collect_response_events([
        _chat_sse({
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }),
        b"data: [DONE]\n\n",
    ])

    failed = _events_of_type(events, "response.failed")
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "server_error"
    assert not _events_of_type(events, "response.completed")


def test_responses_stream_fails_when_only_one_choice_finishes_before_eof():
    events = _collect_response_events([
        _chat_sse({
            "choices": [
                {"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"},
                {"index": 1, "delta": {"content": "partial"}, "finish_reason": None},
            ],
        }),
    ])

    failed = _events_of_type(events, "response.failed")
    assert len(failed) == 1
    assert failed[0]["response"]["error"]["code"] == "server_error"
    assert not _events_of_type(events, "response.completed")


def test_responses_stream_supports_multiline_data_and_cr_line_endings():
    payload = json.dumps({
        "choices": [{
            "index": 0,
            "delta": {"content": "hello"},
            "finish_reason": "stop",
        }],
    }, separators=(",", ":"))
    first, second = payload[:1], payload[1:]
    wire = (
        ": keepalive\r\n"
        "event: message\r\n"
        f"data: {first}\r\n"
        f"data: {second}\r\n"
        "\r\n"
        "data: [DONE]\r\r"
    ).encode("utf-8")

    events = _collect_response_events([bytes([value]) for value in wire])
    completed = _events_of_type(events, "response.completed")
    assert len(completed) == 1
    assert completed[0]["response"]["output"][0]["content"][0]["text"] == "hello"
    assert not _events_of_type(events, "response.failed")


def test_chat_proxy_stream_does_not_duplicate_terminal_events(monkeypatch):
    chunks = [
        _chat_sse({
            "id": "chatcmpl-complete",
            "object": "chat.completion.chunk",
            "created": 123,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "complete"},
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-complete",
            "object": "chat.completion.chunk",
            "created": 123,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
        b"data: [DONE]\n\n",
    ]

    raw = _collect_chat_proxy_stream(chunks, monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)
    finish_reasons = [
        choice["finish_reason"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ]

    assert finish_reasons == ["stop"]
    assert done_count == 1


def test_chat_proxy_stream_rejects_terminal_without_content(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-empty",
            "object": "chat.completion.chunk",
            "created": 123,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
        b"data: [DONE]\n\n",
    ], monkeypatch)

    _assert_chat_proxy_error_only(raw)


def test_chat_proxy_stream_accepts_reasoning_only_terminal(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-reasoning",
            "object": "chat.completion.chunk",
            "created": 123,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": "internal only"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
    ], monkeypatch)

    payloads, done_count = _parse_chat_proxy_sse(raw)
    assert not any(payload.get("error") for payload in payloads)
    assert payloads[0]["choices"][0]["delta"]["reasoning_content"] == "internal only"
    assert done_count == 1


def test_non_stream_chat_conversion_rejects_empty_completed_response():
    converted = responses.chat_response_to_responses({
        "id": "chatcmpl-empty",
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None},
            "finish_reason": "stop",
        }],
        "usage": {},
    }, "test-model")

    assert converted["status"] == "failed"
    assert converted["output"] == []
    assert converted["error"]["code"] == "server_error"


def test_non_stream_chat_conversion_orders_reasoning_before_text():
    converted = responses.chat_response_to_responses({
        "id": "chatcmpl-reasoning",
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "reasoning_content": "analysis",
                "content": "answer",
            },
            "finish_reason": "stop",
        }],
        "usage": {},
    }, "test-model")

    assert converted["status"] == "completed"
    assert [item["type"] for item in converted["output"]] == ["reasoning", "message"]
    assert converted["output"][0]["summary"][0]["text"] == "analysis"


def test_non_stream_chat_conversion_includes_required_response_fields_and_usage_details():
    converted = responses.chat_response_to_responses({
        "id": "chatcmpl-details",
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "answer"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 9,
            "completion_tokens": 4,
            "total_tokens": 13,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }, "test-model", {
        "parallel_tool_calls": False,
        "tool_choice": "none",
        "tools": [],
    })

    assert converted["parallel_tool_calls"] is False
    assert converted["tool_choice"] == "none"
    assert converted["tools"] == []
    assert converted["usage"]["input_tokens_details"] == {"cached_tokens": 3}
    assert converted["usage"]["output_tokens_details"] == {"reasoning_tokens": 2}


def test_non_stream_chat_conversion_deduplicates_reasoning_fallback_content():
    converted = responses.chat_response_to_responses({
        "id": "chatcmpl-reasoning",
        "model": "test-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "reasoning_content": "analysis",
                "content": "analysis",
            },
            "finish_reason": "stop",
        }],
        "usage": {},
    }, "test-model")

    assert [item["type"] for item in converted["output"]] == ["reasoning"]


def _collect_non_stream_upstream(monkeypatch, delta):
    payload = {
        "id": "chatcmpl-empty",
        "object": "chat.completion.chunk",
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": "stop"}],
    }

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def aiter_lines(self):
            yield "data: " + json.dumps(payload)
            yield "data: [DONE]"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(proxy.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(auth_manager, "request_timeout", lambda _default: 30)

    return asyncio.run(proxy._collect_stream(
        "https://upstream.test/v2/chat/completions",
        {"Authorization": "Bearer test"},
        {"model": "test-model", "stream": True},
        {"id": 1, "name": "test-account"},
        None,
        "test-model",
        0,
    ))


def test_non_stream_aggregator_rejects_terminal_without_output(monkeypatch):
    result = _collect_non_stream_upstream(monkeypatch, {})

    assert result[0] == "error"
    assert result[1][0] == 502
    assert "without content" in result[1][1]["error"]["message"]


def test_non_stream_aggregator_accepts_reasoning_only_output(monkeypatch):
    result = _collect_non_stream_upstream(
        monkeypatch,
        {"reasoning_content": "internal only"},
    )

    assert result[0] == "json"
    message = result[1]["choices"][0]["message"]
    assert message["content"] is None
    assert message["reasoning_content"] == "internal only"


def test_chat_proxy_stream_normalizes_empty_finish_reason(monkeypatch):
    chunks = [
        _chat_sse({
            "id": "chatcmpl-empty-finish",
            "object": "chat.completion.chunk",
            "created": 124,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "complete"},
                "finish_reason": "",
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-empty-finish",
            "object": "chat.completion.chunk",
            "created": 124,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
        b"data: [DONE]\n\n",
    ]

    raw = _collect_chat_proxy_stream(chunks, monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert payloads[0]["choices"][0]["finish_reason"] is None
    assert payloads[1]["choices"][0]["finish_reason"] == "stop"
    assert not any(payload.get("error") for payload in payloads)
    assert done_count == 1


def test_chat_proxy_stream_ignores_empty_repeated_tool_name(monkeypatch):
    chunks = [
        _chat_sse({
            "id": "chatcmpl-empty-tool-name",
            "object": "chat.completion.chunk",
            "created": 125,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call_echo",
                    "type": "function",
                    "function": {"name": "echo_value", "arguments": ""},
                }]},
                "finish_reason": "",
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-empty-tool-name",
            "object": "chat.completion.chunk",
            "created": 125,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"name": "", "arguments": '{"value":'},
                }]},
                "finish_reason": "",
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-empty-tool-name",
            "object": "chat.completion.chunk",
            "created": 125,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": 0,
                    "function": {"name": "", "arguments": '"test"}'},
                }]},
                "finish_reason": "",
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-empty-tool-name",
            "object": "chat.completion.chunk",
            "created": 125,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }),
        b"data: [DONE]\n\n",
    ]

    raw = _collect_chat_proxy_stream(chunks, monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)
    tool_deltas = [
        tool_call
        for payload in payloads
        for choice in payload.get("choices") or []
        for tool_call in (choice.get("delta") or {}).get("tool_calls") or []
    ]

    assert not any(payload.get("error") for payload in payloads)
    assert [
        tool_call["function"]["name"]
        for tool_call in tool_deltas
        if "name" in tool_call["function"]
    ] == ["echo_value"]
    arguments = "".join(tool_call["function"].get("arguments", "") for tool_call in tool_deltas)
    assert json.loads(arguments) == {"value": "test"}
    assert [
        choice["finish_reason"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ] == ["tool_calls"]
    assert done_count == 1


def test_chat_proxy_stream_adds_done_after_explicit_terminal_at_eof(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-terminal-eof",
            "object": "chat.completion.chunk",
            "created": 321,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "complete"},
                "finish_reason": "stop",
            }],
        }),
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert [
        choice["finish_reason"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ] == ["stop"]
    assert done_count == 1


def test_chat_proxy_stream_synthesizes_terminal_for_complete_tool_at_clean_eof(
    monkeypatch,
):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-eof",
            "object": "chat.completion.chunk",
            "created": 456,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_complete",
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
                    }],
                },
                "finish_reason": None,
            }],
        }),
    ], monkeypatch)

    payloads, done_count = _parse_chat_proxy_sse(raw)
    terminal_choices = [
        choice
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ]

    assert len(terminal_choices) == 1
    assert terminal_choices[0]["index"] == 0
    assert terminal_choices[0]["delta"] == {}
    assert terminal_choices[0]["finish_reason"] == "tool_calls"
    assert done_count == 1


def test_chat_proxy_stream_rejects_plain_text_without_terminal_at_eof(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-text-eof",
            "object": "chat.completion.chunk",
            "created": 457,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "possibly incomplete"},
                "finish_reason": None,
            }],
        }),
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


@pytest.mark.parametrize(
    "arguments",
    [
        '{"command":"pwd"',
        '{"command":pwd}',
        '[]',
    ],
)
def test_chat_proxy_stream_rejects_invalid_tool_arguments_at_eof(
    monkeypatch,
    arguments,
):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-invalid-tool",
            "object": "chat.completion.chunk",
            "created": 789,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_invalid",
                        "type": "function",
                        "function": {"name": "shell", "arguments": arguments},
                    }],
                },
                "finish_reason": None,
            }],
        }),
    ], monkeypatch)

    payloads, done_count = _parse_chat_proxy_sse(raw)
    errors = [payload["error"] for payload in payloads if payload.get("error")]
    terminal_choices = [
        choice
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ]

    assert len(errors) == 1
    assert errors[0]["message"]
    assert terminal_choices == []
    assert done_count == 1


@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [b": keepalive\n\n", b": still-alive\r\n\r\n"],
    ],
    ids=["empty", "comments-only"],
)
def test_chat_proxy_stream_rejects_empty_or_comment_only_streams(
    monkeypatch,
    chunks,
):
    raw = _collect_chat_proxy_stream(chunks, monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


@pytest.mark.parametrize(
    "chunks",
    [
        [b"data: {not-json}\n\n"],
        [b'data: {"choices":[{"index":0'],
        [b"data: \xff\n\n"],
    ],
    ids=["malformed-json", "partial-frame", "invalid-utf8"],
)
def test_chat_proxy_stream_rejects_malformed_sse_at_eof(monkeypatch, chunks):
    raw = _collect_chat_proxy_stream(chunks, monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


def test_chat_proxy_stream_decoder_reassembles_byte_split_utf8(monkeypatch):
    wire = b"".join([
        _chat_sse({
            "id": "chatcmpl-byte-split",
            "object": "chat.completion.chunk",
            "created": 793,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "逐字节中文"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
    ])
    raw = _collect_chat_proxy_stream([bytes([value]) for value in wire], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert [
        choice["delta"]["content"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if (choice.get("delta") or {}).get("content")
    ] == ["逐字节中文"]
    assert not any(payload.get("error") for payload in payloads)
    assert done_count == 1


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_chat_proxy_stream_decoder_supports_sse_line_endings(
    monkeypatch,
    line_ending,
):
    payload = json.dumps({
        "id": "chatcmpl-line-ending",
        "object": "chat.completion.chunk",
        "created": 794,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "delta": {"content": "line ending"},
            "finish_reason": "stop",
        }],
    }, separators=(",", ":"))
    wire = (
        f"data: {payload}{line_ending}{line_ending}"
        f"data: [DONE]{line_ending}{line_ending}"
    ).encode("utf-8")
    raw = _collect_chat_proxy_stream([wire], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert [
        choice["delta"]["content"]
        for event in payloads
        for choice in event.get("choices") or []
        if (choice.get("delta") or {}).get("content")
    ] == ["line ending"]
    assert not any(event.get("error") for event in payloads)
    assert done_count == 1


def test_chat_proxy_stream_decoder_supports_multiline_data(monkeypatch):
    payload = json.dumps({
        "id": "chatcmpl-multiline",
        "object": "chat.completion.chunk",
        "created": 795,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "delta": {"content": "multiline"},
            "finish_reason": "stop",
        }],
    }, separators=(",", ":"))
    split_at = payload.index('"choices"')
    wire = (
        "event: message\r\n"
        f"data: {payload[:split_at]}\r\n"
        f"data: {payload[split_at:]}\r\n"
        "\r\n"
        "data: [DONE]\r\n\r\n"
    ).encode("utf-8")
    raw = _collect_chat_proxy_stream([bytes([value]) for value in wire], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert [
        choice["delta"]["content"]
        for event in payloads
        for choice in event.get("choices") or []
        if (choice.get("delta") or {}).get("content")
    ] == ["multiline"]
    assert not any(event.get("error") for event in payloads)
    assert done_count == 1


@pytest.mark.parametrize(
    "wire",
    [
        b"data: " + (b"x" * 65) + b"\n\n",
        b"data: " + (b"x" * 40) + b"\ndata: " + (b"y" * 40) + b"\n\n",
    ],
    ids=["line-limit", "event-limit"],
)
def test_chat_proxy_stream_decoder_enforces_eight_mib_limits(
    monkeypatch,
    wire,
):
    assert proxy._MAX_SSE_EVENT_BYTES == 8 * 1024 * 1024
    monkeypatch.setattr(proxy, "_MAX_SSE_EVENT_BYTES", 64)

    raw = _collect_chat_proxy_stream([wire], monkeypatch)

    _assert_chat_proxy_error_only(raw)


def test_chat_proxy_stream_done_ignores_oversized_trailing_line(monkeypatch):
    monkeypatch.setattr(proxy, "_MAX_SSE_EVENT_BYTES", 512)
    wire = b"".join([
        _chat_sse({
            "id": "chatcmpl-done-before-garbage",
            "object": "chat.completion.chunk",
            "created": 796,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "done"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
        b"x" * 513,
        b"\n\n",
    ])

    raw = _collect_chat_proxy_stream([wire], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert not any(payload.get("error") for payload in payloads)
    assert [
        choice["finish_reason"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ] == ["stop"]
    assert done_count == 1


@pytest.mark.parametrize(
    "choices",
    [
        1,
        ["not-a-choice"],
        [{"index": "0", "delta": {"content": "invalid"}, "finish_reason": None}],
        [{"index": 0, "delta": "not-a-delta", "finish_reason": None}],
    ],
    ids=["choices-not-list", "choice-not-object", "invalid-index", "delta-not-object"],
)
def test_chat_proxy_stream_rejects_invalid_chunk_schema_without_leaking(
    monkeypatch,
    choices,
):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-invalid-schema",
            "object": "chat.completion.chunk",
            "created": 790,
            "model": "test-model",
            "choices": choices,
        }),
    ], monkeypatch)

    _assert_chat_proxy_error_only(raw)


@pytest.mark.parametrize(
    ("finish_reason", "trailing_delta"),
    [
        ("stop", {"content": "late content"}),
        ("tool_calls", {
            "tool_calls": [{
                "index": 0,
                "id": "call_late",
                "type": "function",
                "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
            }],
        }),
    ],
    ids=["content-after-stop", "tool-after-tool-terminal"],
)
def test_chat_proxy_stream_rejects_same_choice_delta_after_terminal(
    monkeypatch,
    finish_reason,
    trailing_delta,
):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-early-terminal",
            "object": "chat.completion.chunk",
            "created": 791,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-early-terminal",
            "object": "chat.completion.chunk",
            "created": 791,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": trailing_delta,
                "finish_reason": None,
            }],
        }),
        b"data: [DONE]\n\n",
    ], monkeypatch)

    _assert_chat_proxy_error_only(raw)


def test_chat_proxy_stream_rejects_complete_tool_call_with_stop(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-tool-stop",
            "object": "chat.completion.chunk",
            "created": 792,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_stop",
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
                    }],
                },
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-tool-stop",
            "object": "chat.completion.chunk",
            "created": 792,
            "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
        b"data: [DONE]\n\n",
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


def test_chat_proxy_stream_preserves_http_200_error_event(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "error": {
                "message": "upstream overloaded",
                "type": "server_error",
                "code": "overloaded",
            },
        }),
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)
    errors = [payload["error"] for payload in payloads if payload.get("error")]

    assert len(errors) == 1
    assert errors[0]["message"] == "upstream overloaded"
    assert not any(payload.get("choices") for payload in payloads)
    assert done_count == 1


def test_chat_proxy_stream_rejects_an_unfinished_choice(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-multiple",
            "object": "chat.completion.chunk",
            "created": 999,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "first"},
                    "finish_reason": "stop",
                },
                {
                    "index": 1,
                    "delta": {"content": "second"},
                    "finish_reason": None,
                },
            ],
        }),
    ], monkeypatch, body={"model": "test-model", "stream": True, "n": 2})
    payloads, done_count = _parse_chat_proxy_sse(raw)
    errors = [payload["error"] for payload in payloads if payload.get("error")]
    terminal_choices = [
        (choice["index"], choice["finish_reason"])
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ]

    assert len(errors) == 1
    assert terminal_choices == []
    assert done_count == 1


def test_chat_proxy_stream_rejects_missing_expected_choice(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-missing-choice",
            "object": "chat.completion.chunk",
            "created": 1000,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "only one"},
                "finish_reason": "stop",
            }],
        }),
    ], monkeypatch, body={"model": "test-model", "stream": True, "n": 2})
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert [
        choice["finish_reason"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ] == []
    assert done_count == 1


def test_chat_proxy_stream_rejects_done_with_unfinished_choice(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-premature-done",
            "object": "chat.completion.chunk",
            "created": 1001,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "unfinished"},
                "finish_reason": None,
            }],
        }),
        b"data: [DONE]\n\n",
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


def test_chat_proxy_stream_discards_terminal_before_http_error(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-network-failure",
            "object": "chat.completion.chunk",
            "created": 1002,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
        }),
    ], monkeypatch, stream_error=proxy.httpx.ReadError("connection dropped"))
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len(payloads) == 1
    assert payloads[0].get("error")
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


def test_chat_proxy_stream_ignores_data_after_done(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-before-done",
            "object": "chat.completion.chunk",
            "created": 1003,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"content": "done"},
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n\n",
        _chat_sse({
            "id": "chatcmpl-after-done",
            "object": "chat.completion.chunk",
            "created": 1004,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls",
            }],
        }),
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert not any(payload.get("error") for payload in payloads)
    assert [
        choice["finish_reason"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ] == ["stop"]
    assert done_count == 1


def test_chat_proxy_stream_rejects_tool_finish_without_tool_delta(monkeypatch):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-missing-tool",
            "object": "chat.completion.chunk",
            "created": 1005,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "tool_calls",
            }],
        }),
        b"data: [DONE]\n\n",
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_chat_proxy_stream_accepts_truncated_tools_with_explicit_finish(
    monkeypatch,
    finish_reason,
):
    raw = _collect_chat_proxy_stream([
        _chat_sse({
            "id": "chatcmpl-truncated-tool",
            "object": "chat.completion.chunk",
            "created": 1006,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_truncated",
                        "type": "function",
                        "function": {"name": "shell", "arguments": '{"command":"git'},
                    }],
                },
                "finish_reason": None,
            }],
        }),
        _chat_sse({
            "id": "chatcmpl-truncated-tool",
            "object": "chat.completion.chunk",
            "created": 1006,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }],
        }),
        b"data: [DONE]\n\n",
    ], monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert not any(payload.get("error") for payload in payloads)
    assert [
        choice["finish_reason"]
        for payload in payloads
        for choice in payload.get("choices") or []
        if choice.get("finish_reason") is not None
    ] == [finish_reason]
    assert done_count == 1


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
@pytest.mark.parametrize(
    "tool_deltas",
    [
        [{
            "index": 0,
            "id": "call_bad_function",
            "type": "function",
            "function": "not-an-object",
        }],
        [{
            "index": 0,
            "id": "call_bad_arguments",
            "type": "function",
            "function": {"name": "shell", "arguments": {"command": "git"}},
        }],
        [
            {
                "index": 0,
                "id": "call_first",
                "type": "function",
                "function": {"name": "shell", "arguments": '{"command":"'},
            },
            {
                "index": 0,
                "id": "call_second",
                "type": "function",
                "function": {"arguments": "git"},
            },
        ],
        [
            {
                "index": 0,
                "id": "call_name_conflict",
                "type": "function",
                "function": {"name": "shell", "arguments": '{"command":"'},
            },
            {
                "index": 0,
                "type": "function",
                "function": {"name": "read_file", "arguments": "git"},
            },
        ],
    ],
    ids=["invalid-function", "invalid-arguments", "conflicting-id", "conflicting-name"],
)
def test_chat_proxy_stream_truncation_rejects_invalid_or_conflicting_tool_fields(
    monkeypatch,
    tool_deltas,
    finish_reason,
):
    chunks = [
        _chat_sse({
            "id": "chatcmpl-invalid-truncated-tool",
            "object": "chat.completion.chunk",
            "created": 1007,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {"tool_calls": [tool_delta]},
                "finish_reason": None,
            }],
        })
        for tool_delta in tool_deltas
    ]
    chunks.extend([
        _chat_sse({
            "id": "chatcmpl-invalid-truncated-tool",
            "object": "chat.completion.chunk",
            "created": 1007,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }],
        }),
        b"data: [DONE]\n\n",
    ])
    raw = _collect_chat_proxy_stream(chunks, monkeypatch)
    payloads, done_count = _parse_chat_proxy_sse(raw)

    assert len([payload for payload in payloads if payload.get("error")]) == 1
    assert not any(
        choice.get("finish_reason")
        for payload in payloads
        for choice in payload.get("choices") or []
    )
    assert done_count == 1


def test_chat_proxy_stream_fails_over_after_preoutput_malformed_stream(monkeypatch):
    accounts = [
        {"id": 1, "name": "malformed-account"},
        {"id": 2, "name": "healthy-account"},
    ]
    streams = {
        1: [b"data: {not-json}\n\n"],
        2: [
            _chat_sse({
                "id": "chatcmpl-second-account",
                "object": "chat.completion.chunk",
                "created": 1008,
                "model": "test-model",
                "choices": [{
                    "index": 0,
                    "delta": {"content": "second account"},
                    "finish_reason": "stop",
                }],
            }),
            b"data: [DONE]\n\n",
        ],
    }
    calls = _install_chat_account_stream_fakes(monkeypatch, accounts, streams)

    async def collect():
        return b"".join([
            chunk
            async for chunk in proxy._stream_upstream(
                {"model": "test-model", "stream": True},
                None,
                "test-model",
            )
        ])

    raw = asyncio.run(collect())
    payloads, done_count = _parse_chat_proxy_sse(raw)
    retry_logs = [entry for entry in calls["logs"] if entry[0][8] == "retry"]
    success_logs = [entry for entry in calls["logs"] if entry[0][9] == 200]

    assert [payload["id"] for payload in payloads] == ["chatcmpl-second-account"]
    assert payloads[0]["choices"][0]["delta"]["content"] == "second account"
    assert done_count == 1
    assert calls["picks"] == [set(), {1}]
    assert calls["failures"] == [(1, 502)]
    assert calls["successes"] == [2]
    assert calls["delays"] == [0]
    assert len(retry_logs) == 1
    assert retry_logs[0][0][1]["id"] == 1
    assert retry_logs[0][1]["increment_usage"] is False
    assert len(success_logs) == 1
    assert success_logs[0][0][1]["id"] == 2


def test_chat_proxy_stream_preserves_final_usage_when_no_failover_account(monkeypatch):
    accounts = [{"id": 1, "name": "only-account"}]
    streams = {1: [
        _chat_sse({
            "id": "chatcmpl-missing-second-choice",
            "object": "chat.completion.chunk",
            "created": 1009,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "credit": 1.25,
            },
        }),
    ]}
    calls = _install_chat_account_stream_fakes(monkeypatch, accounts, streams)

    async def collect():
        return b"".join([
            chunk
            async for chunk in proxy._stream_upstream(
                {"model": "test-model", "stream": True, "n": 2},
                None,
                "test-model",
            )
        ])

    raw = asyncio.run(collect())
    final_error_logs = [entry for entry in calls["logs"] if entry[0][8] == "error"]

    _assert_chat_proxy_error_only(raw)
    assert calls["picks"] == [set(), {1}]
    assert calls["failures"] == [(1, 502)]
    assert calls["successes"] == []
    assert len(final_error_logs) == 1
    assert final_error_logs[0][0][1]["id"] == 1
    assert final_error_logs[0][0][4:8] == (2, 3, 5, 1.25)
    assert final_error_logs[0][0][9] == 502


def test_chat_proxy_stream_records_success_before_terminal_is_consumed(monkeypatch):
    accounts = [{"id": 1, "name": "healthy-account"}]
    streams = {
        1: [
            _chat_sse({
                "id": "chatcmpl-early-stop",
                "object": "chat.completion.chunk",
                "created": 1009,
                "model": "test-model",
                "choices": [{
                    "index": 0,
                    "delta": {"content": "done"},
                    "finish_reason": "stop",
                }],
            }),
            b"data: [DONE]\n\n",
        ],
    }
    calls = _install_chat_account_stream_fakes(monkeypatch, accounts, streams)

    async def consume_first():
        generator = proxy._stream_upstream(
            {"model": "test-model", "stream": True},
            None,
            "test-model",
        )
        first = await anext(generator)
        assert calls["successes"] == [1]
        assert len(calls["logs"]) == 1
        assert calls["logs"][0][0][8] == "stop"
        await generator.aclose()
        return first

    first = asyncio.run(consume_first())
    payloads, done_count = _parse_chat_proxy_sse(first)

    assert payloads[0]["choices"][0]["finish_reason"] == "stop"
    assert done_count == 0
    assert calls["failures"] == []
    assert calls["successes"] == [1]
    assert len(calls["logs"]) == 1
    assert calls["logs"][0][0][8] == "stop"
    assert calls["logs"][0][0][9] == 200


def test_chat_proxy_stream_records_failure_before_error_is_consumed(monkeypatch):
    accounts = [{"id": 1, "name": "only-account"}]
    streams = {1: [b"data: {not-json}\n\n"]}
    calls = _install_chat_account_stream_fakes(monkeypatch, accounts, streams)

    async def consume_first():
        generator = proxy._stream_upstream(
            {"model": "test-model", "stream": True},
            None,
            "test-model",
        )
        first = await anext(generator)
        assert calls["failures"] == [(1, 502)]
        assert len([entry for entry in calls["logs"] if entry[0][8] == "error"]) == 1
        await generator.aclose()
        return first

    first = asyncio.run(consume_first())
    final_error_logs = [entry for entry in calls["logs"] if entry[0][8] == "error"]

    _assert_chat_proxy_error_only(first)
    assert calls["failures"] == [(1, 502)]
    assert calls["successes"] == []
    assert len(final_error_logs) == 1
    assert final_error_logs[0][0][9] == 502


def test_retryable_statuses_are_explicit():
    assert proxy._is_retryable_status(429)
    assert proxy._is_retryable_status(503)
    assert not proxy._is_retryable_status(400)


def test_non_stream_proxy_fails_over_on_retryable_upstream(isolated_db, monkeypatch):
    account_id = db.add_account(
        {
            "name": "account",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    calls = []

    async def pick(exclude):
        calls.append("pick")
        return account

    async def headers(value):
        return {"Authorization": "Bearer access"}

    async def delay(_attempt):
        return None

    async def collect(*_args):
        if len(calls) == 1:
            return ("error", (503, {"error": {"message": "busy"}}))
        return ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", headers)
    monkeypatch.setattr(proxy, "_retry_delay", delay)
    monkeypatch.setattr(proxy, "_collect_stream", collect)
    result = asyncio.run(
        proxy.proxy_chat_completions(
            {"model": "auto", "messages": [{"role": "user", "content": "hello"}]},
            None,
        )
    )
    assert result[0] == "json"
    assert calls.count("pick") == 2


def test_debug_redaction_removes_credentials():
    redacted = responses._redact_debug_value(
        {"api_key": "secret", "messages": [{"content": "private"}]}
    )
    assert redacted["api_key"] == "<redacted>"
    assert redacted["messages"][0]["content"] == "<content redacted>"


def test_valid_headers_is_async_and_uses_decrypted_token(isolated_db):
    account_id = db.add_account(
        {
            "name": "account",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    headers = asyncio.run(__import__("auth_manager").get_valid_headers(account))
    assert headers["Authorization"] == "Bearer access-secret"


def test_codex_sanitize_skips_non_codex_prompts():
    """DSH 等非 Codex prompt 不应被清洗或整体替换。"""
    dsh_prompt = "You are DSH, an autonomous agent. Use tools: sandbox access, execute shell commands, filesystem. " + ("x" * 2000)
    payload = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": dsh_prompt},
            {"role": "user", "content": "run the workflow"},
        ],
        "tools": [{"type": "function", "function": {"name": "bash", "description": "execute shell commands"}}],
    }
    out = responses.apply_codex_sanitize(dict(payload))
    assert out["messages"][0]["content"] == dsh_prompt
    # 工具原样保留（codex 路径会过滤非 function 工具，但这里不触发清洗）
    assert out["tools"][0]["function"]["description"] == "execute shell commands"


def test_codex_sanitize_still_sanitizes_codex_prompts():
    """带 Codex 特征的 prompt 仍走清洗。"""
    codex_prompt = (
        "<permissions instructions>\nFilesystem sandboxing defines which files can be read or written.\n"
        "sandbox_mode=workspace\n</permissions instructions>\n"
        "# Escalation Requests\nYou may request escalation.\n"
        "security policy: do not delete files. " + ("y" * 2000)
    )
    payload = {
        "model": "auto",
        "messages": [{"role": "system", "content": codex_prompt}, {"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "bash", "description": "execute shell commands and sandbox"}}],
    }
    out = responses.apply_codex_sanitize(dict(payload))
    cleaned = out["messages"][0]["content"]
    assert cleaned != codex_prompt
    assert "Filesystem sandboxing" not in cleaned
    assert "security policy" not in cleaned
    # Codex 特征段落被移除/清洗（本例所有特征段被删除后为空串，同样证明清洗生效）
    assert cleaned == "" or "coding assistant" in cleaned


def test_codex_sanitize_passthrough_without_system_prompt():
    """无 system message 时原样返回。"""
    payload = {"model": "auto", "messages": [{"role": "user", "content": "hello"}]}
    out = responses.apply_codex_sanitize(dict(payload))
    assert out == payload


# ============================================================
# 工具停转（tool stall）检测与修复 — issue #31
# ============================================================

def _tool_loop_body():
    return {
        "model": "auto",
        "stream": False,
        "tools": [{"type": "function", "function": {"name": "list_files", "parameters": {"type": "object"}}}],
        "messages": [
            {"role": "user", "content": "列出文件"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "a.txt"},
            {"role": "user", "content": "继续跑流程。"},
        ],
    }


def test_stall_detection_ack_text():
    body = _tool_loop_body()
    assert proxy._request_has_tool_loop(body)
    assert proxy._looks_like_stall_text("好的，马上继续跑流程。")
    assert proxy._is_tool_stall(body, "stop", False, "好的，马上继续跑流程。")


def test_stall_detection_rejects_summary():
    body = _tool_loop_body()
    assert not proxy._looks_like_stall_text("任务完成，总结如下：共处理 3 个文件。")
    assert not proxy._is_tool_stall(body, "stop", False, "任务完成，总结如下：共处理 3 个文件。")


def test_stall_detection_requires_tool_loop_and_tools():
    no_tool_history = {
        "model": "auto",
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
        "messages": [{"role": "user", "content": "继续。"}],
    }
    assert not proxy._request_has_tool_loop(no_tool_history)
    assert not proxy._is_tool_stall(no_tool_history, "stop", False, "好的，马上继续。")
    no_tools = {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "tool", "tool_call_id": "x", "content": "y"},
            {"role": "user", "content": "继续。"},
        ],
    }
    assert not proxy._is_tool_stall(no_tools, "stop", False, "好的，马上继续。")
    assert not proxy._is_tool_stall(_tool_loop_body(), "stop", True, "好的，马上继续。")
    assert not proxy._is_tool_stall(_tool_loop_body(), "length", False, "好的，马上继续。")


def test_stall_retry_nonstream_uses_tool_call_result(monkeypatch, isolated_db):
    """停转时自动以 tool_choice=required 重试，并优先采用重试出的工具调用结果。"""
    stall_json = {
        "id": "c1", "object": "chat.completion", "created": 1,
        "model": "auto",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "好的，马上继续跑流程。"},
                     "finish_reason": "stop"}],
        "usage": {},
    }
    fixed_json = {
        "id": "c2", "object": "chat.completion", "created": 2,
        "model": "auto",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]},
                     "finish_reason": "tool_calls"}],
        "usage": {},
    }

    calls = []

    account = {"id": 1, "name": "test-account"}

    async def pick_account(_excluded):
        return account

    async def valid_headers(_account):
        return {"Authorization": "Bearer test"}

    async def fake_collect(url, headers, body, account, api_key_info, model_name, t0):
        calls.append(body.get("tool_choice"))
        if len(calls) == 1:
            return ("json", stall_json)
        return ("json", fixed_json)

    monkeypatch.setattr(proxy, "_collect_stream", fake_collect)
    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick_account)
    monkeypatch.setattr(auth_manager, "get_valid_headers", valid_headers)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda _id: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_a: None)
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda _default: 30)

    async def run():
        return await proxy.proxy_chat_completions(_tool_loop_body(), None)

    result = asyncio.run(run())
    assert result[0] == "json"
    assert result[1]["choices"][0]["message"].get("tool_calls")
    assert calls == [None, "required"]


def test_stall_retry_nonstream_keeps_first_answer_when_retry_has_no_tools(monkeypatch, isolated_db):
    """重试仍无工具调用时，保留首次的文字回复（例如总结类回答）。"""
    stall_json = {
        "id": "c1", "object": "chat.completion", "created": 1,
        "model": "auto",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "好的，马上继续。"},
                     "finish_reason": "stop"}],
        "usage": {},
    }
    again_json = {
        "id": "c2", "object": "chat.completion", "created": 2,
        "model": "auto",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "好的，马上继续。"},
                     "finish_reason": "stop"}],
        "usage": {},
    }

    calls = []

    account = {"id": 1, "name": "test-account"}

    async def pick_account(_excluded):
        return account

    async def valid_headers(_account):
        return {"Authorization": "Bearer test"}

    async def fake_collect(url, headers, body, account, api_key_info, model_name, t0):
        calls.append(body.get("tool_choice"))
        if len(calls) == 1:
            return ("json", stall_json)
        return ("json", again_json)

    monkeypatch.setattr(proxy, "_collect_stream", fake_collect)
    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", pick_account)
    monkeypatch.setattr(auth_manager, "get_valid_headers", valid_headers)
    monkeypatch.setattr(auth_manager, "mark_account_success", lambda _id: None)
    monkeypatch.setattr(auth_manager, "mark_account_failure", lambda *_a: None)
    monkeypatch.setattr(auth_manager, "backend_url", lambda: "https://upstream.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda _default: 30)

    async def run():
        return await proxy.proxy_chat_completions(_tool_loop_body(), None)

    result = asyncio.run(run())
    assert result[0] == "json"
    assert result[1]["choices"][0]["message"].get("content") == "好的，马上继续。"
    assert calls == [None, "required"]


def _stall_stream_chunks() -> list[bytes]:
    """上游流：纯文本增量 + finish_reason=stop（无任何工具调用）。"""
    return [
        _chat_sse({
            "id": "c1", "object": "chat.completion.chunk", "created": 1,
            "choices": [{"index": 0, "delta": {"content": "好的，马上继续跑流程。"}, "finish_reason": None}],
        }),
        _chat_sse({
            "id": "c1", "object": "chat.completion.chunk", "created": 1,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
        b"data: [DONE]\n\n",
    ]


def test_stream_tool_stall_fails_when_flag_on(monkeypatch, isolated_db):
    """流式停转 + CB_GATEWAY_TOOL_STALL_FAIL_STREAM=1 → 回合标记为失败。"""
    monkeypatch.setattr(proxy, "TOOL_STALL_FAIL_STREAM", True)
    body = _tool_loop_body()
    body["stream"] = True
    raw = _collect_chat_proxy_stream(_stall_stream_chunks(), monkeypatch, body)
    payloads, done_count = _parse_chat_proxy_sse(raw)
    assert done_count == 1
    errors = [p for p in payloads if p.get("error")]
    assert len(errors) == 1
    assert errors[0]["error"]["code"] == "upstream_tool_stall"


def test_stream_tool_stall_passthrough_when_flag_off(monkeypatch, isolated_db):
    """默认（flag off）流式停转原样透传，仅日志标记 tool_stall。"""
    body = _tool_loop_body()
    body["stream"] = True
    raw = _collect_chat_proxy_stream(_stall_stream_chunks(), monkeypatch, body)
    payloads, done_count = _parse_chat_proxy_sse(raw)
    assert done_count == 1
    assert not any(p.get("error") for p in payloads)
    assert any(p.get("choices") and p["choices"][0].get("finish_reason") == "stop" for p in payloads)


def test_stream_tool_stall_is_logged(monkeypatch, isolated_db):
    """流式停转（flag off）时日志 finish_reason 记为 tool_stall。"""
    calls = _install_chat_account_stream_fakes(
        monkeypatch,
        [{"id": 1, "name": "test-account"}],
        {1: _stall_stream_chunks()},
    )
    body = _tool_loop_body()
    body["stream"] = True

    async def collect():
        return b"".join([
            chunk
            async for chunk in proxy._stream_upstream(body, None, "test-model")
        ])

    raw = asyncio.run(collect())
    assert b"tool stall" not in raw
    logged_finishes = [entry[0][8] for entry in calls["logs"]]
    assert "tool_stall" in logged_finishes
