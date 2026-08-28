import asyncio
import json
import os
from pathlib import Path

import pytest

import credential_crypto
import database as db
import providers
import proxy
import router
import server
from providers.protocol import KeyChannelMismatch, UnknownModel
from providers.qclaw.constants import STATIC_MODELS as QCLAW_STATIC
from providers.qwenwork.constants import STATIC_MODELS as QWEN_STATIC
from providers.traework.constants import STATIC_MODELS as TRAE_STATIC

QCLAW_NEW_ID = "qclaw-live-only-model"
TRAE_NEW_DOUBAO = "Doubao-Seed-2.2-Pro"
TRAE_DOUBAO_TURBO = "Doubao-Seed-2.1-Turbo"
TRAE_DOUBAO_CODE = "Doubao-Seed-2.0-Code"

QCLAW_HTTP_PAYLOAD = {
    "ret": 0,
    "data": {
        "resp": {
            "common": {"code": 0},
            "data": {
                "model_status_list": [
                    {"id": "default", "name": "Default"},
                    {"id": "pool-glm-5.2", "name": "GLM 5.2"},
                    {"id": QCLAW_NEW_ID, "name": "QClaw Live Only"},
                ]
            },
        }
    },
}

TRAEWORK_HTTP_PAYLOAD = {
    "code": 0,
    "message": "success",
    "data": {
        "list": [
            {
                "function": "solo_coder",
                "models": [
                    {"name": TRAE_DOUBAO_CODE, "display_name": TRAE_DOUBAO_CODE},
                    {"name": TRAE_DOUBAO_TURBO, "display_name": TRAE_DOUBAO_TURBO},
                    {"name": TRAE_NEW_DOUBAO, "display_name": TRAE_NEW_DOUBAO},
                    {"name": "qwen-3.6-plus", "display_name": "qwen-3.6-plus"},
                ],
            }
        ]
    },
}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.headers = {}
        self.content = b"{}"

    def json(self):
        return self._payload


def _ids(models):
    return {str(item.get("id") if isinstance(item, dict) else item) for item in models}


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
def all_channels(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw,qwenwork,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def _seed_live_accounts():
    db.add_account(
        {
            "name": "qc",
            "uid": "qc-1",
            "provider": "qclaw",
            "status": "active",
            "access_token": "sk-qclaw",
            "refresh_token": "jwt-qclaw",
            "extra": {"guid": "guid-1"},
        }
    )
    db.add_account(
        {
            "name": "tw",
            "uid": "tw-1",
            "provider": "traework",
            "status": "active",
            "access_token": "tok-trae",
            "expires_at": 9_999_999_999_999,
            "extra": {"device_id": "dev-1"},
        }
    )


def _install_supplier_http(monkeypatch):
    requested = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            requested.append(("POST", str(url)))
            if "/data/4320/forward" in str(url):
                return _FakeResponse(QCLAW_HTTP_PAYLOAD)
            raise AssertionError(f"unexpected POST {url}")

        async def get(self, url, **kwargs):
            requested.append(("GET", str(url)))
            if "/api/remote/v1/models" in str(url):
                return _FakeResponse(TRAEWORK_HTTP_PAYLOAD)
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("providers.qclaw.jprx.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("providers.traework.models.httpx.AsyncClient", FakeAsyncClient)
    return requested


def _by_channel(result):
    return {item["channel"]: item for item in result["sources"]}


def test_admin_models_page_has_one_click_control():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "一键读取供应模型" in html
    assert "/admin/models/refresh" in html
    assert "syncSources" in html


def test_supplier_catalog_refresh_keeps_channels_distinct(isolated_db, all_channels, monkeypatch):
    assert QCLAW_NEW_ID not in QCLAW_STATIC
    assert TRAE_NEW_DOUBAO not in TRAE_STATIC
    assert TRAE_DOUBAO_TURBO in TRAE_STATIC

    qclaw = providers.get_provider("qclaw")
    traework = providers.get_provider("traework")
    workbuddy = providers.get_provider("workbuddy")
    qwenwork = providers.get_provider("qwenwork")

    assert QCLAW_NEW_ID not in _ids(qclaw.list_models())
    assert TRAE_NEW_DOUBAO not in _ids(traework.list_models())
    assert not qclaw.accepts_model(QCLAW_NEW_ID)
    assert not traework.accepts_model(TRAE_NEW_DOUBAO)

    _seed_live_accounts()
    requested = _install_supplier_http(monkeypatch)

    chat_hits = []

    async def boom_chat(payload, api_key_info):
        chat_hits.append((payload.get("model"), api_key_info))
        raise AssertionError("chat should not run")

    for channel in providers.enabled_provider_ids():
        monkeypatch.setattr(providers.get_provider(channel), "chat_completions", boom_chat)

    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    result = asyncio.run(server.admin_refresh_models())
    sources = _by_channel(result)

    assert sources["qclaw"]["mode"] == "live"
    assert sources["traework"]["mode"] == "live"
    assert sources["workbuddy"]["mode"] == "fallback"
    assert sources["workbuddy"]["message"] == "no supplier-list API"
    assert sources["qwenwork"]["mode"] == "fallback"
    assert sources["qwenwork"]["message"] == "no supplier-list API"

    assert QCLAW_NEW_ID in _ids(sources["qclaw"]["models"])
    assert TRAE_NEW_DOUBAO in _ids(sources["traework"]["models"])
    assert TRAE_DOUBAO_TURBO in _ids(sources["traework"]["models"])

    wb_ids = _ids(sources["workbuddy"]["models"])
    assert QCLAW_NEW_ID not in wb_ids
    assert TRAE_NEW_DOUBAO not in wb_ids
    assert TRAE_DOUBAO_TURBO not in wb_ids
    assert wb_ids == _ids(proxy.DEFAULT_MODELS)

    qwen_ids = _ids(sources["qwenwork"]["models"])
    assert qwen_ids == set(QWEN_STATIC)
    assert TRAE_NEW_DOUBAO not in qwen_ids
    assert QCLAW_NEW_ID not in qwen_ids

    assert any("/data/4320/forward" in url for method, url in requested if method == "POST")
    assert any("/api/remote/v1/models" in url for method, url in requested if method == "GET")
    assert not any("copilot.tencent.com" in url for _, url in requested)
    assert not any("qwenwork.cn" in url for _, url in requested)

    assert QCLAW_NEW_ID in _ids(qclaw.list_models())
    assert TRAE_NEW_DOUBAO in _ids(traework.list_models())
    assert qclaw.accepts_model(QCLAW_NEW_ID)
    assert traework.accepts_model(TRAE_NEW_DOUBAO)
    assert traework.accepts_model(TRAE_DOUBAO_TURBO)
    assert not workbuddy.accepts_model(TRAE_NEW_DOUBAO)
    assert not workbuddy.accepts_model(TRAE_DOUBAO_TURBO)
    assert not qclaw.accepts_model(TRAE_NEW_DOUBAO)
    assert not qwenwork.accepts_model(TRAE_NEW_DOUBAO)
    assert not qwenwork.accepts_model(QCLAW_NEW_ID)

    bound = router.bind({"model": "traework/" + TRAE_NEW_DOUBAO}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == TRAE_NEW_DOUBAO
    bound = router.bind({"model": "qclaw/" + QCLAW_NEW_ID}, {"default_channel": "qclaw"})
    assert bound.channel == "qclaw"
    assert bound.inner == QCLAW_NEW_ID

    def attempt_chat(payload, key):
        bound = router.bind(payload, key)
        return asyncio.run(router.chat_after_bind(bound, payload, key))

    with pytest.raises(UnknownModel):
        attempt_chat({"model": TRAE_NEW_DOUBAO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    with pytest.raises(UnknownModel):
        attempt_chat({"model": TRAE_DOUBAO_TURBO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    with pytest.raises(KeyChannelMismatch):
        attempt_chat({"model": "traework/" + TRAE_NEW_DOUBAO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    with pytest.raises(UnknownModel):
        attempt_chat({"model": QCLAW_NEW_ID, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})

    assert chat_hits == []

    payload = {"object": "list", "data": server.collect_v1_models()}
    data = payload["data"]
    by_id = {item["id"]: item for item in data}

    assert by_id["qclaw/" + QCLAW_NEW_ID]["channel"] == "qclaw"
    assert by_id["traework/" + TRAE_NEW_DOUBAO]["channel"] == "traework"
    assert by_id["traework/" + TRAE_DOUBAO_TURBO]["channel"] == "traework"
    assert TRAE_NEW_DOUBAO not in by_id
    assert TRAE_DOUBAO_TURBO not in by_id
    assert QCLAW_NEW_ID not in by_id
    assert "glm-5.2" in by_id
    assert by_id["glm-5.2"]["channel"] == "workbuddy"
    assert by_id["workbuddy/glm-5.2"]["channel"] == "workbuddy"

    evidence = os.environ.get("BUDDY2API_EVIDENCE_DIR")
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        Path(evidence, "v1-models.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
