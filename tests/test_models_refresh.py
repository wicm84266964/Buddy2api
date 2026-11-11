import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

import credential_crypto
import database as db
import providers
import router
import routing
import server
from providers.qclaw.constants import STATIC_MODELS as QCLAW_STATIC
from providers.qwenwork.constants import STATIC_MODELS as QWEN_STATIC
from providers.traework.constants import STATIC_MODELS as TRAE_STATIC

QCLAW_NEW_ID = "qclaw-live-only-model"
QWEN_NEW_ID = "qwen-live-only-model"
WB_NEW_ID = "wb-live-only-model"
TRAE_NEW_DOUBAO = "Doubao-Seed-2.2-Pro"
TRAE_DOUBAO_TURBO = "Doubao-Seed-2.1-Turbo"
TRAE_DOUBAO_CODE = "Doubao-Seed-2.0-Code"


@pytest.mark.parametrize("channel", ["workbuddy", "qwenwork", "traework"])
def test_capacity_survives_supplier_catalog_and_public_listing(channel, isolated_db, all_channels):
    import catalog
    from providers.workbuddy.models import parse_supplier_models as wb
    from providers.qclaw.jprx import parse_model_list as qc
    from providers.qwenwork.models import parse_supplier_models as qw
    from providers.traework.models import parse_supplier_models as tw

    row = {"id": "capacity-test", "maxInputTokens": 1000000, "maxOutputTokens": 128000,
           "contextWindow": {"defaultLength": 300000, "supportedLengths": [300000, 1000000]}}
    parsed = {"workbuddy": wb, "qclaw": qc, "qwenwork": qw, "traework": tw}[channel]([row])
    normalized = catalog.normalize_models(parsed)
    catalog.save_catalog(channel, normalized)
    public_id = "capacity-test" if channel == "workbuddy" else f"{channel}/capacity-test"
    item = next(item for item in server.collect_v1_models() if item["id"] == public_id)
    assert item["context_window"] == 1000000
    assert item["max_output_tokens"] == 128000
    assert item["capacity_source"] == {"context_window": "catalog", "max_output_tokens": "catalog"}
    if channel == "workbuddy":
        prefixed = next(item for item in server.collect_v1_models() if item["id"] == f"workbuddy/{public_id}")
        assert prefixed["context_window"] == item["context_window"]


def test_public_models_omit_channels_without_login(monkeypatch):
    # A channel is only advertised when it has an enabled account holding a
    # token, so the stub rows carry the same fields a real row would.
    monkeypatch.setattr(server.db, "list_accounts", lambda provider=None: [
        {"provider": "qoder", "status": "active", "access_token": "dt-present"},
        {"provider": "workbuddy-i", "status": "active", "access_token": ""},
        {"provider": "traework", "status": "inactive", "access_token": "dt-disabled"},
    ])
    monkeypatch.setattr(server, "collect_v1_models", lambda: [
        {"id": "qoder/Auto", "channel": "qoder"},
        {"id": "qoder-i/Auto", "channel": "qoder-i"},
        {"id": "workbuddy-i/glm-5.2", "channel": "workbuddy-i"},
        {"id": "traework/doubao", "channel": "traework"},
        {"id": "glm-5.2", "channel": "workbuddy"},
    ])
    assert {item["id"] for item in server.public_v1_models()} == {"qoder/Auto"}


def test_all_channels_publish_capacity_defaults_without_persisting_guesses(isolated_db, all_channels):
    import catalog

    for channel in ("workbuddy", "qwenwork", "traework"):
        catalog.save_catalog(channel, [{"id": "unknown"}])
    items = server.collect_v1_models()
    assert len(items) == 4
    for item in items:
        assert item["context_window"] == 262144
        assert item["max_output_tokens"] == 32768
        assert set(item["capacity_source"].values()) == {"fallback"}
        assert "reasoning" not in item
        assert "supportsReasoning" not in item
        assert catalog.stored_catalog(item["channel"]) == [{"id": "unknown"}]


def test_workbuddy_reasoning_survives_supplier_catalog_and_public_listing(isolated_db, all_channels):
    import catalog
    from providers.workbuddy.models import parse_supplier_models

    payload = {
        "data": {
            "models": [
                {
                    "id": "glm-5.3",
                    "name": "GLM-5.3",
                    "supportsReasoning": True,
                    "reasoning": {
                        "supportedEfforts": ["low", "high", "max", "max", ""],
                        "defaultEffort": "high",
                        "canDisableThinking": True,
                    },
                },
                {
                    "id": "deepseek-v4-pro",
                    "supportsReasoning": True,
                    "reasoning": {"effort": "high", "summary": "auto"},
                },
                {
                    "id": "hy4-preview",
                    "supportsReasoning": True,
                    "reasoning": {"supportedEfforts": ["high"], "defaultEffort": "high"},
                },
            ]
        }
    }
    parsed = parse_supplier_models(payload)
    by_id = {item["id"]: item for item in parsed}
    assert by_id["glm-5.3"]["reasoning"]["supportedEfforts"] == ["low", "high", "max"]
    assert by_id["glm-5.3"]["reasoning"]["defaultEffort"] == "high"
    assert by_id["glm-5.3"]["supportsReasoning"] is True
    assert "supportedEfforts" not in by_id["deepseek-v4-pro"]["reasoning"]
    assert by_id["deepseek-v4-pro"]["reasoning"]["defaultEffort"] == "high"

    catalog.save_catalog("workbuddy", catalog.normalize_models(parsed))
    public = {item["id"]: item for item in server.collect_v1_models()}
    listed = public["glm-5.3"]
    prefixed = public["workbuddy/glm-5.3"]
    assert listed["reasoning"]["supportedEfforts"] == ["low", "high", "max"]
    assert listed["reasoning"]["defaultEffort"] == "high"
    assert listed["reasoning"]["canDisableThinking"] is True
    assert listed["supportsReasoning"] is True
    assert prefixed["reasoning"] == listed["reasoning"]
    assert public["deepseek-v4-pro"]["reasoning"] == {"defaultEffort": "high"}
    assert public["hy4-preview"]["reasoning"]["supportedEfforts"] == ["high"]
    stored = catalog.stored_catalog("workbuddy")
    assert stored is not None
    assert "summary" not in stored[1].get("reasoning", {})


def test_empty_or_invalid_reasoning_is_not_published():
    from model_reasoning import reasoning_fields

    assert reasoning_fields({"id": "x", "reasoning": {"supportedEfforts": []}}) == {}
    assert reasoning_fields({"id": "x", "supportedEfforts": ["low", 1, None]}) == {
        "reasoning": {"supportedEfforts": ["low"]}
    }
    assert reasoning_fields({"id": "x", "supportsReasoning": "true"}) == {}
    assert reasoning_fields(["not-a-row"]) == {}


def test_other_channels_do_not_invent_reasoning_from_thinking_tags(isolated_db, all_channels):
    import catalog
    from providers.qwenwork.models import parse_supplier_models as parse_qwen
    from providers.traework.models import parse_supplier_models as parse_trae

    catalog.save_catalog("qwenwork", catalog.normalize_models(parse_qwen({
        "qwork": [{"key": "pro", "enable": True, "is_reasoning": False}],
    })))
    catalog.save_catalog("traework", catalog.normalize_models(parse_trae({
        "data": {"list": [{"models": [{
            "name": "glm-5.3",
            "reasoning_effort_config": {"support_thinking": False, "options": None},
        }]}]},
    })))
    public = {item["id"]: item for item in server.collect_v1_models()}
    for model_id in ("qwenwork/pro", "traework/glm-5.3"):
        assert "reasoning" not in public[model_id]
        assert "supportsReasoning" not in public[model_id]


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5, "1000000", None, {}])
def test_invalid_capacity_values_fall_back(bad):
    from model_capacity import discovery_capacity

    item = discovery_capacity({"maxInputTokens": bad, "maxOutputTokens": bad})
    assert item["context_window"] == 262144
    assert item["max_output_tokens"] == 32768


def test_string_models_and_partial_capacity_remain_supported():
    import catalog
    from model_capacity import discovery_capacity
    from providers.qclaw.jprx import parse_model_list
    from providers.traework.models import parse_supplier_models

    assert catalog.normalize_models(["legacy"]) == [{"id": "legacy", "name": "legacy"}]
    assert parse_model_list(["legacy"])[0]["id"] == "legacy"
    assert parse_supplier_models(["legacy"])[0]["id"] == "legacy"
    item = discovery_capacity({"max_output_tokens": 64000})
    assert item["context_window"] == 262144
    assert item["max_output_tokens"] == 64000
    assert item["capacity_source"] == {"context_window": "fallback", "max_output_tokens": "catalog"}


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_explicit_output_budget_is_clamped_only_to_known_capacity(field):
    from model_capacity import clamp_output_tokens

    payload = {field: 128000}
    assert clamp_output_tokens(payload, {"max_output_tokens": 64000})[field] == 64000
    assert payload[field] == 128000
    assert clamp_output_tokens(payload, {})[field] == 128000
    assert clamp_output_tokens({field: 8000}, {"max_output_tokens": 64000})[field] == 8000
    assert clamp_output_tokens({}, {"max_output_tokens": 64000}) == {}


@pytest.mark.parametrize("responses_api", [False, True])
def test_dispatch_clamps_capacity_after_alias_resolution(monkeypatch, responses_api):
    class Provider:
        def translate_model(self, model):
            assert model == "alias"
            return "real-model"

        def accepts_model(self, model):
            return True

        def list_models(self):
            return [{"id": "real-model", "max_output_tokens": 64000}]

        async def chat_completions(self, payload, info):
            assert payload["model"] == "real-model"
            assert payload["max_tokens"] == 64000
            return ("json", {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(providers, "get_provider", lambda channel: Provider())
    # One literal account, so the queue is not empty and the walk reaches dispatch.
    monkeypatch.setattr(
        routing, "channel_pool",
        lambda channel: [{"id": 1, "provider": channel, "status": "active", "priority": 0}],
    )
    if responses_api:
        result = asyncio.run(router.responses_after_name("workbuddy/alias", {"input": "hello", "max_output_tokens": 128000}, None))
    else:
        result = asyncio.run(router.chat_after_name("workbuddy/alias", {"messages": [], "max_tokens": 128000}, None))
    assert result[0] == "json"

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

QWENWORK_HTTP_PAYLOAD = {
    "qwork": [
        {"key": "pro", "display_name": "高级", "enable": True},
        {"key": QWEN_NEW_ID, "display_name": "Qwen Live Only", "enable": True},
        {"key": "disabled-model", "display_name": "Hidden", "enable": False},
    ]
}

WORKBUDDY_HTTP_PAYLOAD = {
    "code": 0,
    "msg": "ok",
    "data": {
        "models": [
            {"id": "auto", "name": "Auto", "tags": ["craft"]},
            {"id": "glm-5.2", "name": "GLM-5.2", "tags": ["craft"]},
            {"id": WB_NEW_ID, "name": "WB Live Only", "tags": []},
            {"id": "hunyuan-image-v3.0", "name": "Hunyuan Image V3", "tags": ["text-to-image"]},
            {"id": 123, "name": "numeric-id-skipped"},
        ]
    },
}

TRAEWORK_HTTP_PAYLOAD = {
    "code": 0,
    "message": "success",
    "data": {
        "list": [
            {
                "function": "solo_work_remote",
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
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


@pytest.mark.parametrize("channel", ["workbuddy", "qwenwork", "traework"])
def test_catalog_refresh_allows_zero_quota_without_changing_chat_gate(
    isolated_db, all_channels, monkeypatch, channel,
):
    import catalog

    aid = db.add_account({
        "name": "zero-quota", "uid": "zero-quota", "provider": channel,
        "access_token": "token", "expires_at": 9_999_999_999_999,
    })
    db.merge_account_resource_cache(aid, {"ok": True, "remaining": 0.0})
    account = db.get_account(aid)
    assert not routing.account_available(account)
    before = routing.quota_exempt_channels()

    async def fetch(selected):
        assert selected["id"] == aid
        assert routing.quota_exempt_channels() == before
        return [{"id": "live-model"}]

    monkeypatch.setitem(catalog.LIVE_FETCHERS, channel, fetch)
    result = asyncio.run(catalog.refresh_one(channel))
    assert result["mode"] == "live"
    assert result["models"][0]["id"] == "live-model"
    assert routing.quota_exempt_channels() == before
    assert not routing.account_available(account)
    assert providers.get_provider(channel).pick_account() is None


def test_catalog_refresh_still_rejects_disabled_account(isolated_db, all_channels, monkeypatch):
    import catalog

    db.add_account({
        "name": "disabled", "uid": "disabled", "provider": "traework",
        "status": "inactive", "access_token": "token",
    })

    async def fetch(_account):
        pytest.fail("Disabled accounts must not fetch catalogs")

    monkeypatch.setitem(catalog.LIVE_FETCHERS, "traework", fetch)
    result = asyncio.run(catalog.refresh_one("traework"))
    assert result["mode"] == "fallback"
    assert result["message"] == "no usable account"
    assert routing.quota_exempt_channels() == frozenset()


def _seed_live_accounts():
    db.add_account(
        {
            "name": "wb",
            "uid": "wb-1",
            "provider": "workbuddy",
            "status": "active",
            "access_token": "tok-wb",
            "expires_at": 9_999_999_999_999,
        }
    )
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
    db.add_account(
        {
            "name": "qw",
            "uid": "qw-1",
            "provider": "qwenwork",
            "status": "active",
            "access_token": "tok-qwen",
            "extra": {"login_device_id": "dev-qw", "email": "a@b"},
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
                assert kwargs.get("params") == {
                    "functions": "solo_work_remote",
                    "show_custom_model": "true",
                }
                return _FakeResponse(TRAEWORK_HTTP_PAYLOAD)
            if "/api/v2/model/list" in str(url):
                return _FakeResponse(QWENWORK_HTTP_PAYLOAD)
            if "/v2/enterprises/personal/models" in str(url):
                return _FakeResponse(WORKBUDDY_HTTP_PAYLOAD)
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("providers.qclaw.jprx.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("providers.traework.models.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("providers.qwenwork.models.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("providers.workbuddy.models.httpx.AsyncClient", FakeAsyncClient)
    return requested


def _by_channel(result):
    return {item["channel"]: item for item in result["sources"]}


def test_web_and_cli_version_come_from_version_py():
    from version import VERSION

    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "__APP_VERSION__" in html
    assert "v2.1." not in html
    rendered = server._render_index_html()
    assert f"wicm84266964 · v{VERSION}" in rendered
    assert "__APP_VERSION__" not in rendered
    assert f"Buddy 2 API v{{VERSION}}" in Path(server.__file__).read_text(encoding="utf-8")


def test_admin_models_page_has_one_click_control():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "一键读取供应模型" in html
    assert "/admin/models/refresh" in html
    assert "syncSources" in html
    assert "请选择通道" in html
    assert "addForm.channel" in html
    assert "/admin/models/catalogs" in html
    assert "submitAdd" in html
    assert "aliasChannel" in html
    assert "别名映射" in html
    assert "编辑模型" not in html
    assert "editingModels" not in html


def test_dashboard_does_not_block_on_credit_summary():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "api.get('/admin/stats'" in html
    assert "api.get('/admin/credit-summary'" in html
    assert "Promise.all([api.get('/admin/stats'" not in html
    assert "正在读取上游额度" in html


def test_dashboard_uses_line_and_pie_charts():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "hour-line" in html
    assert "hour-poly" in html
    assert "hour-bars" in html
    assert "trend-bars" in html
    assert "mini-bars" in html
    assert "qualityPie" in html
    assert "modelPie" in html
    assert "donut-split" in html
    assert "mini-line" in html
    assert "近 7 日" in html
    assert "昨日" in html
    assert "七天" in html
    assert "30天" in html
    assert "柱状" in html
    assert "面积" in html
    assert "statsPeriod" in html
    assert "s.value?.periods" in html
    assert "/admin/stats?period=" in html
    assert "模型占比" in html


def test_account_test_ui_lets_user_pick_model():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "openTest" in html
    assert "runTest" in html
    assert "test.model" in html
    assert "开始测试" in html
    assert "{model:'auto',prompt:'ping'}" not in html


def test_supplier_catalog_refresh_keeps_channels_distinct(isolated_db, all_channels, monkeypatch):
    assert QCLAW_NEW_ID not in QCLAW_STATIC
    assert TRAE_NEW_DOUBAO not in TRAE_STATIC
    assert TRAE_DOUBAO_TURBO in TRAE_STATIC

    traework = providers.get_provider("traework")
    workbuddy = providers.get_provider("workbuddy")
    qwenwork = providers.get_provider("qwenwork")

    assert TRAE_NEW_DOUBAO not in _ids(traework.list_models())
    assert WB_NEW_ID not in _ids(workbuddy.list_models())
    assert not traework.accepts_model(TRAE_NEW_DOUBAO)
    assert not workbuddy.accepts_model(WB_NEW_ID)

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

    assert "qclaw" not in sources
    assert sources["traework"]["mode"] == "live"
    assert sources["workbuddy"]["mode"] == "live"
    assert sources["qwenwork"]["mode"] == "live"

    assert TRAE_NEW_DOUBAO in _ids(sources["traework"]["models"])
    assert TRAE_DOUBAO_TURBO in _ids(sources["traework"]["models"])

    wb_ids = _ids(sources["workbuddy"]["models"])
    assert WB_NEW_ID in wb_ids
    assert "glm-5.2" in wb_ids
    assert "auto" in wb_ids
    assert "hunyuan-image-v3.0" not in wb_ids
    assert QCLAW_NEW_ID not in wb_ids
    assert TRAE_NEW_DOUBAO not in wb_ids
    assert TRAE_DOUBAO_TURBO not in wb_ids

    qwen_ids = _ids(sources["qwenwork"]["models"])
    assert "pro" in qwen_ids
    assert QWEN_NEW_ID in qwen_ids
    assert "disabled-model" not in qwen_ids
    assert TRAE_NEW_DOUBAO not in qwen_ids
    assert QCLAW_NEW_ID not in qwen_ids

    assert any("/api/remote/v1/models" in url for method, url in requested if method == "GET")
    assert any("/api/v2/model/list" in url for method, url in requested if method == "GET")
    assert any("/v2/enterprises/personal/models" in url for method, url in requested if method == "GET")
    copilot_urls = [url for _, url in requested if "copilot.tencent.com" in url]
    assert copilot_urls
    assert all("/v2/enterprises/personal/models" in url for url in copilot_urls)

    assert TRAE_NEW_DOUBAO in _ids(traework.list_models())
    assert QWEN_NEW_ID in _ids(qwenwork.list_models())
    assert WB_NEW_ID in _ids(workbuddy.list_models())
    assert traework.accepts_model(TRAE_NEW_DOUBAO)
    assert qwenwork.accepts_model(QWEN_NEW_ID)
    assert qwenwork.accepts_model("pro")
    assert traework.accepts_model(TRAE_DOUBAO_TURBO)
    assert workbuddy.accepts_model(WB_NEW_ID)
    assert not workbuddy.accepts_model("hunyuan-image-v3.0")
    assert not workbuddy.accepts_model(TRAE_NEW_DOUBAO)
    assert not workbuddy.accepts_model(TRAE_DOUBAO_TURBO)
    assert not qwenwork.accepts_model(TRAE_NEW_DOUBAO)
    assert not qwenwork.accepts_model(QCLAW_NEW_ID)

    traework_key = {"default_channel": "traework"}
    assert router.admit_model(traework_key, "traework/" + TRAE_NEW_DOUBAO) == TRAE_NEW_DOUBAO
    assert [
        channel
        for channel, _row in router.account_queue("traework/" + TRAE_NEW_DOUBAO, traework_key)
    ] == ["traework"]

    def attempt_chat(payload, key):
        name = router.admit_model(key, payload["model"])
        return asyncio.run(router.chat_after_name(name, payload, key))

    with pytest.raises(HTTPException) as err:
        attempt_chat({"model": TRAE_NEW_DOUBAO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    assert err.value.detail["error"]["code"] == "unknown_model"
    with pytest.raises(HTTPException) as err:
        attempt_chat({"model": TRAE_DOUBAO_TURBO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    assert err.value.detail["error"]["code"] == "unknown_model"
    with pytest.raises(HTTPException) as err:
        attempt_chat({"model": "traework/" + TRAE_NEW_DOUBAO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    assert err.value.detail["error"]["code"] == "model_not_allowed"
    with pytest.raises(HTTPException) as err:
        attempt_chat({"model": QCLAW_NEW_ID, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    assert err.value.detail["error"]["code"] == "unknown_model"

    assert chat_hits == []

    payload = {"object": "list", "data": server.collect_v1_models()}
    data = payload["data"]
    by_id = {item["id"]: item for item in data}

    assert "qclaw/" + QCLAW_NEW_ID not in by_id
    assert by_id["traework/" + TRAE_NEW_DOUBAO]["channel"] == "traework"
    assert by_id["traework/" + TRAE_DOUBAO_TURBO]["channel"] == "traework"
    assert TRAE_NEW_DOUBAO not in by_id
    assert TRAE_DOUBAO_TURBO not in by_id
    assert QCLAW_NEW_ID not in by_id
    assert "glm-5.2" in by_id
    assert by_id["glm-5.2"]["channel"] == "workbuddy"
    assert by_id["workbuddy/glm-5.2"]["channel"] == "workbuddy"
    assert WB_NEW_ID in by_id
    assert by_id[WB_NEW_ID]["channel"] == "workbuddy"
    assert by_id["workbuddy/" + WB_NEW_ID]["channel"] == "workbuddy"
    assert "hunyuan-image-v3.0" not in by_id

    evidence = os.environ.get("BUDDY2API_EVIDENCE_DIR")
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        Path(evidence, "v1-models.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def test_parse_workbuddy_supplier_models_skips_image_and_non_string_ids():
    from providers.workbuddy.models import parse_supplier_models

    models = parse_supplier_models(WORKBUDDY_HTTP_PAYLOAD)
    ids = {item["id"] for item in models}
    assert ids == {"auto", "glm-5.2", WB_NEW_ID}
    assert next(item["name"] for item in models if item["id"] == WB_NEW_ID) == "WB Live Only"


def test_manual_add_goes_to_selected_channel(isolated_db, all_channels):
    import catalog

    custom_qw = "qwork-user-added"
    custom_wb = "wb-user-added"
    custom_tw = "trae-user-added"

    qwenwork = providers.get_provider("qwenwork")
    workbuddy = providers.get_provider("workbuddy")
    traework = providers.get_provider("traework")

    catalog.upsert_model("qwenwork", custom_qw, "Qwen extra")
    catalog.upsert_model("workbuddy", custom_wb, "WB extra")
    catalog.upsert_model("traework", custom_tw, "Trae extra")

    assert qwenwork.accepts_model(custom_qw)
    assert not workbuddy.accepts_model(custom_qw)
    assert not traework.accepts_model(custom_qw)
    assert workbuddy.accepts_model(custom_wb)
    assert not qwenwork.accepts_model(custom_wb)
    assert traework.accepts_model(custom_tw)
    assert not workbuddy.accepts_model(custom_tw)

    by_id = {item["id"]: item for item in server.collect_v1_models()}
    assert by_id["qwenwork/" + custom_qw]["channel"] == "qwenwork"
    assert custom_qw not in by_id
    assert custom_wb in by_id
    assert by_id[custom_wb]["channel"] == "workbuddy"
    assert by_id["traework/" + custom_tw]["channel"] == "traework"
    assert custom_tw not in by_id

    snap = {item["channel"]: item for item in catalog.catalog_snapshot()["sources"]}
    qw_manual = [item for item in snap["qwenwork"]["models"] if item.get("id") == custom_qw]
    assert qw_manual and qw_manual[0].get("manual") is True
    wb_manual = [item for item in snap["workbuddy"]["models"] if item.get("id") == custom_wb]
    assert wb_manual and wb_manual[0].get("manual") is True

    catalog.remove_model("qwenwork", custom_qw)
    assert not qwenwork.accepts_model(custom_qw)
    catalog.remove_model("workbuddy", custom_wb)
    assert not workbuddy.accepts_model(custom_wb)


def test_manual_extra_survives_live_refresh(isolated_db, all_channels, monkeypatch):
    import catalog

    extra = "trae-hand-added"
    wb_extra = "wb-hand-added"
    catalog.upsert_model("traework", extra, "Hand added")
    catalog.upsert_model("workbuddy", wb_extra, "WB hand added")
    assert extra not in _ids(TRAE_STATIC)
    assert providers.get_provider("traework").accepts_model(extra)
    assert providers.get_provider("workbuddy").accepts_model(wb_extra)

    _seed_live_accounts()
    _install_supplier_http(monkeypatch)
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    result = asyncio.run(server.admin_refresh_models())
    sources = _by_channel(result)

    assert sources["traework"]["mode"] == "live"
    assert extra in _ids(sources["traework"]["models"])
    assert providers.get_provider("traework").accepts_model(extra)
    assert extra not in _ids(providers.get_provider("workbuddy").list_models())
    assert sources["workbuddy"]["mode"] == "live"
    assert wb_extra in _ids(sources["workbuddy"]["models"])
    assert WB_NEW_ID in _ids(sources["workbuddy"]["models"])
    assert providers.get_provider("workbuddy").accepts_model(wb_extra)


def test_manual_add_rejects_unknown_channel(isolated_db, all_channels):
    import catalog

    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("not-a-channel", "foo")
    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("qwenwork", "")
    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("qwenwork", "qwork-advanced")
    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("workbuddy", "glm-5.2")
    with pytest.raises(catalog.CatalogError):
        catalog.remove_model("workbuddy", "glm-5.2")
