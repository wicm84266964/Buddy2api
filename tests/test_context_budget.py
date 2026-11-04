import asyncio
from pathlib import Path

import context_budget
import router


def _long_chat(count: int, size: int = 200) -> dict:
    messages = [{"role": "system", "content": "keep"}]
    for index in range(count):
        messages.append({"role": "user", "content": "旧" * size, "turn": index})
    messages.append({"role": "user", "content": "最新问题"})
    return {"model": "GLM-5.3-Flash", "messages": messages}


def test_loomy_pretrim_drops_old_history_and_keeps_latest(monkeypatch):
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "500")
    payload = _long_chat(30, 40)
    trimmed, changed = context_budget.prepare_dispatch("loomy", payload)
    assert changed is True
    assert trimmed["messages"][-1]["content"] == "最新问题"
    assert trimmed["messages"][0]["content"] == "keep"
    assert len(trimmed["messages"]) < len(payload["messages"])
    assert router.estimate_prompt_tokens(trimmed) <= context_budget.input_limit(payload, 500)


def test_workbuddy_pretrims_above_its_own_budget(monkeypatch):
    # workbuddy 的预算与 loomy 各自独立，不再共用 loomy 的额度
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "500")
    monkeypatch.setenv("CB_PRETRIM_CHANNELS", "workbuddy=500")
    payload = _long_chat(10, 40)
    trimmed, changed = context_budget.prepare_dispatch("workbuddy", payload)
    assert changed is True
    assert trimmed["messages"][-1]["content"] == "最新问题"
    assert router.estimate_prompt_tokens(trimmed) <= context_budget.input_limit(payload, 500)


def test_workbuddy_default_budget_does_not_touch_short_chat(monkeypatch):
    monkeypatch.setenv("CB_PRETRIM_CHANNELS", "")
    payload = _long_chat(10, 40)
    trimmed, changed = context_budget.prepare_dispatch("workbuddy", payload)
    assert changed is False
    assert trimmed is payload


def test_global_budget_covers_any_channel(monkeypatch):
    # 全局兜底: 不用改代码、不用逐个列渠道，一条变量即可覆盖任意渠道
    monkeypatch.setenv("CB_PRETRIM_CHANNELS", "")
    monkeypatch.setenv("CB_PRETRIM_BUDGET", "500")
    payload = _long_chat(10, 40)
    trimmed, changed = context_budget.prepare_dispatch("qwenwork", payload)
    assert changed is True
    assert trimmed["messages"][-1]["content"] == "最新问题"
    assert router.estimate_prompt_tokens(trimmed) <= context_budget.input_limit(payload, 500)


def test_unknown_channel_is_never_pretrimmed(monkeypatch):
    monkeypatch.setenv("CB_PRETRIM_CHANNELS", "")
    monkeypatch.delenv("CB_PRETRIM_BUDGET", raising=False)
    payload = _long_chat(10, 40)
    trimmed, changed = context_budget.prepare_dispatch("traework", payload)
    assert changed is False
    assert trimmed is payload


def test_zero_budget_disables_pretrim(monkeypatch):
    monkeypatch.setenv("CB_LOOMY_CONTEXT_BUDGET", "0")
    payload = _long_chat(10, 40)
    trimmed, changed = context_budget.prepare_dispatch("loomy", payload)
    assert changed is False
    assert trimmed is payload


def test_context_rejection_codes_and_html():
    assert context_budget.is_context_rejection(400, {"code": 11115, "msg": "context"}, prompt_tokens=10)
    assert context_budget.is_context_rejection(
        400, {"code": 11133, "msg": "Invalid request parameters"}, prompt_tokens=150_000
    )
    assert not context_budget.is_context_rejection(
        400, {"code": 11133, "msg": "Invalid request parameters"}, prompt_tokens=20
    )
    html = {"error": {"message": "<html><body>502 Bad Gateway</body></html>"}}
    assert context_budget.is_context_rejection(502, html, prompt_tokens=192_000)
    assert not context_budget.is_context_rejection(502, html, prompt_tokens=100)


def test_rejection_trims_once_then_returns_readable_error():
    calls = []

    async def chat(payload, info):
        calls.append((len(payload["messages"]), info.get("_context_retried")))
        return (
            "error",
            (502, {"error": {"message": "<html>bad gateway</html>", "code": 11115}}),
        )

    payload = _long_chat(12, 80)
    result = asyncio.run(
        context_budget.dispatch_with_context_policy(
            type("P", (), {"chat_completions": staticmethod(chat)})(),
            "loomy",
            payload,
            {},
        )
    )
    assert calls[0][1] is None
    assert calls[1][1] is True
    assert calls[1][0] < calls[0][0]
    assert len(calls) == 2
    assert result[0] == "error"
    assert result[1][0] == 400
    assert result[1][1]["error"]["code"] == "context_length_exceeded"
    assert "<html>" not in result[1][1]["error"]["message"]


def test_small_html_error_is_rewritten_without_retry():
    calls = []

    async def chat(payload, info):
        calls.append(payload)
        return ("error", (502, {"error": {"message": "<html>gateway</html>"}}))

    result = asyncio.run(
        context_budget.dispatch_with_context_policy(
            type("P", (), {"chat_completions": staticmethod(chat)})(),
            "workbuddy",
            {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            {},
        )
    )
    assert len(calls) == 1
    assert result[1][0] == 502
    assert result[1][1]["error"]["code"] == "upstream_html"
    assert "<html>" not in result[1][1]["error"]["message"]


def test_settings_keeps_loomy_budget_outside_automation():
    text = Path("web/index.html").read_text(encoding="utf-8")
    automation = text.split('<div class="card-h">自动化<span', 1)[1].split('<div class="card-h">自动化日志', 1)[0]
    assert "loomy_context_budget" not in automation
    budget = text.split('<div class="card-h">上下文预算', 1)[1].split('<div class="card-h">后端参数', 1)[0]
    assert 'v-model.number="s.loomy_context_budget"' in budget
    assert "默认 160000 token" in budget
    assert "填 0 关闭发送前裁剪" in budget
    assert "CB_LOOMY_CONTEXT_BUDGET 可覆盖" in budget
