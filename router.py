"""Admit a model id, filter the account pool by it, walk the queue. No channel binding step."""

from __future__ import annotations

import copy
from contextlib import nullcontext
import json
import os
from typing import Optional

from fastapi import HTTPException

import auth_manager
import catalog
import database as db
import providers
import responses
import routing
import rate_limiter
from streaming import ClosingStream, close_stream
from reasoning_controls import normalize_chat_reasoning
from model_capacity import clamp_output_tokens
from providers.protocol import (
    BindResult,
    KNOWN_CHANNEL_SET,
    UnknownChannel,
)

ANY_CHANNEL = "*"


def _key_channel(api_key_info: dict | None) -> str:
    if not api_key_info:
        return "workbuddy"
    value = str(api_key_info.get("default_channel") or "workbuddy").strip()
    return value or "workbuddy"


def _key_allows_any(key_channel: str) -> bool:
    return key_channel == ANY_CHANNEL


def _normalize_channel_order(raw) -> list[str]:
    """Configured channel order, with any unlisted enabled channel appended."""
    known = providers.enabled_provider_ids()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, (list, tuple)):
        raw = []
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw:
        channel = str(item or "").strip()
        if channel in known and channel not in seen:
            seen.add(channel)
            ordered.append(channel)
    for channel in known:
        if channel not in seen:
            ordered.append(channel)
    return ordered


def _channel_order() -> list[str]:
    """Channel precedence used for every tie-break in routing."""
    try:
        raw = db.get_setting("channel_order", [])
    except Exception:
        raw = []
    return _normalize_channel_order(raw)


def routable_channel_ids() -> list[str]:
    """Enabled channels that can serve a request right now.

    The client model catalog and the router share this single judgment: a
    channel is advertised and tried exactly when ``routing.channel_pool`` has
    something for it. That gate covers the active account *and* a quota that is
    not provably used up, and it is deliberately fail-open — a channel whose
    quota cannot be read stays routable, because a failed billing call must not
    empty the pool.

    A channel whose accounts are all out of quota is still routable when it has
    at least one model marked 放行: that model skips the zero reading, so
    calling the channel "未参与路由" would be wrong.
    """
    found: list[str] = []
    for channel in _channel_order():
        try:
            pool = routing.channel_pool(channel)
            if not pool and catalog.exempt_models(channel):
                with routing.quota_exempt_scope({channel}):
                    pool = routing.channel_pool(channel)
            if pool:
                found.append(channel)
        except Exception:
            continue
    return found


def _quota_exempt_channels(base: str) -> set[str]:
    """Channels that serve `base` through a model marked 放行 in their catalog.

    The mark lives on the channel's own catalog id, so the request id has to be
    translated per channel first: "deepseek-v4.1-flash-1017" is
    "DeepSeek-Flash" on qoder and "deepseek-v4.1-flash" on workbuddy.
    """
    if not base:
        return set()
    found: set[str] = set()
    for channel in _channel_order():
        provider = providers.get_provider(channel)
        if provider is None or not _channel_accepts(channel, base):
            continue
        try:
            marks = set(catalog.exempt_models(channel))
        except Exception:
            marks = set()
        if not marks:
            continue
        names = {base}
        try:
            names.add(provider.translate_model(base))
        except Exception:
            pass
        if names & marks:
            found.add(channel)
    return found


def _channels_for_name(name: str) -> list[str]:
    """Every enabled channel whose catalog serves `name` exactly as written.

    The client model id is the whole filter. A namespaced id ("qoder/auto")
    belongs to that channel alone; a bare id stays in every channel that
    recognizes it. The old binder is gone because the prefix never had to be
    parsed into a routing decision — it *is* a catalog lookup the provider
    already performs.
    """
    matched: list[str] = []
    for channel in _channel_order():
        provider = providers.get_provider(channel)
        if provider is None:
            continue
        try:
            if provider.accepts_model(name):
                matched.append(channel)
        except Exception:
            continue
    return matched


def _namespaced(name: str) -> tuple[str | None, str]:
    """Split "channel/id" only when the leading part is a known channel id.

    Returns (channel | None, base). A name with no known prefix comes back as
    (None, name) and is then treated as a bare id.
    """
    first, sep, rest = (name or "").partition("/")
    if sep and first in KNOWN_CHANNEL_SET:
        return first, rest
    return None, name


def _channel_accepts(channel: str, name: str) -> bool:
    provider = providers.get_provider(channel)
    if provider is None:
        return False
    try:
        return bool(provider.accepts_model(name))
    except Exception:
        return False


def _pinned_channel(api_key_info: dict | None) -> str | None:
    """The one channel this key is pinned to, or None when it may use any.

    ``_visible_channels`` returns the same thing as a list; this form is for
    callers that need to report the channel's own state.
    """
    if not api_key_info:
        return None
    key_channel = _key_channel(api_key_info)
    return None if _key_allows_any(key_channel) else key_channel


def _visible_channels(api_key_info: dict | None) -> list[str]:
    """Channels whose models this key may see; no binding means all of them.

    A key bound to one channel only ever sees that channel's catalog. This is
    the whole of the old per-channel binding, reduced to one filter. A falsy
    ``api_key_info`` (unauthenticated deployments) carries no binding.
    """
    if not api_key_info:
        return _channel_order()
    key_channel = _key_channel(api_key_info)
    if _key_allows_any(key_channel):
        return _channel_order()
    return [key_channel]


def _bad_model(message: str, kind: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"message": message, "type": "invalid_request_error", "code": kind}},
    )


def _model_allowed(api_key_info: dict | None, channel: str, base: str) -> bool:
    """Evaluate permissions for this hop, not the union of all translations."""
    allowed = (api_key_info or {}).get("allowed_models")
    if not allowed:
        return True
    provider = providers.get_provider(channel)
    if provider is None:
        return False
    translated = provider.translate_model(base)
    variants = {base, translated, f"{channel}/{base}", f"{channel}/{translated}"}
    return not set(allowed).isdisjoint(variants)


def admit_model(api_key_info: dict | None, name) -> str:
    """One admission check, run before any account is touched.

    The request either names a model this key can use — and we hand back the
    bare id to filter the pool with — or it is rejected here with a 400.
    Nothing downstream ever asks again: passing this gate is the proof that
    some catalog owns the name and the key is allowed it.

    Deliberately name-only. It does not care whether an account is alive,
    cooling down, or out of quota; that is the queue's business, and a model
    stays admitted even at 3 a.m. when every pool is empty.

    Rejects with 400 when:
      * the name is not a string, or a namespaced id with nothing after it;
      * a namespaced id names a retired or disabled channel;
      * no channel this key can see has the name in its catalog;
      * the key carries an allowed_models whitelist and none of the name's
        legal spellings is in it.
    """
    if name is None or name == "":
        name = "auto"
    if not isinstance(name, str):
        raise _bad_model("model must be a string", "invalid_model")
    name = name.strip() or "auto"

    prefix, base = _namespaced(name)
    visible = _visible_channels(api_key_info)

    if prefix is not None:
        # A retired or disabled channel is a name the operator deleted, not a
        # name this key happens to lack: say which, even for a key bound
        # elsewhere. The old binder checked the channel's own state first and
        # that ordering is part of the error contract.
        if providers.is_retired_channel(prefix):
            raise _bad_model(providers.retired_message(prefix), "channel_retired")
        if not providers.is_channel_enabled(prefix) or providers.get_provider(prefix) is None:
            raise _bad_model(f"Unknown or disabled channel '{prefix}'", "unknown_channel")
        if prefix not in visible:
            raise _bad_model(
                f"Model '{name}' is not available for this API key",
                "model_not_allowed",
            )
        if not base:
            raise _bad_model(f"model '{name}' is missing the inner id", "invalid_model")
        if not _channel_accepts(prefix, base):
            raise _bad_model(f"Unknown model '{name}'", "unknown_model")
        candidates = [prefix]
    else:
        # A key pinned to a channel that has since retired (or been switched
        # off) is a state the operator can act on, so name it instead of
        # pretending the model does not exist. Keys are created against an
        # enabled channel, so this is a legacy row — the old binder checked it
        # here too.
        pinned = _pinned_channel(api_key_info)
        if pinned is not None:
            if providers.is_retired_channel(pinned):
                raise _bad_model(providers.retired_message(pinned), "channel_retired")
            if not providers.is_channel_enabled(pinned) or providers.get_provider(pinned) is None:
                raise _bad_model(f"Unknown or disabled channel '{pinned}'", "unknown_channel")
        candidates = [c for c in _channels_for_name(name) if c in visible]

    if not candidates:
        raise _bad_model(f"Unknown model '{name}'", "unknown_model")

    allowed = (api_key_info or {}).get("allowed_models")
    if allowed:
        if not any(_model_allowed(api_key_info, channel, base) for channel in candidates):
            raise _bad_model(
                f"Model '{name}' not allowed for this API key",
                "model_not_allowed",
            )
    return base


def _estimate_text_tokens(text: str) -> int:
    """Count CJK and fullwidth characters as one token; other text as about four chars."""
    cjk = 0
    other = 0
    for char in text:
        code = ord(char)
        if (
            0x3400 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x3000 <= code <= 0x303F
            or 0xFF00 <= code <= 0xFFEF
        ):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def _estimate_value(value) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return _estimate_text_tokens(value)
    if isinstance(value, list):
        return sum(_estimate_value(item) for item in value)
    if isinstance(value, dict):
        return sum(_estimate_value(key) + _estimate_value(item) for key, item in value.items())
    return _estimate_text_tokens(str(value))


def estimate_prompt_tokens(payload: dict) -> int:
    """Rough prompt size used only for the Codex pre-flight gate."""
    return max(1, _estimate_value(payload))


CODEX_PROMPT_TOKEN_CAP = 500000


def codex_prompt_limit(channel: str, model: str) -> tuple[int, int]:
    """Return the Codex-only input budget and the window used to derive it."""
    from model_capacity import capacity_fields
    import catalog

    row = next(
        (item for item in catalog.current_models(channel) if item.get("id") == model),
        {},
    )
    confirmed = capacity_fields(row).get("context_window")
    window = min(confirmed, CODEX_PROMPT_TOKEN_CAP) if confirmed else CODEX_PROMPT_TOKEN_CAP
    return max(1, window - 32768), window


def _is_system_item(item) -> bool:
    return isinstance(item, dict) and item.get("role") in ("system", "developer")


def _is_tool_carrier(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("type") in ("function_call", "function_call_output"):
        return True
    if item.get("role") == "tool":
        return True
    return item.get("role") == "assistant" and bool(item.get("tool_calls"))


def _history_segments(items: list) -> list[list]:
    """Keep a tool call and its results in one segment so trimming cannot split them."""
    segments = []
    index = 0
    while index < len(items):
        item = items[index]
        if _is_tool_carrier(item) or (isinstance(item, dict) and item.get("type") == "reasoning"):
            group = []
            while index < len(items) and (
                _is_tool_carrier(items[index])
                or (isinstance(items[index], dict) and items[index].get("type") == "reasoning")
            ):
                group.append(items[index])
                index += 1
            segments.append(group)
            continue
        segments.append([item])
        index += 1
    return segments


def _trim_history(payload: dict, key: str, limit: int) -> list:
    """Drop oldest history until the whole payload fits, keeping the newest segment."""
    segments = _history_segments(payload[key])
    while estimate_prompt_tokens(payload) > limit and len(segments) > 1:
        drop_at = next(
            (
                index
                for index, segment in enumerate(segments[:-1])
                if not all(_is_system_item(item) for item in segment)
            ),
            None,
        )
        if drop_at is None:
            drop_at = 0
        segments.pop(drop_at)
        payload[key] = [item for segment in segments for item in segment]
    return payload[key]


def trim_codex_prompt(payload: dict, channel: str, model: str) -> dict:
    """Trim old Codex history so the current turn can continue."""
    limit, window = codex_prompt_limit(channel, model)
    if estimate_prompt_tokens(payload) <= limit:
        return payload
    trimmed = copy.deepcopy(payload)
    if isinstance(trimmed.get("input"), list):
        _trim_history(trimmed, "input", limit)
    elif isinstance(trimmed.get("messages"), list):
        _trim_history(trimmed, "messages", limit)
    if estimate_prompt_tokens(trimmed) <= limit:
        return trimmed
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": (
                    f"Codex prompt is too long for {model}: the newest request still exceeds "
                    f"the {limit}-token Codex budget ({window}-token window) after trimming old history."
                ),
                "type": "invalid_request_error",
                "code": "prompt_too_long",
                "param": "input",
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


def _rewrite_stream_model(stream, original: str):
    return ClosingStream(_rewrite_stream_model_chunks(stream, original), upstream=stream)


async def _rewrite_stream_model_chunks(stream, original: str):
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


# When enabled, a request that fails on its bound channel (or on every account
# inside it) keeps trying the other channels that can serve the same model,
# ordered by that channel's best available account priority. This makes the
# whole account pool — not just the accounts inside one channel — participate
# in failover, which is what the priority/weight fields are for.


def _cross_channel_failover_enabled() -> bool:
    return os.environ.get(
        "BUDDY_CROSS_CHANNEL_FAILOVER", "1"
    ).strip().lower() not in ("0", "false", "no", "off")


async def _stream_first_is_error(stream) -> tuple[bool, list, object]:
    """Peek the first chunk.

    Returns (is_error, [first_chunk], remaining_iterator). A channel that
    exhausted all its accounts emits a `data: {"error": {...}}` event first;
    a channel that started producing content never counts as an error.
    """
    ait = stream.__aiter__()
    try:
        first = await ait.__anext__()
    except StopAsyncIteration:
        return (True, [], ait)
    text = first.decode("utf-8", errors="replace") if isinstance(first, (bytes, bytearray)) else str(first)
    is_error = False
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            is_error = True
            break
    return (is_error, [first], ait)


def _replay(first: list, rest, account: dict | None = None):
    """Pin upstream operations without carrying a ContextVar token over yield."""
    return ClosingStream(
        _replay_chunks(first, rest, account),
        on_close=lambda: _close_stream(rest, account),
    )


async def _replay_chunks(first, rest, account):
    for chunk in first:
        yield chunk
    iterator = rest.__aiter__()
    while True:
        with auth_manager.pinned_account(account) if account is not None else nullcontext():
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                return
        yield chunk


def account_queue(name: str | None = None, api_key_info: dict | None = None
                  ) -> list[tuple[str, dict]]:
    """The routing queue: every account this request may spend, in order.

    Exactly the operator's four steps, and nowhere else:

      1. drop the accounts that cannot serve at all — disabled channel, account
         that is not ``active``, account cooling down after a failure, quota
         provably used up (``routing.channel_pool`` applies that gate; a model
         marked 放行 on that channel is exempt from the quota half of it);
      2. drop the accounts whose catalog does not serve this model id: a
         namespaced id matches its own channel only, a bare id every channel
         that recognizes it, and a key bound to one channel never leaves it;
      3. order what is left: priority DESC -> channel order -> weight DESC,
         sticky account, historical request count, then id (expiry
         no longer orders anything — it is display-only, see
         ``routing.urgency_days``);
      4. hand the list to the dispatcher, which walks it one account at a time.

    ``name=None`` keeps the whole pool (no model filter); the admin page ranks
    exactly that list, so what it shows is literally the order a request would
    walk. ``api_key_info`` narrows it to the channels the key may see.

    The namespaced/bare split is re-derived here from the same one rule
    ``admit_model`` used; the queue never asks the caller to pre-slice the name.
    """
    order = _channel_order()
    rank = {channel: index for index, channel in enumerate(order)}
    prefix, base = _namespaced(name) if name else (None, "")
    exempt = _quota_exempt_channels(base) if name else set()
    key_channel = _key_channel(api_key_info) if api_key_info else None
    key_locked = (
        None
        if key_channel is None or _key_allows_any(key_channel)
        else key_channel
    )
    rows: list[tuple[str, dict]] = []
    with routing.quota_exempt_scope(exempt):
        for channel in order:
            if prefix is not None and channel != prefix:
                continue
            if key_locked is not None and channel != key_locked:
                continue
            if name and not _channel_accepts(channel, base):
                continue
            if name and not _model_allowed(api_key_info, channel, base):
                continue
            if providers.get_provider(channel) is None:
                continue
            for account in routing.channel_pool(channel):
                aid = int(account.get("id") or 0)
                if not aid or auth_manager.account_is_cooling_down(aid):
                    continue
                rows.append((channel, account))
    if not rows:
        return rows

    preferred = {channel: auth_manager.preferred_account_id(channel) for channel in order}

    def queue_key(row: tuple[str, dict]) -> tuple:
        channel, account = row
        account_key = routing.sort_key(account, channel)
        return (
            account_key[0],
            rank.get(channel, len(order)),
            account_key[1],
            0 if preferred.get(channel) == int(account.get("id") or 0) else 1,
            *account_key[2:],
        )

    rows.sort(key=queue_key)
    return rows


def _hop_label(hop: tuple[str, dict] | None) -> str:
    if not hop:
        return "—"
    channel, account = hop
    name = account.get("name") or account.get("nickname") or account.get("id")
    return f"{channel}/{name}"


def _no_account_error() -> tuple:
    return ("error", (503, {
        "error": {"message": "No enabled account can serve this model right now",
                  "type": "channel_unavailable", "code": "channel_unavailable"}}))


def _clean_name(name) -> str:
    name = ("auto" if name is None or name == "" else str(name)).strip()
    return name or "auto"


async def _request_queue(name: str, api_key_info: dict | None) -> list[tuple[str, dict]]:
    queue = account_queue(name, api_key_info)
    if queue:
        return queue
    # Preserve the old recovery path: when no active account can serve, give
    # expired credentials on permitted channels a chance to refresh first.
    prefix, base = _namespaced(name)
    for channel in _visible_channels(api_key_info):
        if prefix is not None and prefix != channel:
            continue
        if not _channel_accepts(channel, base) or not _model_allowed(api_key_info, channel, base):
            continue
        try:
            expired = any(a.get("status") == "expired" for a in db.list_accounts(provider=channel))
            provider = providers.get_provider(channel)
            if expired and provider is not None:
                await provider.pick_account_with_fallback()
        except Exception:
            continue
    return account_queue(name, api_key_info)


async def chat_after_name(
    name: str, payload: dict, api_key_info: dict | None, after_queue=None
) -> tuple:
    """Walk the queue for `name`, one account at a time, and return a result.

    The caller must have run ``admit_model`` first — that gate is the only
    place the name is judged. The queue is built here, once, and the walk
    consumes exactly that list: no second sort anywhere in the request path.

    ``after_queue`` is awaited once with the non-empty queue before the first
    account is spent; the server passes the key's daily quota reservation
    there, so a request that found no account at all is never counted.

    With cross-channel failover off the first hop's channel owns the request
    and the provider walks its own pool under ``account_attempt_limit``.
    With it on every hop pins exactly one account from the queue — that is
    the only walk that honors the global order.
    """
    name = _clean_name(name)
    _, base = _namespaced(name)
    # A provider picks its account lazily, inside the stream it returns, so the
    # 放行 exemption has to outlive this function rather than sit in a ``with``.
    routing.note_quota_exempt(_quota_exempt_channels(base))
    queue = await _request_queue(name, api_key_info)
    if not queue:
        return _no_account_error()
    if after_queue is not None:
        await after_queue(queue)

    return await _walk_account_queue(name, payload, api_key_info, queue)


async def _close_stream(stream, account: dict | None = None) -> None:
    with auth_manager.pinned_account(account) if account is not None else nullcontext():
        await close_stream(stream)


async def _walk_account_queue(name: str, payload: dict, api_key_info: dict | None,
                              queue: list[tuple[str, dict]]) -> tuple:
    """Shared Chat/Responses failover; admission and daily reservation run once."""
    _, base = _namespaced(name)
    if not _cross_channel_failover_enabled():
        bound = BindResult(channel=queue[0][0], inner=base, original=name)
        result = await _chat_after_bind_no_echo(bound, payload, api_key_info)
        return await echo_original(result, name)

    last_error = None
    for channel, account in queue:
        provider = providers.get_provider(channel)
        if provider is None:
            continue
        # One hop, one account, no pseudo-log: the walk is visible as the real
        # per-account rows the providers already write.
        hop = _hop_label((channel, account))
        sub = BindResult(channel=channel, inner=base, original=name)
        stream = None
        try:
            with auth_manager.pinned_account(account):
                result = await _chat_after_bind_no_echo(sub, payload, api_key_info)
                if result[0] == "stream":
                    stream = result[1]
                    is_error, first, rest = await _stream_first_is_error(stream)
        except Exception as exc:  # a broken hop must not end the walk
            if stream is not None:
                await _close_stream(stream, account)
            result = ("error", (502, {
                "error": {"message": str(exc)[:200] or "dispatch failed",
                          "type": "upstream_error", "code": "upstream_error",
                          "channel": channel}}))
        except BaseException:
            if stream is not None:
                await _close_stream(stream, account)
            raise
        kind = result[0]
        if kind == "json":
            auth_manager.note_dispatched_account(account)
            return await echo_original(result, name)
        if kind == "error":
            last_error = result
            continue
        if kind == "stream":
            if is_error:
                last_error = ("error", (502, {
                    "error": {"message": f"account '{hop}' failed",
                              "type": "channel_unavailable", "code": "channel_unavailable",
                              "channel": channel}}))
                # A local queue timeout is a 429, not an upstream outage. Keep
                # its actionable detail if every candidate is busy.
                for chunk in first:
                    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                    for line in text.splitlines():
                        if line.startswith("data:"):
                            try:
                                detail = json.loads(line[5:])
                            except (ValueError, TypeError):
                                continue
                            if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
                                if detail["error"].get("code") == "local_rate_limit":
                                    last_error = ("error", (429, detail))
                await _close_stream(rest, account)
                continue
            auth_manager.note_dispatched_account(account)
            return ("stream", _rewrite_stream_model(_replay(first, rest, account), name))
    if last_error is not None:
        return last_error
    return _no_account_error()


async def _chat_after_bind_no_echo(
    bound: BindResult, payload: dict, api_key_info: dict | None
) -> tuple:
    provider = providers.get_provider(bound.channel)
    if provider is None:
        raise UnknownChannel(bound.channel)
    inner = provider.translate_model(bound.inner)
    dispatch = normalize_chat_reasoning(dispatch_payload(payload, inner))
    model = next((item for item in provider.list_models()
                  if isinstance(item, dict) and item.get("id") == inner), None)
    dispatch = clamp_output_tokens(dispatch, model)
    info = dict(api_key_info or {})
    info["_log_model"] = bound.original
    info["_resolved_model"] = inner
    info["_bind_channel"] = bound.channel
    from context_budget import dispatch_with_context_policy

    try:
        result = await dispatch_with_context_policy(provider, bound.channel, dispatch, info)
    except rate_limiter.RateLimitError as exc:
        return ("error", (429, exc.payload()))
    if result[0] == "stream":
        return ("stream", rate_limiter.guard_stream(result[1]))
    return result


async def responses_after_name(
    name: str, payload: dict, api_key_info: dict | None, after_queue=None
) -> tuple:
    """Bridge Responses through the same per-account queue as Chat."""
    name = _clean_name(name)
    _, base = _namespaced(name)
    routing.note_quota_exempt(_quota_exempt_channels(base))
    queue = await _request_queue(name, api_key_info)
    if not queue:
        return _no_account_error()
    if after_queue is not None:
        await after_queue(queue)
    channel = queue[0][0]
    if api_key_info and api_key_info.get("client_type") == "codex":
        provider = providers.get_provider(channel)
        resolved = provider.translate_model(base) if provider else base
        payload = trim_codex_prompt(payload, channel, resolved)
    dispatch = dispatch_payload(payload, base)

    async def chat_handler(chat_payload: dict, _api_key_info: dict | None) -> tuple:
        return await _walk_account_queue(name, chat_payload, api_key_info, queue)

    result = await responses.proxy_responses(
        dispatch,
        api_key_info,
        chat_handler=chat_handler,
    )
    return await echo_original(result, name)
