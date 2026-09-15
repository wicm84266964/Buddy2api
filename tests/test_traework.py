import asyncio
import json
import sys
from pathlib import Path

import pytest

import credential_crypto
import database as db
import providers
import router
from providers.protocol import UnknownModel
from providers.traework.chat import (
    _stream_once,
    extract_assistant_text,
    extract_assistant_turn,
    translate_model,
)
from providers.traework.crypto import decrypt_tc_b64
from providers.traework.store import parse_credentials, traework_auth_dirs, traework_user_data_dir
from providers.traework.token import _device_info, _os_info


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


def test_bind_traework_when_enabled(isolated_db, traework_enabled):
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


def _finish_task_item(*, summary: str, reasoning: str = "", usage: dict | None = None) -> dict:
    item = {
        "role": "assistant",
        "message_type": "task",
        "content": json.dumps(
            {
                "task_id": "t1",
                "messages": [
                    {
                        "type": "plan_item",
                        "plan_item": {
                            "thought": "",
                            "reasoning_content": reasoning,
                            "tool_call_info": {
                                "name": "finish",
                                "params": {"summary": summary},
                                "result": {"data": {"summary": ""}, "status": "success"},
                            },
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
    }
    if usage is not None:
        item["token_usage"] = json.dumps(usage, ensure_ascii=False)
    return item


def test_extract_finish_summary_not_reasoning():
    items = [
        {"role": "user", "content": "[]"},
        _finish_task_item(
            summary="pong",
            reasoning='The user wants me to reply with exactly "pong".',
            usage={"prompt_tokens": 26490, "completion_tokens": 16, "total_tokens": 26506, "reasoning_tokens": 11},
        ),
    ]
    turn = extract_assistant_turn(items)
    assert turn["text"] == "pong"
    assert "user wants me to reply" in turn["reasoning"]
    assert turn["usage"]["prompt_tokens"] == 26490
    assert turn["usage"]["completion_tokens"] == 16
    assert turn["usage"]["total_tokens"] == 26506


def test_extract_deepseek_finish_without_reasoning():
    items = [_finish_task_item(summary="pong", reasoning="")]
    turn = extract_assistant_turn(items)
    assert turn["text"] == "pong"
    assert turn["reasoning"] == ""


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


def test_traework_user_data_dir_windows_keeps_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.delenv("CB_TRAEWORK_USER_DATA_DIR", raising=False)
    assert traework_user_data_dir() == tmp_path / "Roaming" / "TRAE SOLO CN"


def test_traework_user_data_dir_darwin_uses_application_support(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CB_TRAEWORK_USER_DATA_DIR", raising=False)
    assert traework_user_data_dir() == tmp_path / "Library" / "Application Support" / "TRAE SOLO CN"
    folders = traework_auth_dirs()
    assert tmp_path / "Library" / "Application Support" / "TRAE SOLO CN" / "User" / "globalStorage" in folders


def test_device_info_windows_osinfo_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("CB_TRAEWORK_OS_INFO", raising=False)
    monkeypatch.delenv("CB_TRAEWORK_DEVICE_NAME", raising=False)
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-TEST")
    info = _device_info({"extra": {"device_id": "1", "machine_id": "m"}})
    assert info["OSInfo"] == "windows"
    assert info["DeviceName"] == "DESKTOP-TEST"
    assert info["DeviceType"] == "PC"
    assert info["DeviceID"] == "1"


def test_device_info_darwin_osinfo(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("CB_TRAEWORK_OS_INFO", raising=False)
    monkeypatch.delenv("CB_TRAEWORK_DEVICE_NAME", raising=False)
    monkeypatch.setenv("USER", "macuser")
    info = _device_info({"extra": {}})
    assert info["OSInfo"] == "mac"
    assert info["DeviceName"] == "macuser"
    assert info["DeviceType"] == "PC"


def test_os_info_env_override(monkeypatch):
    monkeypatch.setenv("CB_TRAEWORK_OS_INFO", "macOS 15.6")
    assert _os_info() == "macOS 15.6"


def test_decrypt_tc_roundtrip_rejects_garbage():
    with pytest.raises(Exception):
        decrypt_tc_b64("not-base64-$$$")


def test_stream_once_yields_bytes():
    async def collect():
        return [chunk async for chunk in _stream_once("pong", "glm-5.3")]

    chunks = asyncio.run(collect())
    assert chunks
    assert all(isinstance(chunk, (bytes, bytearray)) for chunk in chunks)
    assert chunks[-1] == b"data: [DONE]\n\n"
    payload = json.loads(chunks[0].decode("utf-8").split("data:", 1)[1].strip())
    assert payload["choices"][0]["delta"]["content"] == "pong"


def test_responses_bridge_accepts_traework_text_sse(isolated_db, traework_enabled, monkeypatch):
    async def string_stream(payload, api_key_info):
        async def chunks():
            yield (
                'data: {"id":"traework-stub","model":"glm-5.3",'
                '"choices":[{"index":0,"delta":{"role":"assistant","content":"pong"},'
                '"finish_reason":null}]}\n\n'
            )
            yield (
                'data: {"id":"traework-stub","model":"glm-5.3",'
                '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            )
            yield "data: [DONE]\n\n"

        return ("stream", chunks())

    provider = providers.get_provider("traework")
    monkeypatch.setattr(provider, "chat_completions", string_stream)
    original = "traework/qwen-3.7-plus"
    bound = router.bind({"model": original}, {"default_channel": "traework"})

    async def collect_events():
        result = await router.responses_after_bind(
            bound,
            {"model": original, "input": "hi", "stream": True},
            {"id": 1, "name": "dsh-key", "default_channel": "traework"},
        )
        assert result[0] == "stream"
        return [chunk async for chunk in result[1]]

    raw = asyncio.run(collect_events())
    events = [
        json.loads(line[6:])
        for chunk in raw
        for line in (
            chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        ).splitlines()
        if line.startswith("data: {")
    ]
    failed = [event for event in events if event.get("type") == "response.failed"]
    completed = [event for event in events if event.get("type") == "response.completed"]
    assert not failed
    assert completed
    assert completed[0]["response"]["output"][0]["content"][0]["text"] == "pong"
