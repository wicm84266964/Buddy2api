import asyncio
import hashlib
from pathlib import Path

import pytest

import credential_crypto
import database as db
import providers
import router
from providers.protocol import KeyChannelMismatch, UnknownChannel, UnknownModel
from providers.qclaw.constants import JPRX_SIGNATURE_KEY, STATIC_MODELS
from providers.qclaw.chat import _build_body, fill_empty_content
from providers.qclaw.sign import aizone_headers, jprx_ctx
from providers.qclaw.store import parse_credentials, qclaw_auth_dirs


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
def qclaw_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def test_qclaw_quota_is_credit_not_token_cap(qclaw_enabled):
    snapshot = asyncio.run(providers.get_provider("qclaw").fetch_quota({"id": 1}))
    assert snapshot.unit == "credit"
    assert snapshot.remaining is None
    assert snapshot.unsupported is True


def test_qclaw_in_default_registry(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    assert providers.enabled_provider_ids() == ["workbuddy", "qclaw", "qwenwork", "traework"]
    assert providers.get_provider("qclaw") is not None
    assert "qclaw" in providers._LOADED


def test_fill_empty_content_uses_reasoning():
    filled = fill_empty_content({"role": "assistant", "content": "", "reasoning_content": "你好"})
    assert filled["content"] == "你好"
    kept = fill_empty_content({"role": "assistant", "content": "可见", "reasoning_content": "隐藏"})
    assert kept["content"] == "可见"


def test_fill_empty_content_normalizes_reasoning_alias():
    filled = fill_empty_content({"role": "assistant", "content": "", "reasoning": "思考"})
    assert filled["content"] == "思考"
    assert filled["reasoning_content"] == "思考"


@pytest.mark.parametrize(
    ("controls", "expected"),
    [
        ({"reasoning": {"effort": "xhigh"}}, "xhigh"),
        ({"thinking": {"type": "enabled"}}, "high"),
        ({"thinking": {"type": "disabled"}}, "none"),
    ],
)
def test_qclaw_build_body_normalizes_agent_reasoning_controls(controls, expected):
    body, _ = _build_body({"model": "default", "messages": [], **controls})

    assert body["reasoning_effort"] == expected
    assert "thinking" not in body
    assert "reasoning" not in body


def test_jprx_ctx_matches_official_md5_formula():
    body = '{"web_version":"1.4.0"}'
    gid = "abc"
    rnd = "n" * 32
    date = "1787620000"
    header = jprx_ctx(body, gid, rnd=rnd, date=date)
    expected = hashlib.md5(
        f"{body}{JPRX_SIGNATURE_KEY}{rnd}{date}{gid}".encode("utf-8")
    ).hexdigest()
    assert header == f"rnd={rnd}; date={date}; gid={gid}; sg={expected}"


def test_parse_credentials_flat_and_nested():
    flat = parse_credentials(
        {
            "provider": "qclaw",
            "sk_api_key": "sk-test-key",
            "jwt": "jwt-token",
            "user_id": "42",
            "guid": "guid-1",
            "nickname": "n",
        }
    )
    assert flat["provider"] == "qclaw"
    assert flat["access_token"] == "sk-test-key"
    assert flat["refresh_token"] == "jwt-token"
    assert flat["uid"] == "42"
    assert flat["extra"]["guid"] == "guid-1"

    nested = parse_credentials(
        {
            "provider": "qclaw",
            "auth": {"accessToken": "sk-nested", "refreshToken": "jwt-2"},
            "account": {"uid": "9", "nickname": "nn", "guid": "g2"},
        }
    )
    assert nested["access_token"] == "sk-nested"
    assert nested["uid"] == "9"


def test_parse_credentials_requires_key():
    with pytest.raises(ValueError):
        parse_credentials({"provider": "qclaw", "uid": "1"})


def test_bind_qclaw_when_enabled(qclaw_enabled):
    bound = router.bind({"model": "auto"}, {"default_channel": "qclaw"})
    assert bound.channel == "qclaw"
    assert bound.inner == "auto"
    bound = router.bind({"model": "qclaw/pool-glm-5.2"}, {"default_channel": "qclaw"})
    assert bound.inner == "pool-glm-5.2"
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "qclaw"})
    with pytest.raises(KeyChannelMismatch):
        router.bind({"model": "workbuddy/auto"}, {"default_channel": "qclaw"})


def test_bind_qclaw_disabled_is_unknown_channel(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    with pytest.raises(UnknownChannel):
        router.bind({"model": "qclaw/default"}, {"default_channel": "qclaw"})


def test_qclaw_sources_do_not_touch_workbuddy_stack():
    root = Path(__file__).resolve().parents[1] / "providers" / "qclaw"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "copilot.tencent.com" not in text
        assert "import fingerprint" not in text
        assert "from fingerprint" not in text
        assert "X-IDE-Type" not in text


def test_qclaw_auth_dirs_ignore_workbuddy_cb_auth_dir(monkeypatch, tmp_path):
    qdir = tmp_path / "qclaw-auth"
    qdir.mkdir()
    wb = tmp_path / "workbuddy-auth"
    wb.mkdir()
    monkeypatch.setenv("CB_QCLAW_AUTH_DIR", str(qdir))
    monkeypatch.setenv("CB_AUTH_DIR", str(wb))
    monkeypatch.delenv("CB_QCLAW_USER_DATA_DIR", raising=False)
    dirs = qclaw_auth_dirs()
    assert qdir in dirs
    assert wb not in dirs


def test_upsert_qclaw_account_updates_token(isolated_db):
    from providers.qclaw.store import upsert_account

    first = upsert_account(
        parse_credentials(
            {"sk_api_key": "sk-old", "jwt": "jwt-old", "user_id": "u1", "guid": "g"}
        )
    )
    second = upsert_account(
        parse_credentials(
            {"sk_api_key": "sk-new", "jwt": "jwt-new", "user_id": "u1", "guid": "g"}
        )
    )
    assert first["id"] == second["id"]
    assert second["updated"] is True
    row = db.get_account(first["id"])
    assert row["provider"] == "qclaw"
    assert row["access_token"] == "sk-new"
    assert row["refresh_token"] == "jwt-new"
    wb = db.add_account({"name": "wb", "uid": "u1", "access_token": "wb", "provider": "workbuddy"})
    assert wb != first["id"]


def test_aizone_headers_require_conversation_request_id():
    headers = aizone_headers(api_key="sk-test", guid="g", account="1")
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["X-Conversation-Request-ID"]
    assert "x-signature" not in {k.lower() for k in headers}


def test_static_models_include_pool_glm():
    from providers.qclaw import PROVIDER

    assert "pool-glm-5.2" in STATIC_MODELS
    assert PROVIDER.accepts_model("auto")
    assert PROVIDER.accepts_model("default")
    assert PROVIDER.translate_model("auto") == "default"
    assert PROVIDER.checkin_supported is False
