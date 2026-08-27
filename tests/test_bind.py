import asyncio
import json

import pytest
from fastapi import HTTPException

import providers
import router
from providers.protocol import (
    InvalidModel,
    KeyChannelMismatch,
    UnknownChannel,
    UnknownModel,
)


class _QwenStub:
    id = "qwenwork"
    display_name = "QwenWork"
    checkin_supported = False

    def __init__(self):
        self.last_payload = None
        self.last_api_key_info = None

    def list_models(self):
        return [{"id": "auto"}, {"id": "qwork-advanced"}]

    def alias_map(self):
        return {}

    def accepts_model(self, inner):
        return inner in {"auto", "qwork-advanced"}

    def translate_model(self, model):
        return model

    def pick_account(self, exclude_ids=None):
        return None

    async def pick_account_with_fallback(self, exclude_ids=None):
        return None

    async def has_usable_account(self):
        return False

    async def chat_completions(self, payload, api_key_info):
        self.last_payload = payload
        self.last_api_key_info = api_key_info
        return (
            "json",
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


@pytest.fixture()
def qwen_enabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qwenwork")
    stub = _QwenStub()
    previous = providers._LOADED.get("qwenwork")
    providers.register_provider(stub)
    yield stub
    if previous is not None:
        providers._LOADED["qwenwork"] = previous
    else:
        providers._LOADED.pop("qwenwork", None)


def test_unprefixed_auto_on_workbuddy_key():
    bound = router.bind({"model": "auto"}, {"default_channel": "workbuddy"})
    assert bound.channel == "workbuddy"
    assert bound.inner == "auto"
    assert bound.original == "auto"


def test_namespaced_workbuddy_strips_inner():
    bound = router.bind({"model": "workbuddy/glm-5.2"}, {"default_channel": "workbuddy"})
    assert bound.channel == "workbuddy"
    assert bound.inner == "glm-5.2"
    assert bound.original == "workbuddy/glm-5.2"


def test_workbuddy_gpt_alias_accepted():
    bound = router.bind({"model": "workbuddy/gpt-5.5"}, {"default_channel": "workbuddy"})
    assert bound.inner == "gpt-5.5"


def test_bare_qwork_advanced_on_workbuddy_key_rejected():
    with pytest.raises(UnknownModel):
        router.bind({"model": "qwork-advanced"}, {"default_channel": "workbuddy"})


def test_prefix_mismatch_is_403(qwen_enabled):
    with pytest.raises(KeyChannelMismatch):
        router.bind(
            {"model": "workbuddy/auto"},
            {"default_channel": "qwenwork"},
        )


def test_unprefixed_auto_on_qwenwork_key(qwen_enabled):
    bound = router.bind({"model": "auto"}, {"default_channel": "qwenwork"})
    assert bound.channel == "qwenwork"
    assert bound.inner == "auto"


def test_unprefixed_auto_on_disabled_qwenwork_key(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    with pytest.raises(UnknownChannel):
        router.bind({"model": "auto"}, {"default_channel": "qwenwork"})


def test_glm_on_qwenwork_key_rejected(qwen_enabled):
    with pytest.raises(UnknownModel):
        router.bind({"model": "glm-5.2"}, {"default_channel": "qwenwork"})


def test_qwenwork_flag_off_namespaced(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy")
    with pytest.raises(UnknownChannel):
        router.bind(
            {"model": "qwenwork/qwork-advanced"},
            {"default_channel": "qwenwork"},
        )


def test_qwenwork_namespaced_ok(qwen_enabled):
    bound = router.bind(
        {"model": "qwenwork/qwork-advanced"},
        {"default_channel": "qwenwork"},
    )
    assert bound.channel == "qwenwork"
    assert bound.inner == "qwork-advanced"


def test_qwenwork_glm_inner_invalid(qwen_enabled):
    with pytest.raises(InvalidModel):
        router.bind(
            {"model": "qwenwork/glm-5.2"},
            {"default_channel": "qwenwork"},
        )


def test_ensure_usable_503_does_not_need_workbuddy(qwen_enabled):
    with pytest.raises(HTTPException) as err:
        asyncio.run(router.ensure_usable("qwenwork"))
    assert err.value.status_code == 503
    assert err.value.detail["error"]["code"] == "channel_unavailable"


def test_responses_after_bind_dispatches_to_selected_provider(qwen_enabled, monkeypatch):
    async def fail_workbuddy(*_args, **_kwargs):
        raise AssertionError("Responses bypassed the selected provider")

    monkeypatch.setattr("proxy.proxy_chat_completions", fail_workbuddy)
    bound = router.bind(
        {"model": "qwenwork/qwork-advanced"},
        {"default_channel": "qwenwork"},
    )

    result = asyncio.run(router.responses_after_bind(
        bound,
        {
            "model": "qwenwork/qwork-advanced",
            "input": "hello",
            "reasoning": {"effort": "low"},
        },
        {"id": 7, "name": "qwen-key", "default_channel": "qwenwork"},
    ))

    assert result[0] == "json"
    assert result[1]["model"] == "qwenwork/qwork-advanced"
    assert result[1]["output"][0]["content"][0]["text"] == "ok"
    assert qwen_enabled.last_payload["model"] == "qwork-advanced"
    assert qwen_enabled.last_payload["reasoning_effort"] == "low"
    assert qwen_enabled.last_api_key_info["_bind_channel"] == "qwenwork"


def test_responses_after_bind_rewrites_nested_stream_model(qwen_enabled, monkeypatch):
    async def stream_response(payload, api_key_info):
        qwen_enabled.last_payload = payload
        qwen_enabled.last_api_key_info = api_key_info

        async def chunks():
            yield (
                'data: {"id":"chatcmpl-stub","model":"qwork-advanced",'
                '"choices":[{"index":0,"delta":{"content":"ok"},'
                '"finish_reason":null}]}\n\n'
            ).encode()
            yield (
                'data: {"id":"chatcmpl-stub","model":"qwork-advanced",'
                '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            ).encode()
            yield b"data: [DONE]\n\n"

        return ("stream", chunks())

    monkeypatch.setattr(qwen_enabled, "chat_completions", stream_response)
    original = "qwenwork/qwork-advanced"
    bound = router.bind(
        {"model": original},
        {"default_channel": "qwenwork"},
    )

    async def collect_events():
        result = await router.responses_after_bind(
            bound,
            {"model": original, "input": "hello", "stream": True},
            {"id": 7, "name": "qwen-key", "default_channel": "qwenwork"},
        )
        assert result[0] == "stream"
        return [chunk async for chunk in result[1]]

    chunks = asyncio.run(collect_events())
    events = [
        json.loads(line[6:])
        for chunk in chunks
        for line in (
            chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        ).splitlines()
        if line.startswith("data: {")
    ]
    snapshots = [event["response"] for event in events if "response" in event]

    assert snapshots
    assert all(snapshot["model"] == original for snapshot in snapshots)
    assert all(event.get("model", original) == original for event in events)


def test_responses_after_bind_preserves_provider_error(qwen_enabled, monkeypatch):
    expected = (429, {"error": {"message": "busy", "type": "rate_limit_error"}})

    async def fail_response(_payload, _api_key_info):
        return ("error", expected)

    monkeypatch.setattr(qwen_enabled, "chat_completions", fail_response)
    original = "qwenwork/qwork-advanced"
    bound = router.bind(
        {"model": original},
        {"default_channel": "qwenwork"},
    )

    result = asyncio.run(router.responses_after_bind(
        bound,
        {"model": original, "input": "hello"},
        {"id": 7, "name": "qwen-key", "default_channel": "qwenwork"},
    ))

    assert result == ("error", expected)
