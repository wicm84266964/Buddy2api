"""Bind /v1 requests to a single channel. Does not construct upstream HTTP clients."""

from __future__ import annotations

import copy
import json
from typing import Optional

from fastapi import HTTPException

import providers
import responses
from reasoning_controls import normalize_chat_reasoning
from providers.protocol import (
    BindResult,
    InvalidModel,
    KeyChannelMismatch,
    KNOWN_CHANNEL_SET,
    UnknownChannel,
    UnknownModel,
)


def _key_channel(api_key_info: dict | None) -> str:
    if not api_key_info:
        return "workbuddy"
    value = str(api_key_info.get("default_channel") or "workbuddy").strip()
    return value or "workbuddy"


def _other_channel_ids(inner: str, except_channel: str) -> bool:
    for channel in providers.enabled_provider_ids():
        if channel == except_channel:
            continue
        provider = providers.get_provider(channel)
        if provider and provider.accepts_model(inner):
            return True
    return False


def bind(payload: dict, api_key_info: dict | None) -> BindResult:
    original = payload.get("model", "auto")
    if original is None or original == "":
        original = "auto"
    if not isinstance(original, str):
        raise InvalidModel(str(original), "model must be a string")
    original = original.strip() or "auto"
    key_channel = _key_channel(api_key_info)

    first, sep, rest = original.partition("/")
    if sep and first in KNOWN_CHANNEL_SET:
        channel = first
        inner = rest
        if not inner:
            raise InvalidModel(original, f"model '{original}' is missing the inner id")
        if not providers.is_channel_enabled(channel) or providers.get_provider(channel) is None:
            raise UnknownChannel(channel)
        provider = providers.get_provider(channel)
        if not provider.accepts_model(inner):
            raise InvalidModel(original)
        if key_channel != channel:
            raise KeyChannelMismatch(channel, key_channel)
        return BindResult(channel=channel, inner=inner, original=original)

    # Unprefixed: skip step 2, bind key.default_channel
    channel = key_channel
    if not providers.is_channel_enabled(channel) or providers.get_provider(channel) is None:
        raise UnknownChannel(channel)
    provider = providers.get_provider(channel)
    inner = original
    if inner == "auto" or provider.accepts_model(inner):
        return BindResult(channel=channel, inner=inner, original=original)
    if _other_channel_ids(inner, channel):
        raise UnknownModel(
            original,
            f"Model '{original}' belongs to another channel; "
            f"switch this API key's channel or send a namespaced id",
        )
    raise UnknownModel(original)


def bind_http(payload: dict, api_key_info: dict | None) -> BindResult:
    try:
        return bind(payload, api_key_info)
    except UnknownChannel as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": str(exc),
                    "type": "unknown_channel",
                    "code": "unknown_channel",
                    "channel": exc.channel,
                }
            },
        ) from exc
    except KeyChannelMismatch as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": str(exc),
                    "type": "key_channel_mismatch",
                    "code": "key_channel_mismatch",
                    "channel": exc.channel,
                    "key_channel": exc.key_channel,
                }
            },
        ) from exc
    except InvalidModel as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": str(exc),
                    "type": "invalid_model",
                    "code": "invalid_model",
                }
            },
        ) from exc
    except UnknownModel as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": str(exc),
                    "type": "unknown_model",
                    "code": "unknown_model",
                }
            },
        ) from exc


async def ensure_usable(channel: str) -> None:
    provider = providers.get_provider(channel)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "message": f"Unknown or disabled channel '{channel}'",
                    "type": "unknown_channel",
                    "code": "unknown_channel",
                    "channel": channel,
                }
            },
        )
    if await provider.has_usable_account():
        return
    raise HTTPException(
        status_code=503,
        detail={
            "error": {
                "message": f"No usable accounts for channel '{channel}'",
                "type": "channel_unavailable",
                "code": "channel_unavailable",
                "channel": channel,
            }
        },
    )


def dispatch_payload(payload: dict, inner: str) -> dict:
    body = copy.copy(payload)
    body["model"] = inner
    return body


def _rewrite_json_model(obj, original: str):
    if isinstance(obj, dict):
        rewritten = None
        if "model" in obj and isinstance(obj["model"], str):
            rewritten = dict(obj)
            rewritten["model"] = original
        if isinstance(obj.get("response"), dict):
            if rewritten is None:
                rewritten = dict(obj)
            rewritten["response"] = _rewrite_json_model(obj["response"], original)
        if rewritten is not None:
            obj = rewritten
        return obj
    return obj


async def _rewrite_stream_model(stream, original: str):
    async for chunk in stream:
        if not isinstance(chunk, (bytes, bytearray, str)):
            yield chunk
            continue
        text = chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else chunk
        rewritten = []
        for line in text.splitlines(keepends=True):
            raw = line[:-1] if line.endswith("\n") else line
            ended = line.endswith("\n")
            if raw.startswith("data:") and raw[5:].strip() not in {"", "[DONE]"}:
                data = raw[5:]
                if data.startswith(" "):
                    data = data[1:]
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    rewritten.append(line)
                    continue
                parsed = _rewrite_json_model(parsed, original)
                new_line = "data: " + json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
                rewritten.append(new_line + ("\n" if ended else ""))
            else:
                rewritten.append(line)
        out = "".join(rewritten)
        yield out.encode("utf-8") if isinstance(chunk, (bytes, bytearray)) else out


async def echo_original(result: tuple, original: str) -> tuple:
    kind = result[0]
    if kind == "json" and isinstance(result[1], dict):
        body = dict(result[1])
        if "model" in body:
            body["model"] = original
        return ("json", body)
    if kind == "stream":
        return ("stream", _rewrite_stream_model(result[1], original))
    return result


async def chat_after_bind(
    bound: BindResult, payload: dict, api_key_info: dict | None
) -> tuple:
    result = await _chat_after_bind_no_echo(bound, payload, api_key_info)
    return await echo_original(result, bound.original)


async def _chat_after_bind_no_echo(
    bound: BindResult, payload: dict, api_key_info: dict | None
) -> tuple:
    provider = providers.get_provider(bound.channel)
    if provider is None:
        raise UnknownChannel(bound.channel)
    inner = provider.translate_model(bound.inner)
    dispatch = normalize_chat_reasoning(dispatch_payload(payload, inner))
    info = dict(api_key_info or {})
    info["_log_model"] = bound.original
    info["_bind_channel"] = bound.channel
    return await provider.chat_completions(dispatch, info)


async def responses_after_bind(
    bound: BindResult, payload: dict, api_key_info: dict | None
) -> tuple:
    """Bridge Responses through the provider selected by the existing bind."""
    dispatch = dispatch_payload(payload, bound.inner)

    async def chat_handler(chat_payload: dict, _api_key_info: dict | None) -> tuple:
        return await _chat_after_bind_no_echo(bound, chat_payload, api_key_info)

    result = await responses.proxy_responses(
        dispatch,
        api_key_info,
        chat_handler=chat_handler,
    )
    return await echo_original(result, bound.original)
