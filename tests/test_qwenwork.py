import hashlib
from pathlib import Path

import pytest

import credential_crypto
import database as db
import providers
import router
from providers.protocol import UnknownChannel, UnknownModel
from providers.qwenwork import cosy
from providers.qwenwork.chat import build_body, envelope_error, unwrap_sse_payload
from providers.qwenwork.constants import COSY_VERSION, COSY_VERSION_FROZEN, RSA_PUBLIC_KEY_PEM, STATIC_MODELS
from providers.qwenwork.store import parse_credentials, qwenwork_auth_dirs


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
    assert providers.enabled_provider_ids() == ["workbuddy", "qclaw", "qwenwork", "traework"]
    assert providers.get_provider("qwenwork") is not None
    assert "qwenwork" in providers._LOADED


def test_cosy_version_is_frozen():
    assert COSY_VERSION_FROZEN is True
    assert COSY_VERSION == "1.1.18"
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


def test_bind_qwenwork_when_enabled(qwen_enabled):
    bound = router.bind({"model": "auto"}, {"default_channel": "qwenwork"})
    assert bound.channel == "qwenwork"
    assert bound.inner == "auto"
    bound = router.bind({"model": "qwenwork/qwork-advanced"}, {"default_channel": "qwenwork"})
    assert bound.inner == "qwork-advanced"
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "qwenwork"})


def test_bind_qwenwork_disabled_is_unknown_channel(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    with pytest.raises(UnknownChannel):
        router.bind({"model": "qwenwork/qwork-advanced"}, {"default_channel": "qwenwork"})


def test_qwenwork_sources_do_not_touch_workbuddy_stack():
    root = Path(__file__).resolve().parents[1] / "providers" / "qwenwork"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "copilot.tencent.com" not in text
        assert "import fingerprint" not in text
        assert "from fingerprint" not in text
        assert "X-IDE-Type" not in text
        assert "Encode=1" not in text


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


def test_static_models_match_official_0_1_8():
    assert STATIC_MODELS == ("qwork-advanced", "qwork-auto", "qwork-lite", "qmodel_latest")


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
