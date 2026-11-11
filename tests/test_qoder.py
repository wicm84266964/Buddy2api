import asyncio
import json
import time

import pytest
from fastapi import HTTPException

import credential_crypto
import database as db
import providers
import router
from providers.qoder.encode import encode_body
from providers.qoder.constants import upstream_key
from providers.qoder.checkin import claim_checkin, describe_campaigns, fetch_checkin
from providers.qoder.quota import parse_usage


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_qoder_regions_do_not_share_intl_only_models():
    cn = providers.get_provider("qoder")
    intl = providers.get_provider("qoder-i")
    assert cn.accepts_model("Qwen3.8-Flash")
    assert intl.accepts_model("Ultimate")
    assert not cn.accepts_model("Ultimate")


def test_qoder_prefix_stays_on_its_own_key(isolated_db):
    db.add_account({"name": "cn", "uid": "cn", "provider": "qoder", "priority": 0})
    db.add_account({"name": "intl", "uid": "intl", "provider": "qoder-i", "priority": 0})
    key = {"default_channel": "qoder-i"}
    assert router.admit_model(key, "qoder-i/Ultimate") == "Ultimate"
    # The intl-only model is reached through the qoder-i channel and nowhere else.
    assert [channel for channel, _row in router.account_queue("qoder-i/Ultimate", key)] == ["qoder-i"]
    with pytest.raises(HTTPException) as err:
        router.admit_model({"default_channel": "qoder"}, "qoder-i/Ultimate")
    assert err.value.status_code == 400
    assert err.value.detail["error"]["code"] == "model_not_allowed"


def test_qoder_encode_reorders_and_replaces_padding():
    encoded = encode_body(b"{}")
    assert b"{" not in encoded
    assert b"$" in encoded or len(encoded) == len(__import__("base64").b64encode(b"{}"))


def test_qoder_envelope_extracts_inner_content():
    from providers.qoder.chat import _texts_from_event

    line = 'data: {"statusCodeValue":200,"body":"{\\"choices\\":[{\\"delta\\":{\\"content\\":\\"好\\"}}]}"}'
    assert _texts_from_event(line) == ["好"]


def test_qoder_model_list_uses_chat_group():
    from providers.qoder.models import parse_supplier_models

    models = parse_supplier_models({
        "chat": [{"key": "qfmodel", "display_name": "Qwen3.8-Flash", "enable": True}],
        "quest": [{"key": "other", "display_name": "Other", "enable": True}],
    })
    assert [item["id"] for item in models] == ["Qwen3.8-Flash"]


def test_upstream_key_is_region_local():
    assert upstream_key("qoder", "Qwen3.8-Flash") == "qfmodel"
    assert upstream_key("qoder-i", "Ultimate") == "ultimate"


def test_campaign_list_is_top_level_and_claimed_is_not_empty():
    described = describe_campaigns({
        "showCampaign": True,
        "claimable": False,
        "campaigns": [
            {
                "actionType": "CLAIM_BENEFIT",
                "claimStatus": "CLAIMED",
                "campaignId": "act-1",
                "benefit": {"kind": "CREDITS", "amount": 100, "validity": {"days": 30}},
            },
            {"actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED", "campaignId": "promo"},
        ],
    })
    assert described["state"] == "claimed"
    assert described["credit"] == 100
    assert "100" in described["message"]


def test_missing_campaign_is_not_marked_already_claimed():
    described = describe_campaigns({"campaigns": [{"actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED"}]})
    assert described["state"] == "none"
    assert described["credit"] == 0


def test_usage_adds_plan_and_resource_pack():
    parsed = parse_usage({
        "userQuota": {"total": 300, "used": 0, "remaining": 300, "unit": "credits"},
        "addOnQuota": {"total": 100, "used": 0, "remaining": 100, "unit": "credits"},
        "orgResourcePackage": {"cap": 0, "remaining": 0, "available": False, "unit": "credits"},
    })
    assert parsed["remaining"] == 400
    assert [row["package_name"] for row in parsed["packages"]] == ["套餐额度", "个人资源包"]


def test_fetch_checkin_reads_claimed_campaign(isolated_db, monkeypatch):
    from providers.qoder import checkin

    account_id = db.add_account({
        "name": "qoder",
        "uid": "q-1",
        "provider": "qoder",
        "access_token": "dt-test",
        "refresh_token": "drt-test",
        "status": "active",
    })
    account = db.get_account(account_id)
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "campaigns": [{
                    "actionType": "CLAIM_BENEFIT",
                    "claimStatus": "CLAIMED",
                    "campaignId": "act-1",
                    "benefit": {"amount": 100},
                }]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(fetch_checkin(account, force=True))
    assert result["already_claimed"] is True
    assert result["no_campaign"] is False
    assert result["credit"] == 100
    assert calls[0].endswith("/sash/api/v1/me/campaigns?forceRefresh=true")
    cached = asyncio.run(fetch_checkin(account, force=False))
    assert cached["already_claimed"] is True
    assert len(calls) == 1


def test_chat_stream_keeps_reasoning_out_of_content():
    from providers.qoder.chat import _texts_from_event

    line = 'data: {"statusCodeValue":200,"body":"{\\"choices\\":[{\\"delta\\":{\\"reasoning_content\\":\\"think\\",\\"content\\":\\"pong\\"}}]}"}'
    assert _texts_from_event(line, include_reasoning=False) == ["pong"]
    thinking = 'data: {"statusCodeValue":200,"body":"{\\"choices\\":[{\\"delta\\":{\\"reasoning_content\\":\\"think\\"}}]}"}'
    assert _texts_from_event(thinking, include_reasoning=False) == []


def test_qoder_stream_forwards_tool_calls():
    from providers.qoder.chat import _parts_from_event

    inner = json.dumps({
        "choices": [{
            "delta": {
                "content": "我来查一下：",
                "tool_calls": [{
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"},
                }],
            }
        }]
    })
    line = "data: " + json.dumps({"statusCodeValue": 200, "body": inner})
    texts, calls = _parts_from_event(line, include_reasoning=False)
    assert texts == ["我来查一下："]
    assert calls[0]["id"] == "call-1"
    assert calls[0]["function"]["name"] == "read_file"


def test_prepare_messages_gives_tool_only_assistant_visible_content():
    from providers.qoder.chat import prepare_messages

    prepared = prepare_messages([
        {"role": "user", "content": "查一下"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "list_files", "arguments": {"path": "."}},
            }],
        },
        {"role": "user", "content": "继续"},
        {"role": "tool", "tool_call_id": "call-1", "content": "empty"},
    ])
    assistant = prepared[1]
    assert assistant["content"] == " "
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path":"."}'
    assert prepared[2] == {"role": "tool", "tool_call_id": "call-1", "content": "empty"}
    assert prepared[3]["role"] == "user"


def test_prepare_messages_pairs_tool_results_and_keeps_orphans():
    from providers.qoder.chat import prepare_messages

    prepared = prepare_messages([
        {"role": "user", "content": "读文件"},
        {
            "role": "assistant",
            "content": "我来读。",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
        },
        {"role": "user", "content": "顺便说一下"},
        {"role": "tool", "toolCallId": "call-1", "content": [{"type": "text", "text": "file ok"}]},
        {"role": "tool", "tool_call_id": "missing", "content": "no pair"},
    ])
    assert [item["role"] for item in prepared] == ["user", "assistant", "tool", "user", "user"]
    assert prepared[2]["tool_call_id"] == "call-1"
    assert prepared[2]["content"] == "file ok"
    assert "no pair" in prepared[4]["content"]


def test_account_test_reads_envelope_text(monkeypatch):
    from providers.qoder import chat

    class FakeResponse:
        status_code = 200

        async def aiter_lines(self):
            yield 'data: {"statusCodeValue":200,"body":"{\\"choices\\":[{\\"delta\\":{\\"content\\":\\"pong\\"}}]}"}'

        async def aread(self):
            return b""

    class FakeStream:
        def __init__(self):
            self.response = FakeResponse()

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, method, url, headers=None, content=None):
            assert method == "POST"
            assert "agent_chat_generation" in url
            return FakeStream()

    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(chat.cosy, "auth_headers", lambda **kwargs: {"Authorization": "test"})
    result = asyncio.run(chat.test_chat({"provider": "qoder", "access_token": "dt-test", "uid": "u"}, "auto", "ping"))
    assert result["ok"] is True
    assert result["message"] == "pong"
    assert providers.get_provider("qoder").test_chat is not None


def test_claim_posts_only_claimable_benefit(isolated_db, monkeypatch):
    from providers.qoder import checkin

    account = {
        "id": db.add_account({
            "name": "qoder",
            "uid": "q-2",
            "provider": "qoder",
            "access_token": "dt-test",
            "refresh_token": "drt-test",
            "status": "active",
        }),
        "provider": "qoder",
        "access_token": "dt-test",
    }
    posts = []

    class Listed:
        status_code = 200

        def json(self):
            return {
                "campaigns": [{
                    "actionType": "CLAIM_BENEFIT",
                    "claimStatus": "CLAIMABLE",
                    "campaignId": "act-9",
                    "benefit": {"amount": 100},
                }]
            }

    class Claimed:
        status_code = 200
        text = ""

        def json(self):
            return {"status": "CLAIMED", "benefit": {"amount": 100, "validity": {"days": 30}}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return Listed()

        async def post(self, url, headers=None, json=None):
            posts.append((url, json))
            return Claimed()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(claim_checkin(account))
    assert result["claimed"] is True
    assert result["credit"] == 100
    assert posts[0][0].endswith("/sash/api/v1/me/campaigns/act-9/claim")
    assert posts[0][1] == {}


def test_campaign_validity_days_are_parsed():
    from providers.qoder.checkin import _benefit_valid_days

    assert _benefit_valid_days({"benefit": {"amount": 100, "validity": {"days": 30}}}) == 30
    assert _benefit_valid_days({"benefit": {"amount": 100, "validity": 7}}) == 7
    assert _benefit_valid_days({"benefit": {"amount": 100}}) is None
    assert _benefit_valid_days({"benefit": {"amount": 100, "validity": {"days": 0}}}) is None
    assert _benefit_valid_days({}) is None

    described = describe_campaigns(
        {
            "campaigns": [
                {
                    "actionType": "CLAIM_BENEFIT",
                    "claimStatus": "CLAIMED",
                    "campaignId": "act-1",
                    "benefit": {"kind": "CREDITS", "amount": 100, "validity": {"days": 30}},
                }
            ]
        }
    )
    assert described["state"] == "claimed"
    assert described["valid_days"] == 30


def test_claimed_campaign_caches_the_credits_deadline(isolated_db, monkeypatch):
    import control_plane
    import routing
    from providers.qoder import checkin

    account_id = db.add_account(
        {
            "name": "qoder",
            "uid": "q-deadline",
            "provider": "qoder",
            "access_token": "dt-test",
            "status": "active",
        }
    )
    account = db.get_account(account_id)

    class Listed:
        status_code = 200

        def json(self):
            return {
                "campaigns": [
                    {
                        "actionType": "CLAIM_BENEFIT",
                        "claimStatus": "CLAIMED",
                        "campaignId": "act-1",
                        "benefit": {"kind": "CREDITS", "amount": 100, "validity": {"days": 30}},
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return Listed()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(control_plane.checkin_status_one(account, force=True))
    assert result["already_claimed"] is True
    assert result["expire_source"] == checkin.CAMPAIGN_EXPIRE_SOURCE
    assert time.time() + 30 * 86400 - 60 <= float(result["next_expire_ts"]) <= time.time() + 30 * 86400 + 60

    fresh = db.get_account(account_id)
    snapshot = routing.resource_snapshot(fresh)
    assert snapshot["next_expire_days"] == 30
    assert snapshot["expires_at"] == snapshot["next_expire_ts"]
    assert routing.urgency_source(fresh, "qoder") == "quota"
    assert routing.urgency_days(fresh, "qoder") == 30.0


def test_claim_caches_the_not_granted_validity(isolated_db, monkeypatch):
    import control_plane
    import routing
    from providers.qoder import checkin

    account_id = db.add_account(
        {
            "name": "qoder",
            "uid": "q-claim-deadline",
            "provider": "qoder",
            "access_token": "dt-test",
            "status": "active",
        }
    )
    account = db.get_account(account_id)

    class Listed:
        status_code = 200

        def json(self):
            return {
                "campaigns": [
                    {
                        "actionType": "CLAIM_BENEFIT",
                        "claimStatus": "CLAIMABLE",
                        "campaignId": "act-9",
                        "benefit": {"amount": 100},
                    }
                ]
            }

    class Granted:
        status_code = 200
        text = ""

        def json(self):
            return {"status": "CLAIMED", "benefit": {"amount": 100, "validity": {"days": 15}}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return Listed()

        async def post(self, url, headers=None, json=None):
            return Granted()

    monkeypatch.setattr(checkin.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(control_plane.claim_one(account))
    assert result["claimed"] is True
    assert "next_expire_ts" in result

    snapshot = routing.resource_snapshot(db.get_account(account_id))
    assert snapshot["next_expire_days"] == 15
    assert routing.urgency_days(db.get_account(account_id), "qoder") == 15.0

@pytest.mark.parametrize("stream", [False, True])
def test_qoder_failover_reaches_fifth_account(isolated_db, monkeypatch, stream):
    import httpx
    from providers.qoder import chat
    accounts = [db.add_account({"name": str(i), "uid": str(i), "provider": "qoder"}) for i in range(5)]
    visited = []

    async def pick(channel, excluded):
        return next((db.get_account(aid) for aid in accounts if aid not in excluded), None)

    def signed(account, channel, raw):
        return "https://upstream.test/chat", {"X-Account": str(account["id"])}, b"{}"

    def respond(request):
        aid = int(request.headers["X-Account"])
        visited.append(aid)
        if aid != accounts[-1]:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"fifth"}}]}\n\n')

    original_client = httpx.AsyncClient
    monkeypatch.setattr(chat, "_pick", pick)
    monkeypatch.setattr(chat, "_signed_request", signed)
    monkeypatch.setattr(chat.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(chat.auth_manager, "mark_account_failure", lambda *args: None)
    monkeypatch.setattr(chat.auth_manager, "mark_account_success", lambda *args: None)

    async def run():
        result = await chat.chat_completions("qoder", {"model": "auto", "messages": [{"role": "user", "content": "ping"}], "stream": stream}, None)
        if stream:
            return b"".join([chunk async for chunk in result[1]]).decode()
        assert result[0] == "json"
        return result[1]["choices"][0]["message"]["content"]
    assert "fifth" in asyncio.run(run())
    assert visited == accounts
    logs = db.list_logs()
    assert len(logs) == 5
    assert all(row["stream"] == int(stream) for row in logs)
    assert logs[0]["status_code"] == 200
