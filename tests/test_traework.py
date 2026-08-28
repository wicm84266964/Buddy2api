import json
from pathlib import Path

import pytest

import credential_crypto
import database as db
import providers
import router
from providers.protocol import UnknownModel
from providers.traework.chat import extract_assistant_text, translate_model
from providers.traework.crypto import decrypt_tc_b64
from providers.traework.store import parse_credentials, traework_auth_dirs


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
def traework_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def test_traework_in_default_registry(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)
    assert providers.enabled_provider_ids() == ["workbuddy", "qclaw", "qwenwork", "traework"]
    assert providers.get_provider("traework") is not None
    assert "traework" in providers._LOADED


def test_parse_credentials_official_shape():
    parsed = parse_credentials(
        {
            "token": "jwt-access",
            "refreshToken": "rt-1",
            "userId": "3577",
            "expiredAt": "2026-09-09T08:55:19.325Z",
            "host": "https://api.trae.cn",
            "account": {"username": "书虫"},
            "device_id": "3446",
        }
    )
    assert parsed["provider"] == "traework"
    assert parsed["access_token"] == "jwt-access"
    assert parsed["refresh_token"] == "rt-1"
    assert parsed["uid"] == "3577"
    assert parsed["extra"]["device_id"] == "3446"
    assert parsed["expires_at"] > 10_000_000_000


def test_parse_credentials_requires_token():
    with pytest.raises(ValueError):
        parse_credentials({"account": {"username": "x"}})


def test_bind_traework_when_enabled(traework_enabled):
    bound = router.bind({"model": "auto"}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == "auto"
    bound = router.bind({"model": "traework/qwen-3.7-plus"}, {"default_channel": "traework"})
    assert bound.inner == "qwen-3.7-plus"
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "traework"})


def test_parse_supplier_models_official_grouped_list():
    from providers.traework.models import parse_supplier_models

    parsed = parse_supplier_models(
        {
            "code": 0,
            "message": "success",
            "data": {
                "list": [
                    {
                        "function": "solo_coder",
                        "models": [
                            {
                                "name": "Doubao-Seed-2.0-Code",
                                "display_name": "Doubao-Seed-2.0-Code",
                                "is_default": False,
                            },
                            {
                                "name": "Doubao-Seed-Code",
                                "display_name": "Doubao-Seed-Code",
                            },
                            {
                                "name": "qwen-3.6-plus",
                                "display_name": "qwen-3.6-plus",
                            },
                        ],
                    }
                ]
            },
        }
    )
    ids = [item["id"] for item in parsed]
    assert ids == ["Doubao-Seed-2.0-Code", "Doubao-Seed-Code", "qwen-3.6-plus"]
    assert "function" not in ids


def test_translate_auto():
    assert translate_model("auto") == "qwen-3.7-plus"


def test_extract_assistant_text_from_task():
    items = [
        {"role": "user", "content": "[]"},
        {
            "role": "assistant",
            "message_type": "task",
            "content": json.dumps(
                {
                    "task_id": "t1",
                    "messages": [
                        {"type": "text", "text_content": "pong"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]
    assert extract_assistant_text(items) == "pong"


def test_traework_sources_do_not_touch_workbuddy_stack():
    root = Path(__file__).resolve().parents[1] / "providers" / "traework"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "copilot.tencent.com" not in text
        assert "import fingerprint" not in text
        assert "from fingerprint" not in text
        assert "X-IDE-Type" not in text


def test_traework_auth_dirs_ignore_workbuddy_cb_auth_dir(monkeypatch, tmp_path):
    tdir = tmp_path / "trae-auth"
    tdir.mkdir()
    wb = tmp_path / "workbuddy-auth"
    wb.mkdir()
    monkeypatch.setenv("CB_TRAEWORK_AUTH_DIR", str(tdir))
    monkeypatch.setenv("CB_AUTH_DIR", str(wb))
    dirs = [path.resolve() for path in traework_auth_dirs()]
    assert tdir.resolve() in dirs
    assert wb.resolve() not in dirs


def test_decrypt_tc_roundtrip_rejects_garbage():
    with pytest.raises(Exception):
        decrypt_tc_b64("not-base64-$$$")
