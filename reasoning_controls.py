"""Normalize reasoning controls used by OpenAI-compatible agent clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


_MISSING = object()
_DISABLED_VALUES = {"none", "off", "disable", "disabled", "false"}
_ENABLED_VALUES = {"auto", "enable", "enabled", "adaptive", "on", "true"}
_EFFORT_ALIASES = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra_high": "xhigh",
    "extra-high": "xhigh",
    "max": "max",
    "ultra": "ultra",
}


class InvalidReasoningControl(ValueError):
    """A recognized reasoning control has an invalid or conflicting value."""


@dataclass(frozen=True)
class ReasoningControl:
    """Canonical four-state reasoning request."""

    mode: str
    effort: Optional[str] = None
    source: Optional[str] = None
    budget_tokens: Optional[float] = None

    @property
    def enabled(self) -> Optional[bool]:
        if self.mode == "disabled":
            return False
        if self.mode in {"enabled", "effort"}:
            return True
        return None


DEFAULT_REASONING = ReasoningControl("default")


def _invalid(source: str, message: str) -> InvalidReasoningControl:
    return InvalidReasoningControl(f"{source} {message}")


def _from_effort(value, source: str) -> Optional[ReasoningControl]:
    if value is None:
        return None
    if isinstance(value, bool):
        return ReasoningControl("enabled" if value else "disabled", source=source)
    if not isinstance(value, str):
        raise _invalid(source, "must be a string")
    normalized = value.strip().lower()
    if not normalized:
        raise _invalid(source, "must not be empty")
    if normalized == "default":
        return ReasoningControl("default", source=source)
    if normalized in _DISABLED_VALUES:
        return ReasoningControl("disabled", source=source)
    if normalized in _ENABLED_VALUES:
        return ReasoningControl("enabled", source=source)
    effort = _EFFORT_ALIASES.get(normalized)
    if effort is None:
        allowed = "none, minimal, low, medium, high, xhigh, max, or ultra"
        raise _invalid(source, f"must be one of {allowed}")
    return ReasoningControl("effort", effort=effort, source=source)


def _from_switch(value, source: str) -> Optional[ReasoningControl]:
    if value is None:
        return None
    if isinstance(value, bool):
        return ReasoningControl("enabled" if value else "disabled", source=source)
    if isinstance(value, int) and value in {0, 1}:
        return ReasoningControl("enabled" if value else "disabled", source=source)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _DISABLED_VALUES:
            return ReasoningControl("disabled", source=source)
        if normalized in _ENABLED_VALUES:
            return ReasoningControl("enabled", source=source)
    raise _invalid(source, "must be a boolean or an enabled/disabled value")


def _from_disable_switch(value, source: str) -> Optional[ReasoningControl]:
    if value is None or value is False or value == 0:
        return None
    if value is True or value == 1:
        return ReasoningControl("disabled", source=source)
    raise _invalid(source, "must be a boolean")


def _from_budget(value, source: str) -> Optional[ReasoningControl]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise _invalid(source, "must be a non-negative number")
    return ReasoningControl(
        "disabled" if value == 0 else "enabled",
        source=source,
        budget_tokens=value,
    )


def _append_nested_effort(
    candidates: list[ReasoningControl],
    container,
    container_name: str,
) -> None:
    if not isinstance(container, dict) or "effort" not in container:
        return
    control = _from_effort(container.get("effort"), f"{container_name}.effort")
    if control is not None:
        candidates.append(control)


def _native_control_group(source: Optional[str]) -> Optional[str]:
    if source == "reasoning" or (source and source.startswith("reasoning.")):
        return "reasoning"
    if source == "thinking" or (source and source.startswith("thinking.")):
        return "thinking"
    return source


def _validate_native_object_conflicts(
    effort_candidates: list[ReasoningControl],
    switch_candidates: list[ReasoningControl],
) -> None:
    for group in ("reasoning", "thinking"):
        controls = [
            control
            for control in [*effort_candidates, *switch_candidates]
            if _native_control_group(control.source) == group
            and control.enabled is not None
        ]
        if len({control.enabled for control in controls}) > 1:
            sources = ", ".join(
                control.source or "reasoning control" for control in controls
            )
            raise InvalidReasoningControl(f"conflicting reasoning controls: {sources}")


def resolve_reasoning_control(
    payload: dict,
    *,
    prefer_nested: bool = False,
) -> ReasoningControl:
    """Resolve common Chat, Responses, DeepSeek, and Claude-style controls."""
    if not isinstance(payload, dict):
        return DEFAULT_REASONING

    reasoning = payload.get("reasoning", _MISSING)
    thinking = payload.get("thinking", _MISSING)
    output_config = payload.get("output_config", _MISSING)
    effort_candidates: list[ReasoningControl] = []

    def add_top_level(key: str) -> None:
        if key not in payload:
            return
        control = _from_effort(payload.get(key), key)
        if control is not None:
            effort_candidates.append(control)

    if prefer_nested:
        _append_nested_effort(effort_candidates, reasoning, "reasoning")
        add_top_level("reasoning_effort")
    else:
        add_top_level("reasoning_effort")
        _append_nested_effort(effort_candidates, reasoning, "reasoning")
    add_top_level("reasoningEffort")
    _append_nested_effort(effort_candidates, output_config, "output_config")
    _append_nested_effort(effort_candidates, thinking, "thinking")

    if reasoning is not _MISSING and not isinstance(reasoning, dict):
        control = _from_effort(reasoning, "reasoning")
        if control is not None:
            effort_candidates.append(control)
    if thinking is not _MISSING and not isinstance(thinking, dict):
        control = _from_effort(thinking, "thinking")
        if control is not None:
            effort_candidates.append(control)

    switch_candidates: list[ReasoningControl] = []
    if isinstance(thinking, dict):
        if "type" in thinking:
            kind = thinking.get("type")
            if not isinstance(kind, str):
                raise _invalid("thinking.type", "must be a string")
            normalized = kind.strip().lower()
            if normalized == "disabled":
                switch_candidates.append(ReasoningControl("disabled", source="thinking.type"))
            elif normalized in {"enabled", "adaptive"}:
                switch_candidates.append(ReasoningControl("enabled", source="thinking.type"))
            else:
                raise _invalid("thinking.type", "must be enabled, adaptive, or disabled")
        if "budget_tokens" in thinking:
            control = _from_budget(thinking.get("budget_tokens"), "thinking.budget_tokens")
            if control is not None:
                switch_candidates.append(control)

    if isinstance(reasoning, dict):
        if "enabled" in reasoning:
            control = _from_switch(reasoning.get("enabled"), "reasoning.enabled")
            if control is not None:
                switch_candidates.append(control)
        if "budget_tokens" in reasoning:
            control = _from_budget(reasoning.get("budget_tokens"), "reasoning.budget_tokens")
            if control is not None:
                switch_candidates.append(control)

    for key in ("enable_thinking", "think"):
        if key in payload:
            control = _from_switch(payload.get(key), key)
            if control is not None:
                switch_candidates.append(control)
    if "disable_reasoning" in payload:
        control = _from_disable_switch(payload.get("disable_reasoning"), "disable_reasoning")
        if control is not None:
            switch_candidates.append(control)

    _validate_native_object_conflicts(effort_candidates, switch_candidates)

    selected_effort = next(
        (control for control in effort_candidates if control.mode != "default"),
        None,
    )
    if selected_effort is not None:
        group = _native_control_group(selected_effort.source)
        if group in {"reasoning", "thinking"}:
            related_switches = [
                control
                for control in switch_candidates
                if _native_control_group(control.source) == group
            ]
        else:
            related_switches = []
        budget = next(
            (
                control.budget_tokens
                for control in related_switches
                if control.budget_tokens is not None
            ),
            None,
        )
        if budget is not None:
            return ReasoningControl(
                selected_effort.mode,
                effort=selected_effort.effort,
                source=selected_effort.source,
                budget_tokens=budget,
        )
        return selected_effort

    if switch_candidates:
        selected = switch_candidates[0]
        group = _native_control_group(selected.source)
        budget = next(
            (
                control.budget_tokens
                for control in switch_candidates
                if _native_control_group(control.source) == group
                and control.budget_tokens is not None
            ),
            None,
        )
        if budget is not None and selected.budget_tokens is None:
            return ReasoningControl(
                selected.mode,
                effort=selected.effort,
                source=selected.source,
                budget_tokens=budget,
            )
        return selected
    return DEFAULT_REASONING


def chat_reasoning_effort(control: ReasoningControl) -> Optional[str]:
    """Project a canonical control to the common Chat reasoning_effort field."""
    if control.mode == "default":
        return None
    if control.mode == "disabled":
        return "none"
    if control.mode == "enabled":
        return "high"
    return control.effort


def workbuddy_reasoning_effort(control: ReasoningControl) -> Optional[str]:
    """Project standard effort levels to WorkBuddy's low/high/max dialect."""
    if control.mode in {"default", "disabled"}:
        return None
    if control.mode == "enabled":
        return "high"
    return {
        "minimal": "low",
        "low": "low",
        "medium": "high",
        "high": "high",
        "xhigh": "max",
        "max": "max",
        "ultra": "max",
    }.get(control.effort, control.effort)


def normalize_chat_reasoning(payload: dict, *, prefer_nested: bool = False) -> dict:
    """Return a copy with compatibility controls converted to reasoning_effort."""
    control = resolve_reasoning_control(payload, prefer_nested=prefer_nested)
    body = dict(payload)

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        remaining = dict(reasoning)
        if reasoning.get("summary") is not None and "reasoning_summary" not in body:
            body["reasoning_summary"] = reasoning["summary"]
        for key in ("effort", "enabled", "summary"):
            remaining.pop(key, None)
        if remaining:
            body["reasoning"] = remaining
        else:
            body.pop("reasoning", None)
    elif "reasoning" in body:
        body.pop("reasoning", None)

    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        remaining = dict(thinking)
        for key in ("type", "effort"):
            remaining.pop(key, None)
        if remaining:
            body["thinking"] = remaining
        else:
            body.pop("thinking", None)
    elif "thinking" in body:
        body.pop("thinking", None)

    output_config = body.get("output_config")
    if isinstance(output_config, dict) and "effort" in output_config:
        remaining = {key: value for key, value in output_config.items() if key != "effort"}
        if remaining:
            body["output_config"] = remaining
        else:
            body.pop("output_config", None)

    for key in (
        "reasoningEffort",
        "enable_thinking",
        "disable_reasoning",
        "think",
    ):
        body.pop(key, None)

    effort = chat_reasoning_effort(control)
    if effort is None:
        body.pop("reasoning_effort", None)
    else:
        body["reasoning_effort"] = effort
    return body
