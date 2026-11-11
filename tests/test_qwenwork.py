import asyncio
import hashlib
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

import credential_crypto
import database as db
import providers
import router
from providers.qwenwork import cosy
from providers.qwenwork.chat import (
    build_body,
    envelope_error,
    static_headers,
    translate_model,
    unwrap_sse_payload,
    usage_from_payload,
)
from providers.qwenwork.encode import prepare_infer
from providers.qwenwork.constants import COSY_VERSION, COSY_VERSION_FROZEN, RSA_PUBLIC_KEY_PEM, STATIC_MODELS
from providers.qwenwork.store import parse_credentials, qwenwork_auth_dirs


@pytest.mark.parametrize("stream", [False, True])
def test_chat_encoding_failure_preserves_original_error(monkeypatch, stream):
    from providers.qwenwork import chat
    from providers.qwenwork.encode import EncodeError

    failures = []

    async def pick(tried):
        return {"id": 1}

    def pack(*args):
        raise EncodeError("test encoding failure")

    monkeypatch.setattr(chat, "_pick", pick)
    monkeypatch.setattr(chat, "_packed_request", pack)
    monkeypatch.setattr(chat.auth_manager, "account_attempt_limit", lambda channel: 1)
    monkeypatch.setattr(chat.auth_manager, "mark_account_failure", lambda *args: failures.append(args))
    monkeypatch.setattr(chat, "_log", lambda *args, **kwargs: None)

    async def run():
        kind, result = await chat.chat_completions(
            {"model": "qwork-advanced", "messages": [{"role": "user", "content": "ping"}], "stream": stream},
            None,
        )
        if stream:
            assert kind == "stream"
            assert b"test encoding failure" in await anext(result)
            await result.aclose()
        else:
            assert kind == "error"
            assert result[0] == 503
            assert result[1]["error"]["message"] == "test encoding failure"

    asyncio.run(run())
    assert failures == [(1, 503)]


def test_stream_closes_after_upstream_error(monkeypatch):
    from contextlib import asynccontextmanager
    import httpx
    from providers.qwenwork import chat

    closed = []

    async def pick(tried):
        return {"id": 1}

    @asynccontextmanager
    async def slot(account):
        yield

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append("client")

        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            try:
                yield httpx.Response(400, content=b"upstream rejected request")
            finally:
                closed.append("response")

    monkeypatch.setattr(chat, "_pick", pick)
    monkeypatch.setattr(chat, "_packed_request", lambda *args: ("https://example.test", {}, "{}"))
    monkeypatch.setattr(chat.auth_manager, "account_attempt_limit", lambda channel: 1)
    monkeypatch.setattr(chat.auth_manager, "mark_account_failure", lambda *args: None)
    monkeypatch.setattr(chat.rate_limiter, "account_slot", slot)
    monkeypatch.setattr(chat.httpx, "AsyncClient", Client)

    async def run():
        result = chat._stream("{}", "test", "qwork-advanced", None, "qwork-advanced")
        assert b"upstream rejected request" in await anext(result)
        await result.aclose()

    asyncio.run(run())
    assert closed == ["response", "client"]


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


@pytest.fixture()
def qwen_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def test_qwenwork_in_default_registry(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    assert providers.enabled_provider_ids() == [
        "workbuddy",
        "workbuddy-i",
        "qwenwork",
        "qoder",
        "qoder-i",
        "traework",
        "loomy",
        "xiaohuanxiong",
    ]
    assert providers.get_provider("qwenwork") is not None
    assert "qwenwork" in providers._LOADED


def test_cosy_version_is_frozen():
    assert COSY_VERSION_FROZEN is True
    assert COSY_VERSION == "1.1.32"
    assert RSA_PUBLIC_KEY_PEM.startswith("-----BEGIN PUBLIC KEY-----")


def test_signing_path_strips_algo_and_query():
    assert cosy.signing_path(
        "https://gateway.qwenwork.cn/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=x"
    ) == "/api/v2/service/pro/sse/agent_chat_generation"


def test_cosy_md5_is_stable_with_injected_key():
    material = cosy.encrypt_user_info(
        uid="u1",
        name="n",
        email="e@x",
        access_token="tok",
        aes_key="0123456789abcdef",
        rsa_cipher_b64="RSAKEY",
    )
    token = cosy.generate_auth_token(
        material,
        url="https://gateway.qwenwork.cn/algo/api/v2/x?q=1",
        body="{}",
        timestamp=1700000000,
        request_id="aabbccddeeff00112233445566778899",
    )
    expected = hashlib.md5(token["sign_str"].encode("utf-8")).hexdigest()
    assert token["Authorization"] == f"Bearer COSY.{token['Authorization'].split('.')[1]}.{expected}"
    assert token["Cosy-Key"] == "RSAKEY"
    assert token["Cosy-User"] == "u1"
    assert "\nRSAKEY\n1700000000\n{}\n/api/v2/x" in token["sign_str"]


def test_aes_key_is_16_ascii_hex():
    key = cosy.new_aes_key()
    assert len(key) == 16
    assert all(ch in "0123456789abcdef" for ch in key)


def test_parse_credentials_official_shape():
    parsed = parse_credentials(
        {
            "token": "jwt-access",
            "refreshToken": "ory_rt_x",
            "expiresAt": "2026-08-25T14:09:38Z",
            "user": {"id": "uid-1", "name": "书虫", "email": "a@b"},
            "loginDeviceId": "dev-1",
        }
    )
    assert parsed["provider"] == "qwenwork"
    assert parsed["access_token"] == "jwt-access"
    assert parsed["refresh_token"] == "ory_rt_x"
    assert parsed["uid"] == "uid-1"
    assert parsed["extra"]["login_device_id"] == "dev-1"
    assert parsed["expires_at"] > 10_000_000_000


def test_parse_credentials_requires_token():
    with pytest.raises(ValueError):
        parse_credentials({"user": {"id": "1"}})


def test_qwenwork_quota_remaining_nested():
    from providers.qwenwork import _quota_remaining

    assert _quota_remaining({"quota": {"remaining": 88}}) == 88
    assert _quota_remaining({"plan": {"credits": 12}}) == 12
    assert _quota_remaining({"data": {"user": {}, "quota": {"balance": 3.5}}}) == 3.5
    assert _quota_remaining({"code": 0, "data": {"quota": {"remaining": 2098.8}}}) == 2098.8
    assert _quota_remaining({"foo": 1}) is None


def test_bind_qwenwork_when_enabled(qwen_enabled):
    key = {"default_channel": "qwenwork"}
    assert router.admit_model(key, "auto") == "auto"
    assert router.admit_model(key, "qwenwork/qwork-advanced") == "qwork-advanced"
    with pytest.raises(HTTPException) as err:
        router.admit_model(key, "glm-5.2")
    assert err.value.status_code == 400
    assert err.value.detail["error"]["code"] == "unknown_model"


def test_bind_qwenwork_disabled_is_unknown_channel(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    with pytest.raises(HTTPException) as err:
        router.admit_model({"default_channel": "qwenwork"}, "qwenwork/qwork-advanced")
    assert err.value.status_code == 400
    assert err.value.detail["error"]["code"] == "unknown_channel"


def test_qwenwork_sources_do_not_touch_workbuddy_stack():
    root = Path(__file__).resolve().parents[1] / "providers" / "qwenwork"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "copilot.tencent.com" not in text
        assert "import fingerprint" not in text
        assert "from fingerprint" not in text
        assert "X-IDE-Type" not in text


def test_qwenwork_auth_dirs_ignore_workbuddy_cb_auth_dir(monkeypatch, tmp_path):
    qdir = tmp_path / "qwen-auth"
    qdir.mkdir()
    wb = tmp_path / "workbuddy-auth"
    wb.mkdir()
    monkeypatch.setenv("CB_QWENWORK_AUTH_DIR", str(qdir))
    monkeypatch.setenv("CB_AUTH_DIR", str(wb))
    dirs = [path.resolve() for path in qwenwork_auth_dirs()]
    assert qdir.resolve() in dirs
    assert wb.resolve() not in dirs


def test_envelope_error_from_outer_400():
    raw = '{"headers":{},"body":"{\\"code\\":\\"400\\",\\"message\\":\\"Invalid agent chat JSON body\\"}","statusCodeValue":400}'
    assert "Invalid agent chat JSON body" in (envelope_error(raw) or "")
    assert unwrap_sse_payload(raw) == []


def test_unwrap_outer_sse_envelope():
    inner = '{"id":"1","choices":[{"delta":{"content":"hi"}}]}'
    payloads = unwrap_sse_payload('{"headers":{},"body":%s,"statusCodeValue":200}' % inner)
    assert payloads == [inner]
    assert unwrap_sse_payload("[DONE]") == []
    assert unwrap_sse_payload("{}") == []
    assert unwrap_sse_payload(inner) == [inner]


def test_usage_is_read_from_stream_payload():
    usage, finish = usage_from_payload(
        '{"choices":[{"finish_reason":"tool_calls","delta":{}}],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}'
    )
    assert usage["prompt_tokens"] == 12
    assert usage["completion_tokens"] == 3
    assert finish == "tool_calls"
    assert usage_from_payload("not-json") == ({}, "")


def test_static_models_include_live_and_legacy_ids():
    assert "pro" in STATIC_MODELS
    assert "flash" in STATIC_MODELS
    assert "qwen3.8-max-preview" in STATIC_MODELS
    assert "qwork-advanced" in STATIC_MODELS
    assert "qwork-auto" in STATIC_MODELS
    assert "qwork-ultimate" in STATIC_MODELS


def test_translate_model_sends_official_tier_keys():
    assert translate_model("qwork-advanced") == "qwork-advanced"
    assert translate_model("auto") == "qwork-advanced"
    assert translate_model("pro") == "qwork-advanced"
    assert translate_model("advanced") == "qwork-advanced"
    assert translate_model("qwork-auto") == "qwork-auto"
    assert translate_model("flash") == "qwork-auto"
    assert translate_model("qwork-ultimate") == "qwork-ultimate"
    assert translate_model("ultimate") == "qwork-ultimate"
    assert translate_model("qwork-lite") == "qwork-lite"


def test_parse_qwenwork_supplier_models():
    from providers.qwenwork.models import parse_supplier_models

    models = parse_supplier_models(
        {
            "qwork": [
                {"key": "pro", "display_name": "高级", "enable": True},
                {"key": "flash", "display_name": "标准", "enable": True},
                {"key": "hidden", "display_name": "x", "enable": False},
            ]
        }
    )
    ids = {item["id"] for item in models}
    assert ids == {"pro", "flash"}
    assert next(item["name"] for item in models if item["id"] == "pro") == "高级"


@pytest.mark.parametrize(
    ("controls", "expected"),
    [
        ({}, False),
        ({"reasoning_effort": "high"}, True),
        ({"thinking": {"type": "enabled"}}, True),
        ({"reasoning_effort": "none"}, False),
        ({"enable_thinking": False}, False),
    ],
)
def test_qwenwork_build_body_applies_reasoning_switch(controls, expected):
    body, _, _ = build_body({
        "model": "qwork-advanced",
        "messages": [{"role": "user", "content": "hello"}],
        **controls,
    })

    assert body["chat_context"]["extra"]["modelConfig"]["is_reasoning"] is expected
    assert body["model_config"]["is_reasoning"] is expected
    assert body["model_config"]["key"] == "qwork-advanced"
    assert body["business"] == {
        "product": "qoder_work",
        "type": "agent",
        "version": "1",
        "feature_switches": {},
    }
    assert body["chat_context"]["extra"]["modelConfig"]["key"] == "qwork-advanced"


def test_build_body_maps_legacy_ids_to_official_keys():
    body, _, model = build_body({
        "model": "pro",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert model == "qwork-advanced"
    assert body["model_config"]["key"] == "qwork-advanced"
    headers = static_headers(model, "req-1", "dev-machine")
    assert headers["x-model-key"] == "qwork-advanced"
    assert headers["X-Model-Key"] == "qwork-advanced"
    assert headers["x-internal-model-key"] == "qwork-advanced"
    assert headers["Cosy-Version"] == "1.1.32"
    assert headers["X-QwenWork-Version"] == "1.0.4"
    assert headers["Cosy-MachineType"] == "5"
    assert headers["Cosy-MachineToken"] == "dev-machine"

    body, _, model = build_body({
        "model": "flash",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert model == "qwork-auto"
    assert body["model_config"]["key"] == "qwork-auto"


def test_prepare_infer_emits_encode_body():
    body, raw, model = build_body({
        "model": "pro",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 32,
    })
    prepared = prepare_infer(
        body=raw,
        model_key=model,
        machine_id="dev-machine",
        uid="u1",
        name="n",
        email="e@x",
        access_token="tok",
    )
    assert "Encode=1" in prepared.url
    assert prepared.url.endswith("Encode=1") or "&Encode=1" in prepared.url
    assert prepared.body
    assert not prepared.body.lstrip().startswith("{")
    assert prepared.headers.get("X-Model-Key") == model
    assert prepared.headers.get("Cosy-Version") == "1.1.32"
    assert "Authorization" in prepared.headers
    assert "Cosy-Key" in prepared.headers


def test_daily_credit_notice_unwraps_adapter_payload():
    from providers.qwenwork.checkin import parse_daily_credit_notice

    assert parse_daily_credit_notice({"data": {"should_show": True, "credits": 100}}) == {
        "should_show": True,
        "credits": 100.0,
    }
    assert parse_daily_credit_notice({"data": {"data": {"should_show": False, "credits": 0}}})["should_show"] is False
    assert parse_daily_credit_notice({"data": {"should_show": True, "credits": 1.5}})["credits"] == 0


def test_claim_reads_same_day_wallet_and_status_keeps_it(isolated_db, monkeypatch):
    from providers.qwenwork import checkin

    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-1",
            "provider": "qwenwork",
            "access_token": "device-token",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": {
                    "active_wallets": {
                        "wallets": [
                            {"balance": 99.5, "valid_to": checkin._next_shanghai_midnight().isoformat()},
                            {"balance": 2000, "valid_to": "2026-11-18T22:09:24+08:00"},
                        ]
                    }
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            raise AssertionError(f"claim must not POST {url}")

        async def get(self, url, **kwargs):
            calls.append((url, kwargs["headers"]))
            return FakeResponse()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(checkin.claim_checkin(account))
    assert result["ok"] is True
    assert result["claimed"] is False
    assert result["already_claimed"] is True
    assert result["credit"] == 99.5
    assert result["message"] == "今日每日积分已到账 99.5"
    assert calls[0][0].endswith("/api/v1/adapter/user/wallets")
    assert "daily-credit-notice" not in calls[0][0]
    assert calls[0][1]["Authorization"] == "Bearer device-token"
    assert calls[0][1]["X-QwenWork-App-Mode"] == "standard"
    assert calls[0][1]["X-QwenWork-Version"] == "1.3.1"

    status = asyncio.run(checkin.fetch_checkin(account))
    assert status["already_claimed"] is True
    assert status["claimed"] is False
    assert status["credit"] == 99.5
    assert len(calls) == 1


def test_claim_refreshes_once_after_unauthorized(isolated_db, monkeypatch):
    from providers.qwenwork import checkin

    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-2",
            "provider": "qwenwork",
            "access_token": "old",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    posts = {"n": 0}

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            if self.status_code >= 400:
                return {}
            return {"data": {"should_show": False, "credits": 0}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            posts["n"] += 1
            assert "daily-credit-notice" not in url
            if posts["n"] == 1:
                assert kwargs["headers"]["Authorization"] == "Bearer old"
                return FakeResponse(401)
            assert kwargs["headers"]["Authorization"] == "Bearer fresh"
            return FakeResponse(200)

    async def fake_refresh(row):
        return {**row, "access_token": "fresh"}

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(checkin, "refresh_account", fake_refresh)
    result = asyncio.run(checkin.claim_checkin(account))
    assert posts["n"] == 2
    assert result["ok"] is True
    assert result["already_claimed"] is False
    assert result["no_campaign"] is True
    assert "尚未到账" in result["message"]
    assert "不能代领" in result["message"]


def test_claim_400_includes_upstream_message(isolated_db, monkeypatch):
    from providers.qwenwork import checkin

    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-400",
            "provider": "qwenwork",
            "access_token": "tok",
            "expires_at": 9_999_999_999_999,
        }
    )

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"errorCode": "BAD_REQUEST", "errorMessage": "unexpected body"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            assert url.endswith("/api/v1/adapter/user/wallets")
            return FakeResponse()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(checkin.claim_checkin(db.get_account(account_id)))
    assert result["ok"] is False
    assert result["status_code"] == 400
    assert "unexpected body" in result["message"]


def test_refresh_uses_openapi_identity_without_bearer(isolated_db, monkeypatch):
    from providers.qwenwork import token

    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-refresh",
            "provider": "qwenwork",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 1_700_000_000_000,
        }
    )
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "device_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "refresh_token_expires_in": 86400,
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs["headers"], kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(token.httpx, "AsyncClient", FakeClient)
    fresh = asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert calls[0][0].endswith("/api/v1/deviceToken/refresh")
    assert calls[0][1]["X-QwenWork-Version"] == "1.3.1"
    assert calls[0][1]["X-QwenWork-App-Mode"] == "standard"
    assert calls[0][1]["User-Agent"] == "qoderwork/1.3.1"
    assert "Authorization" not in calls[0][1]
    assert calls[0][2] == {"refresh_token": "old-refresh", "target": "c"}
    assert fresh["access_token"] == "new-access"
    assert fresh["refresh_token"] == "new-refresh"


def test_refresh_401_explains_claim_can_still_succeed(isolated_db, monkeypatch):
    from providers.qwenwork import token

    expires_at = int((time.time() + 3 * 86400) * 1000)
    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-dead-refresh",
            "provider": "qwenwork",
            "access_token": "still-valid",
            "refresh_token": "dead-refresh",
            "expires_at": expires_at,
        }
    )

    class FakeResponse:
        status_code = 401

        def json(self):
            return {
                "errorCode": "INVALID_REFRESH_TOKEN",
                "errorMessage": "refresh token is invalid",
                "upstreamError": {"code": "invalid_grant"},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(token.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(token, "read_official_session", lambda path=None: None)
    with pytest.raises(token.QwenWorkAuthError) as caught:
        asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert "续期令牌已失效" in str(caught.value)
    assert "领取不受影响" in str(caught.value)
    saved = db.get_account(account_id)
    assert saved["access_token"] == "still-valid"
    assert saved["refresh_token"] == "dead-refresh"


def test_refresh_retries_a_newer_official_session(isolated_db, monkeypatch):
    from providers.qwenwork import token

    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-stale",
            "provider": "qwenwork",
            "access_token": "old-access",
            "refresh_token": "stale-refresh",
            "expires_at": 1_700_000_000_000,
        }
    )
    used = []

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            if self.status_code >= 400:
                return {"errorCode": "INVALID_REFRESH_TOKEN", "errorMessage": "refresh token is invalid"}
            return {"token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 60}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            refresh = kwargs["json"]["refresh_token"]
            used.append(refresh)
            return FakeResponse(401 if refresh == "stale-refresh" else 200)

    monkeypatch.setattr(token.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        token,
        "read_official_session",
        lambda path=None: {
            "uid": "qw-stale",
            "access_token": "official-access",
            "refresh_token": "official-refresh",
            "expires_at": 1_800_000_000_000,
        },
    )
    fresh = asyncio.run(token.refresh_account(db.get_account(account_id)))
    assert used == ["stale-refresh", "official-refresh"]
    assert fresh["access_token"] == "fresh-access"
    assert fresh["refresh_token"] == "fresh-refresh"


def test_provider_checkin_is_enabled():
    from providers.qwenwork import PROVIDER

    assert PROVIDER.checkin_supported is True
    assert hasattr(PROVIDER, "claim_checkin")
    assert hasattr(PROVIDER, "fetch_checkin")


def test_wallet_expiry_skips_spent_and_past_wallets():
    from datetime import datetime, timedelta

    from providers.qwenwork import checkin

    now = datetime(2026, 11, 1, 12, 0, tzinfo=checkin.SHANGHAI)
    soon = (now + timedelta(days=3)).isoformat()
    later = (now + timedelta(days=40)).isoformat()
    past = (now - timedelta(days=1)).isoformat()
    payload = {
        "data": {
            "active_wallets": {
                "wallets": [
                    {"balance": 0, "valid_to": soon},
                    {"balance": 500, "valid_to": past},
                    {"balance": 2000, "valid_to": later},
                    {"balance": 99.5, "valid_to": soon},
                    {"balance": 10},
                ]
            }
        }
    }
    assert checkin.wallet_expiry(payload, now=now) == datetime.fromisoformat(soon).timestamp()
    assert checkin.wallet_expiry({"data": {"active_wallets": {"wallets": []}}}, now=now) is None
    assert checkin.wallet_expiry({}, now=now) is None


def test_status_checkin_caches_the_wallet_deadline(isolated_db, qwen_enabled, monkeypatch):
    import control_plane
    import routing
    from datetime import datetime, timedelta

    from providers.qwenwork import checkin

    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-deadline",
            "provider": "qwenwork",
            "access_token": "device-token",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)
    expiry = datetime.now(checkin.SHANGHAI) + timedelta(days=30)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": {"active_wallets": {"wallets": [{"balance": 2000, "valid_to": expiry.isoformat()}]}}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(control_plane.checkin_status_one(account, force=True))
    # The daily bucket is missing, but the long-lived pack still has a deadline.
    assert result["no_campaign"] is True
    assert result["expire_source"] == checkin.WALLET_EXPIRE_SOURCE
    assert abs(float(result["next_expire_ts"]) - expiry.timestamp()) < 1

    fresh = db.get_account(account_id)
    snapshot = routing.resource_snapshot(fresh)
    assert snapshot["next_expire_ts"] == snapshot["expires_at"]
    assert abs(float(snapshot["next_expire_ts"]) - expiry.timestamp()) < 1
    assert snapshot["next_expire_days"] == 30
    # A wallet balance is a daily bucket, not the account total: never cached as
    # remaining, or routing would drop the account as exhausted.
    assert "remaining" not in snapshot
    assert routing.urgency_source(fresh, "qwenwork") == "quota"
    assert routing.urgency_days(fresh, "qwenwork") == 30.0
    assert routing.quota_exhausted(fresh) is False


def test_status_checkin_without_a_deadline_writes_nothing(isolated_db, qwen_enabled, monkeypatch):
    import control_plane
    import routing
    from providers.qwenwork import checkin

    account_id = db.add_account(
        {
            "name": "qw",
            "uid": "qw-no-deadline",
            "provider": "qwenwork",
            "access_token": "device-token",
            "expires_at": 9_999_999_999_999,
        }
    )
    account = db.get_account(account_id)

    class FakeResponse:
        status_code = 200

        def json(self):
            # Spent daily bucket: no valid_to worth routing on.
            return {"data": {"active_wallets": {"wallets": [{"balance": 0}]}}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(control_plane.checkin_status_one(account, force=True))
    assert "next_expire_ts" not in result
    assert routing.resource_snapshot(db.get_account(account_id)) == {}
    assert routing.urgency_source(db.get_account(account_id), "qwenwork") == "default"
    assert routing.urgency_days(db.get_account(account_id), "qwenwork") == routing.DEFAULT_URGENCY_DAYS


def test_resource_cache_takes_remaining_without_inventing_an_expiry(isolated_db):
    import resource_sync
    import routing

    account_id = db.add_account({"name": "x", "uid": "x-1", "provider": "xiaohuanxiong", "status": "active"})
    account = db.get_account(account_id)

    stored = resource_sync.store_quota_expiry(account, remaining=500, source="test")
    assert stored["remaining"] == 500
    assert stored["available_total"] == 500
    assert "next_expire_ts" not in stored
    assert "expires_at" not in stored
    assert "next_expire_days" not in stored

    snapshot = routing.resource_snapshot(account)
    assert snapshot["remaining"] == 500
    assert "next_expire_ts" not in snapshot
    assert routing.urgency_source(account, "xiaohuanxiong") == "default"
    assert routing.urgency_days(account, "xiaohuanxiong") == routing.DEFAULT_URGENCY_DAYS
    assert routing.quota_exhausted(account) is False

    # A deadline that already passed is not a deadline: keep the older reading.
    assert resource_sync.store_quota_expiry(account, expires_at=time.time() - 60) == {}
    assert routing.resource_snapshot(account)["remaining"] == 500
    assert resource_sync.store_quota_expiry(account) == {}
    assert routing.resource_snapshot(account)["remaining"] == 500


def test_daily_bucket_keeps_the_fraction_of_a_day(isolated_db):
    import resource_sync
    import routing
    from providers.qwenwork import checkin

    # The daily bucket expires at the next Shanghai midnight, so the cached
    # deadline must carry the fraction of a day: flattening it to whole days
    # would report "expires now" for most of the afternoon.
    midnight = checkin._next_shanghai_midnight()
    account_id = db.add_account({"name": "qw", "uid": "qw-daily", "provider": "qwenwork", "status": "active"})
    fresh = db.get_account(account_id)
    stored = resource_sync.store_quota_expiry(fresh, expires_at=midnight.timestamp(), source=checkin.WALLET_EXPIRE_SOURCE)

    days = stored["next_expire_days"]
    assert isinstance(days, float)
    assert 0 < days <= 1
    assert abs(days - (midnight.timestamp() - time.time()) / 86400) < 0.01

    snapshot = routing.resource_snapshot(fresh)
    assert snapshot["next_expire_days"] == days
    assert 0 < routing.urgency_days(fresh, "qwenwork") <= 1
    assert routing.urgency_days(fresh, "qwenwork") == days
    assert routing.urgency_source(fresh, "qwenwork") == "quota"
