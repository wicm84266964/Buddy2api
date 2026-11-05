"""
server.py — Buddy 2 API 主服务

FastAPI 应用，包含：
  - /v1/chat/completions  代理端点（OpenAI 兼容）
  - /v1/models            模型列表
  - /health               健康检查
  - /admin/*              管理 API
  - /                     Web UI
"""

import argparse
import asyncio
import contextvars
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import threading
import webbrowser
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

import database as db
import auth_manager
import catalog
import proxy
import responses
import providers
import router
import routing
import control_plane
import resource_sync
from providers.protocol import KNOWN_CHANNEL_IDS, KNOWN_CHANNEL_SET
from providers.qclaw.store import default_guid, upsert_account as upsert_qclaw_account
from reasoning_controls import (
    InvalidReasoningControl,
    normalize_chat_reasoning,
    resolve_reasoning_control,
)
from version import VERSION
import self_update


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cors_origins() -> list[str]:
    value = os.environ.get(
        "CB_GATEWAY_CORS_ORIGINS",
        "http://127.0.0.1:8787,http://localhost:8787",
    )
    return [origin.strip() for origin in value.split(",") if origin.strip()]


@asynccontextmanager
async def _lifespan(_app):
    import context_budget

    context_budget.load_saved_channel_budgets()
    control_plane.start_keepalive()
    try:
        yield
    finally:
        await control_plane.stop_keepalive()


app = FastAPI(title="Buddy 2 API", version=VERSION, lifespan=_lifespan)
_CORS_ORIGINS = _cors_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials="*" not in _CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Api-Key"],
)

WEB_DIR = Path(__file__).parent / "web"


# ============================================================
# 中间件：管理 API 鉴权
# ============================================================

ADMIN_TOKEN: str = ""
ALLOW_NO_ADMIN_AUTH = False
LOCAL_MODE = False
ALLOW_UNAUTHENTICATED_API = _env_flag("CB_GATEWAY_ALLOW_UNAUTHENTICATED_API", False)
MAX_BODY_BYTES = max(1024, _env_int("CB_GATEWAY_MAX_BODY_BYTES", 32 * 1024 * 1024))
_CURRENT_REQUEST: contextvars.ContextVar[Request | None] = contextvars.ContextVar("current_request", default=None)


def _atomic_write(path: Path, content: str | bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            payload = content.encode("utf-8") if isinstance(content, str) else content
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@app.middleware("http")
async def _request_context(request: Request, call_next):
    management = request.url.path == "/" or request.url.path == "/admin" or request.url.path.startswith("/admin/")
    if LOCAL_MODE and management and not _trusted_local_request(request):
        return JSONResponse({"detail": "Local access requires a loopback host and same-origin request"}, status_code=403)
    token = _CURRENT_REQUEST.set(request)
    try:
        return await call_next(request)
    finally:
        _CURRENT_REQUEST.reset(token)


def _trusted_local_request(request: Request) -> bool:
    try:
        if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
            return False
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
                return False
        if request.headers.get("sec-fetch-site") not in {None, "none", "same-origin"}:
            return False
        return True
    except ValueError:
        return False


def _check_admin(authorization: str | None):
    if LOCAL_MODE:
        request = _CURRENT_REQUEST.get()
        if not request or not _trusted_local_request(request):
            raise HTTPException(status_code=403, detail="Local management requires a trusted local request")
        return
    if ALLOW_NO_ADMIN_AUTH:
        return
    candidates = []
    if authorization:
        parts = authorization.split(" ", 1)
        candidates.append(parts[1] if len(parts) == 2 else parts[0])

    if not any(t and secrets.compare_digest(t, ADMIN_TOKEN) for t in candidates):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _check_client_auth(
    authorization: str | None,
    x_api_key: str | None,
    *,
    consume_quota: bool = True,
):
    """Validate a client API key and atomically reserve its daily quota."""
    keys = db.list_api_keys()
    if not keys:
        if ALLOW_UNAUTHENTICATED_API:
            return None
        raise HTTPException(
            status_code=503,
            detail={"error": {"message": "No API keys configured", "type": "server_error"}},
        )

    token = ""
    if x_api_key:
        token = x_api_key
    elif authorization:
        parts = authorization.split(" ", 1)
        token = parts[1] if len(parts) == 2 else parts[0]

    if not token:
        raise HTTPException(status_code=401, detail={"error": {"message": "API key required", "type": "invalid_request_error"}})

    key_info = db.get_api_key_by_key(token)
    if not key_info:
        raise HTTPException(status_code=401, detail={"error": {"message": "Invalid API key", "type": "invalid_request_error"}})

    daily_limit = int(key_info.get("daily_limit") or 0)
    if consume_quota and not db.reserve_api_key_request(key_info["id"], daily_limit):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "Daily API key request limit exceeded", "type": "rate_limit_error"}},
        )
    return key_info


def _reserve_client_quota(key_info: dict | None):
    if not key_info:
        return
    daily_limit = int(key_info.get("daily_limit") or 0)
    if not db.reserve_api_key_request(key_info["id"], daily_limit):
        raise HTTPException(
            status_code=429,
            detail={"error": {"message": "Daily API key request limit exceeded", "type": "rate_limit_error"}},
        )


def _normalize_domain_pattern(raw) -> str:
    """Accept only `*`, `*.suffix`, or an exact host; normalize to lower-case."""
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="account_domain_filter entries must be strings")
    pattern = raw.strip().lower()
    if not pattern:
        raise HTTPException(status_code=400, detail="account_domain_filter entries must be non-empty")
    if pattern == "*":
        return pattern
    if pattern.startswith("*."):
        suffix = pattern[2:]
        host = auth_manager._host_from(suffix)
        if not suffix or "*" in suffix or host != suffix:
            raise HTTPException(
                status_code=400,
                detail=f"invalid account_domain_filter pattern: {raw!r} "
                       "(use an exact host, '*.suffix', or '*')",
            )
        return pattern
    # Same host parsing as account["domain"], so a saved pattern always names a
    # host the picker can actually compare against.
    host = auth_manager._host_from(pattern)
    if not host or "*" in host or " " in host:
        raise HTTPException(
            status_code=400,
            detail=f"invalid account_domain_filter pattern: {raw!r} "
                   "(use an exact host, '*.suffix', or '*')",
        )
    return host


def _validate_account_domain_filter(raw) -> list | None:
    """API-key scoped account domain patterns (e.g. ["*.workbuddy.ai"]); empty -> None; else 400."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="account_domain_filter must be an array of strings")
    patterns = [_normalize_domain_pattern(item) for item in raw]
    return patterns or None


def _validate_key_channel(channel: str) -> str:
    value = str(channel or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="default_channel is required")
    if value == router.ANY_CHANNEL:
        return value
    if providers.is_retired_channel(value):
        raise HTTPException(status_code=400, detail=providers.retired_message(value))
    if value not in KNOWN_CHANNEL_SET:
        raise HTTPException(status_code=400, detail=f"Unknown channel '{value}'")
    if not providers.is_channel_enabled(value) or providers.get_provider(value) is None:
        raise HTTPException(status_code=400, detail=f"Channel '{value}' is not enabled")
    return value


async def _read_json(request: Request, *, allow_empty: bool = False):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw and allow_empty:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")


async def _read_json_object(request: Request, *, allow_empty: bool = False) -> dict:
    data = await _read_json(request, allow_empty=allow_empty)
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return data


def _invalid_reasoning_http(exc: InvalidReasoningControl) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "code": "invalid_reasoning_control",
            }
        },
    )


async def _gather_limited(accounts: list[dict], operation, limit: int = 4) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(account: dict):
        async with semaphore:
            return await operation(account)

    return list(await asyncio.gather(*(run(account) for account in accounts)))


# ============================================================
# OpenAI 兼容端点
# ============================================================

@app.get("/health")
async def health():
    accounts = db.list_accounts()
    keys = db.list_api_keys()
    channels = {}
    for channel in providers.enabled_provider_ids():
        rows = db.list_accounts(provider=channel)
        channels[channel] = {
            "accounts": len(rows),
            "active": sum(1 for account in rows if account.get("status") == "active"),
            "loaded": providers.get_provider(channel) is not None,
        }
    return {
        "status": "ok",
        "instance": _instance_id(),
        "version": VERSION,
        "accounts": len(accounts),
        "active_accounts": sum(1 for account in accounts if account.get("status") == "active"),
        "active_keys": sum(1 for key in keys if key.get("status") == "active"),
        "channels": channels,
    }


def collect_v1_models() -> list[dict]:
    """Aggregate per-channel catalogs for GET /v1/models. WorkBuddy is bare + namespaced."""
    from model_capacity import discovery_capacity
    from model_reasoning import discovery_reasoning
    data = []
    workbuddy = providers.get_provider("workbuddy")
    wb_models = workbuddy.list_models() if workbuddy else db.get_setting("models", proxy.DEFAULT_MODELS)
    for item in wb_models:
        mid = item["id"] if isinstance(item, dict) else str(item)
        data.append({
            "id": mid,
            "object": "model",
            "created": 0,
            "owned_by": "buddy2api",
            "channel": "workbuddy",
            **discovery_capacity(item),
            **discovery_reasoning(item),
        })
        data.append({
            "id": f"workbuddy/{mid}",
            "object": "model",
            "created": 0,
            "owned_by": "buddy2api",
            "channel": "workbuddy",
            **discovery_capacity(item),
            **discovery_reasoning(item),
        })
    for channel in providers.enabled_provider_ids():
        if channel == "workbuddy":
            continue
        provider = providers.get_provider(channel)
        if provider is None:
            continue
        for item in provider.list_models():
            mid = item["id"] if isinstance(item, dict) else str(item)
            data.append({
                "id": f"{channel}/{mid}",
                "object": "model",
                "created": 0,
                "owned_by": "buddy2api",
                "channel": channel,
                **discovery_capacity(item),
                **discovery_reasoning(item),
            })
    return data


def channels_with_login() -> set[str]:
    """Channels the client catalog may advertise.

    Shared judgment with the router: a channel is offered only when it is
    enabled and holds at least one *active* account with a stored token.
    Disabled (``inactive``) and expired accounts do not keep a channel alive.
    """
    found = set()
    for account in db.list_accounts():
        if str(account.get("status") or "") != "active":
            continue
        if not str(account.get("access_token") or "").strip():
            continue
        found.add(str(account.get("provider") or ""))
    return found


def public_v1_models() -> list[dict]:
    """Client catalog. A channel with no active account is omitted."""
    present = channels_with_login()
    return [item for item in collect_v1_models() if item.get("channel") in present]


@app.get("/v1/models")
async def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    await run_in_threadpool(
        lambda: _check_client_auth(authorization, x_api_key, consume_quota=False)
    )
    return {"object": "list", "data": public_v1_models()}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    api_key_info = await run_in_threadpool(
        lambda: _check_client_auth(authorization, x_api_key, consume_quota=False)
    )
    payload = await _read_json_object(request)

    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages or not all(isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})
    if "model" in payload and not isinstance(payload["model"], str):
        raise HTTPException(status_code=400, detail={"error": {"message": "model must be a string", "type": "invalid_request_error"}})
    try:
        payload = normalize_chat_reasoning(payload)
    except InvalidReasoningControl as exc:
        raise _invalid_reasoning_http(exc) from exc

    # The one admission gate: a name this key cannot use dies here with a 400
    # and never touches the pool. The queue and the key's daily quota are spent
    # inside the dispatcher, in that order, so a request that found no account
    # is never counted against the key.
    router.admit_model(api_key_info, payload.get("model"))
    result = await router.chat_after_name(
        payload.get("model"),
        payload,
        api_key_info,
        after_queue=lambda queue: run_in_threadpool(_reserve_client_quota, api_key_info),
    )

    if result[0] == "error":
        status, detail = result[1]
        return JSONResponse(status_code=status, content=detail)
    elif result[0] == "json":
        return JSONResponse(content=result[1])
    elif result[0] == "stream":
        return StreamingResponse(
            result[1],
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


@app.post("/v1/responses")
async def resp_responses(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    """OpenAI Responses API 兼容端点（Codex wire_api="responses" 支持）。"""
    api_key_info = await run_in_threadpool(
        lambda: _check_client_auth(authorization, x_api_key, consume_quota=False)
    )
    payload = await _read_json_object(request)
    if "input" not in payload:
        raise HTTPException(
            status_code=400,
            detail={"error": {"message": "input is required", "type": "invalid_request_error"}},
        )
    if "model" in payload and not isinstance(payload["model"], str):
        raise HTTPException(status_code=400, detail={"error": {"message": "model must be a string", "type": "invalid_request_error"}})
    try:
        resolve_reasoning_control(payload, prefer_nested=True)
    except InvalidReasoningControl as exc:
        raise _invalid_reasoning_http(exc) from exc
    router.admit_model(api_key_info, payload.get("model"))
    try:
        result = await router.responses_after_name(
            payload.get("model"),
            payload,
            api_key_info,
            after_queue=lambda queue: run_in_threadpool(_reserve_client_quota, api_key_info),
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        sys.stderr.write(f"[responses] ERROR: {e}\n{traceback.format_exc()}\n")
        sys.stderr.flush()
        return JSONResponse(status_code=502, content={"error": {"message": f"internal bridge error: {e}", "type": "server_error"}})

    if result[0] == "error":
        status, detail = result[1]
        return JSONResponse(status_code=status, content=detail)
    elif result[0] == "json":
        return JSONResponse(content=result[1])
    elif result[0] == "stream":
        return StreamingResponse(
            result[1],
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# ============================================================
# Admin API
# ============================================================

@app.get("/admin/channels")
async def admin_channels(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    env_set = bool((os.environ.get("CB_GATEWAY_PROVIDERS") or "").strip())
    in_container = auth_manager._running_in_container()
    items = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        items.append({
            "id": channel,
            "display_name": getattr(provider, "display_name", channel),
            "enabled": True,
            "loaded": provider is not None,
            "checkin_supported": bool(getattr(provider, "checkin_supported", False)),
            "env_locked": env_set,
            "host_auth_limited": bool(
                in_container and channel in {"qclaw", "qwenwork", "loomy", "xiaohuanxiong"}
            ),
        })
    return {
        "channels": items,
        "known": [item for item in KNOWN_CHANNEL_IDS if not providers.is_retired_channel(item)],
    }


@app.get("/admin/stats")
async def admin_stats(
    period: str = "today",
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    if period not in db.STATS_PERIODS:
        raise HTTPException(status_code=400, detail="invalid period")
    return await run_in_threadpool(db.get_stats, period)


@app.get("/admin/credit-summary")
async def admin_credit_summary(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await control_plane.credit_summary(force=bool(force))


# --- Accounts ---

def _global_rank_map() -> tuple[dict[int, int], dict[int, int]]:
    """Execution ranks for every account the router could spend right now.

    Computed from the router's own ordering and returned as
    ``(rank_by_account, global_rank_by_account)``:

      rank         position inside the account's own channel pool (1-based)
      global_rank  position in the one routing queue (``router.account_queue``),
                   i.e. priority first, then the configured channel order,
                   numbered 1..N without a gap

    An account that is not in any pool — expired, or a quota that is provably
    used up — is absent from both maps, which the callers report as null. Both
    admin endpoints share this one function so the page and the preview can
    never disagree about a rank.
    """
    rank_by_account: dict[int, int] = {}
    global_rank_by_account: dict[int, int] = {}
    per_channel: dict[str, int] = {}
    for position, (cid, account) in enumerate(router.account_queue(), start=1):
        aid = int(account["id"])
        per_channel[cid] = per_channel.get(cid, 0) + 1
        rank_by_account[aid] = per_channel[cid]
        global_rank_by_account[aid] = position
    return rank_by_account, global_rank_by_account


def _static_rank_map(accounts: list[dict] | None = None) -> dict[int, int]:
    """A number for every account in the table, state-independent, never null.

    The account list sorts; it does not route. An expired, disabled, cooling
    down or out-of-quota account is still a row the operator reorders, so this
    map reads the whole table and applies only the static keys (priority, the
    configured channel order, id) — see ``routing.static_order_index``. It takes
    the channel order from the router so "channel order" means the same list the
    model-config page shows. ``accounts`` is the list the caller already read:
    passing it in keeps the numbering tied to the very rows being served (and
    saves a second read of the table).
    """
    if accounts is None:
        accounts = db.list_accounts()
    return routing.static_order_index(accounts, router._channel_order())


@app.get("/admin/accounts")
async def admin_list_accounts(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    accounts = db.list_accounts()
    # The page only displays these ranks — it never re-sorts by its own copy of
    # the keys, which is how the two drifted apart before. channel_rank is the
    # third layer: the channel's position in the model-config order (0-based).
    channel_rank = {cid: i for i, cid in enumerate(router._channel_order())}
    account_rank, account_global_rank = _global_rank_map()
    account_static_rank = _static_rank_map(accounts)
    result = []
    for a in accounts:
        s = auth_manager.get_account_status(a)
        s["phone"] = a.get("phone", "")
        s["account_type"] = a.get("account_type", "")
        s["enterprise_id"] = a.get("enterprise_id", "")
        s["domain"] = a.get("domain", "")
        s["weight"] = int(a.get("weight") or 1)
        s["priority"] = int(a.get("priority") or 0)
        s["credit_limit"] = float(a.get("credit_limit") or 0)
        s["provider"] = a.get("provider") or "workbuddy"
        channel = a.get("provider") or "workbuddy"
        # urgency_days: None means "automatic". The effective value and where it
        # came from are both sent so the page can show an editable box with the
        # default it is overriding.
        raw_urgency = a.get("urgency_days")
        aid = int(a.get("id") or 0)
        s["urgency_days"] = None if raw_urgency in (None, "") else float(raw_urgency)
        s["urgency_effective"] = round(routing.urgency_days(a, channel), 4)
        s["urgency_source"] = routing.urgency_source(a, channel)
        s["quota_state"] = routing.quota_state(a)
        s["routing"] = {
            "priority": int(a.get("priority") or 0),
            "urgency_days": None if raw_urgency in (None, "") else float(raw_urgency),
            "urgency_effective": round(routing.urgency_days(a, channel), 4),
            "urgency_source": routing.urgency_source(a, channel),
            "quota_state": routing.quota_state(a),
            "remaining": routing.remaining_quota(a),
            "rpm_used": routing.rpm_used(aid),
            "rpm_cap": routing.account_rpm_cap(),
            "inflight_cap": routing.account_inflight_cap(),
            # rank: position inside its own channel; channel_rank: position of
            # that channel in the model-config order; global_rank: position in
            # the whole account pool across every channel that would be tried.
            # All three null when the account is gated out entirely.
            # static_rank: the table's own numbering over EVERY account — the
            # operator's sort key, so it is never null and never moves when an
            # account's state (or its quota) changes.
            "rank": account_rank.get(aid),
            "channel_rank": channel_rank.get(channel),
            "global_rank": account_global_rank.get(aid),
            "static_rank": account_static_rank.get(aid),
        }
        result.append(s)
    return result


@app.get("/admin/accounts/discover")
async def admin_discover_accounts(
    auth_dir: str | None = None,
    channel: str | None = None,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    try:
        return await run_in_threadpool(control_plane.discover, channel, auth_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/admin/accounts/import")
async def admin_import_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    channel = str(data.get("channel") or "workbuddy").strip() or "workbuddy"
    token = str(data.get("preview_token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="preview_token is required")
    paths = data.get("paths")
    if paths is not None and (
        not isinstance(paths, list) or not all(isinstance(item, str) for item in paths)
    ):
        raise HTTPException(status_code=400, detail="paths must be an array of strings")
    try:
        return await run_in_threadpool(
            control_plane.import_channel, channel, token, paths, data.get("auth_dir")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/admin/accounts/scan")
async def admin_scan_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    auth_dir = data.get("auth_dir") if isinstance(data, dict) else None
    return await run_in_threadpool(auth_manager.auto_scan_and_import, auth_dir)


@app.post("/admin/accounts")
async def admin_add_account(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    provider_id = str(data.get("provider") or data.get("channel") or "").strip()
    if provider_id and provider_id != "workbuddy":
        if providers.is_retired_channel(provider_id):
            raise HTTPException(status_code=400, detail=providers.retired_message(provider_id))
        provider = providers.get_provider(provider_id)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{provider_id}' is not enabled")
        parse_credentials = getattr(provider, "parse_credentials", None)
        if parse_credentials is None:
            raise HTTPException(status_code=400, detail=f"Channel '{provider_id}' does not support pasted credentials")
        try:
            parsed = parse_credentials(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        upsert = getattr(provider, "upsert_account", None)
        if upsert is None and provider_id == "qclaw":
            result = upsert_qclaw_account(parsed)
        elif upsert is None:
            aid = db.add_account({**parsed, "provider": provider_id})
            result = {"id": aid, "updated": False}
        else:
            result = upsert(parsed)
        return {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": provider_id}
    # 直接粘贴 auth JSON
    auth_data = data.get("auth", {})
    account_data = data.get("account", {})
    if not isinstance(auth_data, dict) or not isinstance(account_data, dict):
        raise HTTPException(status_code=400, detail="auth and account must be JSON objects")
    parsed = {
        "name": account_data.get("nickname", data.get("name", "")),
        "uid": account_data.get("uid", ""),
        "nickname": account_data.get("nickname", ""),
        "phone": account_data.get("phoneNumber", ""),
        "account_type": account_data.get("type", "personal"),
        "access_token": auth_data.get("accessToken", ""),
        "refresh_token": auth_data.get("refreshToken", ""),
        "expires_at": auth_data.get("expiresAt", 0),
        "refresh_expires_at": auth_data.get("refreshExpiresAt", 0),
        "domain": auth_data.get("domain", "www.codebuddy.cn"),
        "enterprise_id": account_data.get("enterpriseId", ""),
        "session_state": auth_data.get("sessionState", ""),
    }
    if not parsed["access_token"]:
        raise HTTPException(status_code=400, detail="No accessToken found in auth data")
    aid = db.add_account(parsed)
    return {"id": aid, "status": "ok"}


def _qclaw_provider():
    raise HTTPException(status_code=400, detail=providers.retired_message("qclaw"))


@app.post("/admin/qclaw/import-path")
async def admin_qclaw_import_path(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    provider = _qclaw_provider()
    data = await _read_json_object(request)
    path = str(data.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    try:
        parsed = provider.import_path(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = upsert_qclaw_account(parsed)
    return {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "qclaw"}


@app.post("/admin/qclaw/login/start")
async def admin_qclaw_login_start(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    provider = _qclaw_provider()
    data = await _read_json_object(request, allow_empty=True)
    guid = str((data or {}).get("guid") or default_guid() or "").strip()
    if not guid:
        raise HTTPException(status_code=400, detail="guid is required (or login to official QClaw once)")
    return await provider.start_login(guid)


@app.post("/admin/qclaw/login/complete")
async def admin_qclaw_login_complete(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    provider = _qclaw_provider()
    data = await _read_json_object(request)
    from providers.qclaw.oauth import parse_callback

    guid = str(data.get("guid") or default_guid() or "").strip()
    callback = str(data.get("callback") or data.get("code") or "").strip()
    if not guid or not callback:
        raise HTTPException(status_code=400, detail="guid and callback/code are required")
    parsed_cb = parse_callback(callback)
    state = str(data.get("state") or parsed_cb.get("state") or "")
    try:
        parsed = await provider.complete_login(guid, parsed_cb["code"], state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = upsert_qclaw_account(parsed)
    return {"id": result["id"], "status": "ok", "updated": result["updated"], "provider": "qclaw"}


def _workbuddy_provider():
    provider = providers.get_provider("workbuddy")
    if provider is None:
        raise HTTPException(status_code=400, detail="Channel 'workbuddy' is not enabled")
    return provider


@app.post("/admin/workbuddy/login/start")
async def admin_workbuddy_login_start(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    realm = str((data or {}).get("realm") or "cn").strip().lower()
    provider = providers.get_provider("workbuddy-i" if realm == "global" else "workbuddy")
    if provider is None:
        raise HTTPException(status_code=400, detail="WorkBuddy channel is not enabled")
    if realm not in {"cn", "global"}:
        raise HTTPException(status_code=400, detail="realm must be cn or global")
    try:
        return await provider.start_login(realm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:180]) from exc


@app.get("/admin/workbuddy/login/poll")
async def admin_workbuddy_login_poll(
    response: Response,
    state: str = "",
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    response.headers["Cache-Control"] = "no-store"
    provider = _workbuddy_provider()
    if not str(state or "").strip():
        raise HTTPException(status_code=400, detail="state is required")
    try:
        return await provider.poll_login(state)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="登录已超时，请重新开始") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:180]) from exc


@app.put("/admin/accounts/{aid}")
async def admin_update_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    allowed = {"name", "status", "weight", "priority", "credit_limit", "credit_baseline", "urgency_days"}
    update_data = {k: data[k] for k in allowed if k in data}
    if "status" in update_data and update_data["status"] not in {"active", "inactive", "expired"}:
        raise HTTPException(status_code=400, detail="Invalid account status")
    if "credit_limit" in update_data and "credit_baseline" not in update_data:
        account = db.get_account(aid)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        update_data["credit_baseline"] = float(account.get("total_credits") or 0)
    for field in ("weight", "priority", "credit_limit", "credit_baseline"):
        if field in update_data:
            try:
                update_data[field] = float(update_data[field]) if field.startswith("credit_") else int(update_data[field])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{field} must be numeric")
    if "weight" in update_data and update_data["weight"] < 1:
        raise HTTPException(status_code=400, detail="weight must be at least 1")
    if "urgency_days" in update_data:
        # Empty string / null clears the override, so the automatic value applies
        # again. This is the "reset" button next to the input box.
        raw_urgency = update_data["urgency_days"]
        if raw_urgency in (None, ""):
            update_data["urgency_days"] = None
        else:
            try:
                update_data["urgency_days"] = max(0.0, float(raw_urgency))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="urgency_days must be numeric or empty")
    db.update_account(aid, update_data)
    return {"status": "ok"}


@app.delete("/admin/accounts/{aid}")
async def admin_delete_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_account(aid)
    return {"status": "ok"}


@app.post("/admin/accounts/{aid}/refresh")
async def admin_refresh_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    result = await control_plane.refresh_one(account)
    result["status"] = "ok" if result.get("ok") else "failed"
    return result


@app.post("/admin/accounts/{aid}/test")
async def admin_test_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    data = await _read_json_object(request, allow_empty=True)
    model = data.get("model") if isinstance(data, dict) else None
    prompt = data.get("prompt") if isinstance(data, dict) else None
    channel = str(account.get("provider") or "workbuddy")
    if channel != "workbuddy":
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        test = getattr(provider, "test_chat", None)
        if test is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' does not support account test")
        default_prompt = "请回复：pong" if channel == "traework" else "ping"
        return await test(account, model or "auto", prompt or default_prompt)
    return await proxy.test_account_chat(account, model or "auto", prompt or "ping")


@app.get("/admin/accounts/{aid}/resources")
async def admin_account_resources(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    channel = str(account.get("provider") or "workbuddy")
    if channel not in {"workbuddy", "workbuddy-i"}:
        provider = providers.get_provider(channel)
        if provider is None:
            raise HTTPException(status_code=400, detail=f"Channel '{channel}' is not enabled")
        fetch_quota = getattr(provider, "fetch_quota", None)
        if fetch_quota is None:
            return {
                "ok": True,
                "unsupported": True,
                "account_id": aid,
                "unit": "unknown",
                "remaining": None,
                "message": "quota API not available",
            }
        snapshot = await fetch_quota(account)
        extra = getattr(snapshot, "extra", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        unit = str(getattr(snapshot, "unit", "") or "unknown")
        remaining = getattr(snapshot, "remaining", None)
        unsupported = bool(getattr(snapshot, "unsupported", False))
        credit_remaining = remaining if unit == "credit" and not unsupported else None
        if bool(getattr(snapshot, "ok", False)) and credit_remaining is not None:
            # Route on what we just measured. Routing reads nothing but the
            # cache, so a live "0 credits" report that never reaches it leaves
            # the account sitting at rank #1 in its channel pool. Best effort:
            # failing to cache a reading must not fail the report itself.
            try:
                resource_sync.store_quota_reading(
                    account,
                    remaining=credit_remaining,
                    source=f"{channel}-quota-check",
                )
            except Exception:
                pass
        packages = extra.get("packages") if isinstance(extra.get("packages"), list) else []
        return {
            "ok": bool(getattr(snapshot, "ok", False)),
            "account_id": aid,
            "unit": unit,
            "remaining": remaining,
            "used": extra.get("used"),
            "limit": extra.get("limit"),
            "total_dosage": credit_remaining,
            "available_total": credit_remaining,
            "unsupported": unsupported,
            "message": getattr(snapshot, "message", "") or "",
            "packages": packages,
        }
    return await auth_manager.fetch_account_resources(account, force=bool(force))


@app.get("/admin/accounts/{aid}/checkin")
async def admin_checkin_status(
    aid: int,
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return await control_plane.checkin_status_one(account, force=bool(force))


@app.get("/admin/accounts/checkin-status-all")
async def admin_checkin_status_all(
    force: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await control_plane.checkin_status_all(force=bool(force))


@app.post("/admin/accounts/{aid}/checkin")
async def admin_claim_checkin(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return await control_plane.claim_one(account)


@app.post("/admin/accounts/checkin-all")
async def admin_claim_all_checkin(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    channels = data.get("channels") if isinstance(data, dict) else None
    if channels is not None and (
        not isinstance(channels, list) or not all(isinstance(item, str) for item in channels)
    ):
        raise HTTPException(status_code=400, detail="channels must be an array of strings")
    return await control_plane.checkin_all(channels, source="manual")


@app.post("/admin/accounts/keepalive-all")
async def admin_keepalive_all(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request, allow_empty=True)
    channels = data.get("channels") if isinstance(data, dict) else None
    if channels is not None and (
        not isinstance(channels, list) or not all(isinstance(item, str) for item in channels)
    ):
        raise HTTPException(status_code=400, detail="channels must be an array of strings")
    return await control_plane.keepalive_all(channels, source="manual", claim=True)


# --- API Keys ---

@app.get("/admin/api-keys")
async def admin_list_keys(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.list_api_keys(include_secret=True)


@app.post("/admin/api-keys")
async def admin_create_key(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    name = str(data.get("name", "")).strip()[:120]
    allowed = data.get("allowed_models")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(isinstance(model, str) for model in allowed)
    ):
        raise HTTPException(status_code=400, detail="allowed_models must be an array of strings")
    try:
        daily_limit = max(0, int(data.get("daily_limit") or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="daily_limit must be a non-negative integer")
    client_type = data.get("client_type", "custom")
    if client_type not in {"custom", "codex"}:
        raise HTTPException(status_code=400, detail="Invalid client_type")
    if "default_channel" not in data or data.get("default_channel") in (None, ""):
        raise HTTPException(status_code=400, detail="default_channel is required")
    default_channel = _validate_key_channel(data.get("default_channel"))
    key = f"sk-cb-{secrets.token_urlsafe(32)}"
    kid = db.add_api_key(
        key, name, allowed, daily_limit, client_type, default_channel=default_channel,
    )
    return {"id": kid, "key": key, "status": "ok", "default_channel": default_channel}


@app.put("/admin/api-keys/{kid}")
async def admin_update_key(
    kid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    if "daily_limit" in data:
        try:
            data["daily_limit"] = max(0, int(data["daily_limit"] or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="daily_limit must be a non-negative integer")
    if "status" in data and data["status"] not in {"active", "inactive"}:
        raise HTTPException(status_code=400, detail="Invalid API key status")
    if "client_type" in data and data["client_type"] not in {"custom", "codex"}:
        raise HTTPException(status_code=400, detail="Invalid client_type")
    if "default_channel" in data:
        data["default_channel"] = _validate_key_channel(data.get("default_channel"))
    if "allowed_models" in data and (
        data["allowed_models"] is not None
        and (not isinstance(data["allowed_models"], list) or not all(isinstance(model, str) for model in data["allowed_models"]))
    ):
        raise HTTPException(status_code=400, detail="allowed_models must be an array of strings")
    data.pop("account_domain_filter", None)
    db.update_api_key(kid, data)
    return {"status": "ok"}


@app.delete("/admin/api-keys/{kid}")
async def admin_delete_key(
    kid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_api_key(kid)
    return {"status": "ok"}


# --- Logs ---

@app.get("/admin/logs")
async def admin_logs(
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return db.list_logs(max(1, min(500, limit)), max(0, offset))


@app.get("/admin/logs/search")
async def admin_logs_search(
    q: str | None = None,
    status: str = "all",
    account_id: str | None = None,
    api_key_id: str | None = None,
    model: str | None = None,
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    if account_id not in (None, "", "all") and not str(account_id).isdigit():
        raise HTTPException(status_code=400, detail="account_id must be numeric")
    if api_key_id not in (None, "", "all") and not str(api_key_id).isdigit():
        raise HTTPException(status_code=400, detail="api_key_id must be numeric")
    return db.search_logs({
        "q": q or "",
        "status": status,
        "account_id": account_id,
        "api_key_id": api_key_id,
        "model": model or "",
        "limit": limit,
        "offset": offset,
    })


# --- Settings ---

@app.get("/admin/version")
async def admin_get_version(
    refresh: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return await self_update.check(force=bool(refresh))


@app.post("/admin/update")
async def admin_apply_update(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    result = await run_in_threadpool(self_update.apply)
    if result.get("restart"):
        self_update.schedule_restart()
    return result

_SERVER = None
_SHUTDOWN_STARTED = False


def bind_runtime_server(server) -> None:
    global _SERVER
    _SERVER = server


def reset_shutdown_state() -> None:
    global _SHUTDOWN_STARTED
    _SHUTDOWN_STARTED = False


def schedule_shutdown(delay: float = 0.4) -> dict:
    """Stop this process after the response is sent. Does not change Git or Docker."""
    if self_update.in_docker():
        return {
            "ok": False,
            "message": "Docker 部署请在宿主机执行 docker compose stop。容器自动重启时，管理页关不掉它",
        }
    global _SHUTDOWN_STARTED
    if not _SHUTDOWN_STARTED:
        _SHUTDOWN_STARTED = True
        threading.Thread(target=_shutdown_after, args=(delay,), daemon=True).start()
    return {
        "ok": True,
        "message": "正在关闭 Buddy2api。页面即将打不开，下次请重新运行 start.bat",
    }


def _shutdown_after(delay: float) -> None:
    time.sleep(max(0.0, delay))
    server = _SERVER
    if server is not None:
        server.should_exit = True
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    os._exit(0)


@app.post("/admin/shutdown")
async def admin_shutdown(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return schedule_shutdown()


@app.get("/admin/automation-logs")
async def admin_automation_logs(
    limit: int = 100,
    failures_only: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return {
        "items": db.list_automation_events(limit=limit, failures_only=bool(failures_only)),
        "latest": db.latest_automation_runs(),
    }


@app.get("/admin/settings")
async def admin_get_settings(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_all_settings()


@app.put("/admin/settings")
async def admin_update_settings(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    allowed_settings = {
        "backend_url",
        "default_domain",
        "timeout",
        "keepalive_enabled",
        "keepalive_hour",
        "auto_checkin_enabled",
        "auto_checkin_hour",
        "loomy_context_budget",
        "context_channel_budgets",
    }
    unknown = set(data) - allowed_settings
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported settings: {', '.join(sorted(unknown))}")
    for flag in ("keepalive_enabled", "auto_checkin_enabled"):
        if flag in data:
            data[flag] = control_plane.as_bool(data[flag], False)
    for hour_key in ("keepalive_hour", "auto_checkin_hour"):
        if hour_key not in data:
            continue
        try:
            hour = int(data[hour_key])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{hour_key} must be an integer between 0 and 23")
        if not 0 <= hour <= 23:
            raise HTTPException(status_code=400, detail=f"{hour_key} must be an integer between 0 and 23")
        data[hour_key] = hour
    if "context_channel_budgets" in data:
        budgets = data["context_channel_budgets"]
        if not isinstance(budgets, dict):
            raise HTTPException(status_code=400, detail="context_channel_budgets must be an object")
        for channel, budget in budgets.items():
            if channel not in providers.DEFAULT_PROVIDER_IDS or channel == "loomy":
                raise HTTPException(status_code=400, detail=f"Unsupported budget channel: {channel}")
            if type(budget) is not int or not 0 <= budget <= 2_000_000:
                raise HTTPException(status_code=400, detail=f"Budget for {channel} must be an integer between 0 and 2000000")
    if "loomy_context_budget" in data:
        try:
            budget = int(data["loomy_context_budget"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="loomy_context_budget must be an integer")
        if budget < 0 or budget > 2_000_000:
            raise HTTPException(status_code=400, detail="loomy_context_budget must be between 0 and 2000000")
        data["loomy_context_budget"] = budget
    if "timeout" in data:
        try:
            data["timeout"] = max(5, min(600, int(data["timeout"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="timeout must be an integer between 5 and 600")
    if "backend_url" in data:
        backend_url = str(data["backend_url"]).strip().rstrip("/")
        if not backend_url.startswith("https://"):
            raise HTTPException(status_code=400, detail="backend_url must use HTTPS")
        data["backend_url"] = backend_url
    for k, v in data.items():
        db.set_setting(k, v)
    if "context_channel_budgets" in data:
        import context_budget

        context_budget.set_saved_channel_budgets(data["context_channel_budgets"])
    return {"status": "ok"}


# --- Models ---

@app.get("/admin/models")
async def admin_get_models(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_setting("models", proxy.DEFAULT_MODELS)


@app.get("/admin/models/catalogs")
async def admin_get_model_catalogs(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return catalog.catalog_snapshot()


@app.post("/admin/models/refresh")
async def admin_refresh_models(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return await catalog.refresh_supplier_catalogs()


@app.put("/admin/models")
async def admin_update_models(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json(request)
    if not isinstance(data, list) or not all(
        isinstance(model, dict) and isinstance(model.get("id"), str) and model.get("id")
        for model in data
    ):
        raise HTTPException(status_code=400, detail="Models must be an array of objects with an id")
    db.set_setting("models", data)
    return {"status": "ok"}


@app.post("/admin/models/catalogs")
async def admin_upsert_catalog_model(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    channel = str(data.get("channel") or "").strip()
    model_id = str(data.get("id") or data.get("model") or "").strip()
    name = str(data.get("name") or "").strip()
    try:
        return catalog.upsert_model(channel, model_id, name)
    except catalog.CatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/admin/models/catalogs")
async def admin_remove_catalog_model(
    channel: str,
    model_id: str,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    try:
        return catalog.remove_model(channel, model_id)
    except catalog.CatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_TRUE_FLAGS = {"1", "true", "yes", "on"}
_FALSE_FLAGS = {"0", "false", "no", "off"}


def _flag(value, field: str) -> bool:
    """Read a JSON boolean, or the words people send instead of one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_FLAGS:
            return True
        if token in _FALSE_FLAGS:
            return False
    raise HTTPException(status_code=400, detail=f"{field} must be a boolean")


@app.post("/admin/models/exempt")
async def admin_set_model_exempt(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Mark one model 放行: quota readings never keep its accounts out of routing."""
    _check_admin(authorization)
    data = await _read_json_object(request)
    channel = str(data.get("channel") or "").strip()
    model_id = str(data.get("id") or data.get("model") or "").strip()
    exempt = _flag(data.get("exempt", True), "exempt")
    try:
        return catalog.set_exempt(channel, model_id, exempt)
    except catalog.CatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- Codex 一键配置 ---

@app.post("/admin/codex/setup")
async def admin_codex_setup(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """一键配置 Codex：写入 config.toml 和 auth.json。"""
    _check_admin(authorization)
    data = await _read_json_object(request)
    api_key = str(data.get("api_key", "")).strip()
    if not api_key.startswith("sk-cb-") or len(api_key) > 256:
        raise HTTPException(status_code=400, detail="api_key is required")

    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)

    config_path = codex_dir / "config.toml"
    auth_path = codex_dir / "auth.json"

    # 备份现有文件
    results = {"backed_up": [], "written": [], "config_path": str(config_path), "auth_path": str(auth_path)}
    for p in [config_path, auth_path]:
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            _atomic_write(bak, p.read_bytes())
            results["backed_up"].append(str(bak))

    # 读取现有 config.toml，保留 marketplaces 等非冲突段
    existing_config = ""
    if config_path.exists():
        existing_config = config_path.read_text(encoding="utf-8")

    # 构建新的 config.toml
    # 保留 [marketplaces.*] 和 [projects.*] 和 [desktop] 段，替换/插入顶层和 provider
    new_lines = []
    in_skip_section = False
    skip_section_prefixes = ["[model_providers", "model ", "model=", "model_provider"]

    for line in existing_config.splitlines():
        stripped = line.strip()
        # 跳过旧的 model / model_provider / [model_providers.*] 行
        if any(stripped.startswith(prefix) for prefix in ["model =", "model=", "model_provider"]):
            continue
        if stripped.startswith("[model_providers"):
            in_skip_section = True
            continue
        if in_skip_section:
            if stripped.startswith("[") and not stripped.startswith("[model_providers"):
                in_skip_section = False
                new_lines.append(line)
            else:
                continue
        else:
            new_lines.append(line)

    # 在文件开头插入 model 和 provider 配置
    codex_config = f'''model = "auto"
model_provider = "buddy2api"

[model_providers.buddy2api]
name = "Buddy2api"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"

'''
    # 保留原有内容（去掉了旧 model 配置）
    preserved = "\n".join(new_lines).strip()
    final_config = codex_config + ("\n" + preserved if preserved else "")
    _atomic_write(config_path, final_config)
    results["written"].append(str(config_path))

    # 写 auth.json
    import json as _json
    auth_content = _json.dumps({"OPENAI_API_KEY": api_key}, indent=2)
    _atomic_write(auth_path, auth_content)
    results["written"].append(str(auth_path))

    # 当前进程保留变量；持久化凭据只写入权限受限的 auth.json。
    os.environ["OPENAI_API_KEY"] = api_key

    results["status"] = "ok"
    results["message"] = "Codex 配置已写入。请完全关闭 Codex 后重新打开。"
    return results


@app.get("/admin/codex/status")
async def admin_codex_status(authorization: str | None = Header(default=None)):
    """检查 Codex 配置状态。"""
    _check_admin(authorization)
    codex_dir = Path.home() / ".codex"
    config_path = codex_dir / "config.toml"
    auth_path = codex_dir / "auth.json"

    result = {
        "codex_dir_exists": codex_dir.exists(),
        "config_exists": config_path.exists(),
        "auth_exists": auth_path.exists(),
        "config_has_buddy2api": False,
        "config_wire_api": None,
        "config_model": None,
        "auth_has_key": False,
    }

    if config_path.exists():
        content = config_path.read_text(encoding="utf-8")
        result["config_has_buddy2api"] = "buddy2api" in content
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("wire_api"):
                result["config_wire_api"] = s.split("=", 1)[1].strip().strip('"')
            elif s.startswith("model ") or s.startswith("model="):
                result["config_model"] = s.split("=", 1)[1].strip().strip('"')

    if auth_path.exists():
        try:
            import json as _json
            auth = _json.loads(auth_path.read_text(encoding="utf-8"))
            result["auth_has_key"] = bool(auth.get("OPENAI_API_KEY"))
        except Exception:
            pass

    return result


# --- Model Aliases ---

@app.get("/admin/aliases")
async def admin_get_aliases(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    import aliases

    return aliases.snapshot()


@app.put("/admin/aliases")
async def admin_update_aliases(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    import aliases

    data = await _read_json_object(request)
    try:
        aliases.save_user_aliases(data)
    except aliases.AliasError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


# --- Channel routing order ---

@app.get("/admin/channel-order")
async def admin_get_channel_order(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    import providers
    import router

    order = router._channel_order()
    routable = set(router.routable_channel_ids())
    rows = []
    for cid in order:
        provider = providers.get_provider(cid)
        rows.append({
            "id": cid,
            "display_name": getattr(provider, "display_name", None) or cid,
            "routable": cid in routable,
        })
    return {"order": order, "channels": rows}


@app.put("/admin/channel-order")
async def admin_update_channel_order(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    import router

    data = await _read_json_object(request)
    raw = data.get("order")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="order must be a list of channel ids")
    normalized = router._normalize_channel_order([str(x) for x in raw])
    db.set_setting("channel_order", normalized)
    return {"status": "ok", "order": normalized}


# --- Routing behaviour (caps, watermark, per-channel urgency) ---

@app.get("/admin/routing-settings")
async def admin_get_routing_settings(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return {
        "settings": routing.routing_settings(),
        "defaults": dict(routing.DEFAULT_SETTINGS),
        "channel_urgency": dict(routing.DEFAULT_CHANNEL_URGENCY),
    }


@app.put("/admin/routing-settings")
async def admin_update_routing_settings(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await _read_json_object(request)
    written: dict = {}

    for key in ("routing_account_rpm", "routing_account_inflight"):
        if key in data:
            try:
                value = max(0, int(float(data[key])))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be numeric")
            db.set_setting(key, value)
            written[key] = value

    if "routing_inflight_window_seconds" in data:
        try:
            window = max(1.0, float(data["routing_inflight_window_seconds"]))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="routing_inflight_window_seconds must be numeric")
        db.set_setting("routing_inflight_window_seconds", window)
        written["routing_inflight_window_seconds"] = window

    if "routing_low_watermark_pct" in data:
        try:
            pct = max(0.0, min(100.0, float(data["routing_low_watermark_pct"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="routing_low_watermark_pct must be numeric")
        db.set_setting("routing_low_watermark_pct", pct)
        written["routing_low_watermark_pct"] = pct

    if "routing_channel_urgency" in data:
        raw_map = data["routing_channel_urgency"]
        if not isinstance(raw_map, dict):
            raise HTTPException(status_code=400, detail="routing_channel_urgency must be an object")
        clean: dict[str, float] = {}
        for cid, value in raw_map.items():
            if value in (None, ""):
                continue
            try:
                clean[str(cid)] = max(0.0, float(value))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"urgency for {cid} must be numeric")
        db.set_setting("routing_channel_urgency", clean)
        written["routing_channel_urgency"] = clean

    return {"status": "ok", "settings": routing.routing_settings(), "written": written}


@app.get("/admin/routing-plan")
async def admin_routing_plan(
    model: str | None = None,
    channel: str | None = None,
    authorization: str | None = Header(default=None),
):
    """Show the exact queue a request would walk — computed once, here.

    The page never re-sorts anything: this returns the same
    ``router.account_queue`` list the dispatcher walks, grouped per channel so
    it stays readable. Each block carries ``try_order`` (1-based position of the
    channel in that queue) and ``channel_rank`` (0-based position in the
    model-config order); each account row carries the same ``global_rank`` the
    accounts page shows.
    """
    _check_admin(authorization)
    requested = (model or "").strip()
    key_info = {"default_channel": channel} if channel else None
    queue = router.account_queue(requested or None, key_info)
    ordered_channels: list[str] = []
    by_channel: dict[str, list[dict]] = {}
    for cid, account in queue:
        if cid not in by_channel:
            by_channel[cid] = []
            ordered_channels.append(cid)
        by_channel[cid].append(account)

    if not ordered_channels and requested:
        # Nothing can be spent right now; still report the channels that know
        # this model so the page can explain why the queue is empty. Once the
        # caller names a channel, only that channel can be the answer.
        prefix, base = router._namespaced(requested)
        known = [prefix] if prefix else router._channels_for_name(base)
        if channel:
            known = [cid for cid in known if cid == channel]
        ordered_channels = known

    channel_rank = {cid: i for i, cid in enumerate(router._channel_order())}
    _, account_global_rank = _global_rank_map()
    plans = []
    for try_order, cid in enumerate(ordered_channels, start=1):
        provider = providers.get_provider(cid)
        routed = []
        for index, account in enumerate(by_channel.get(cid) or [], start=1):
            row = routing.describe_account(account, index, cid)
            row["name"] = account.get("name") or row["name"]
            row["global_rank"] = account_global_rank.get(int(account["id"]))
            routed.append(row)
        plans.append({
            "channel": cid,
            "display_name": getattr(provider, "display_name", None) or cid,
            # try_order: where this channel sits in the queue (1-based).
            # channel_rank: its position in the model-config order (0-based).
            "try_order": try_order,
            "channel_rank": channel_rank.get(cid),
            "accounts": routed,
        })
    return {
        "model": requested or None,
        "channels": plans,
        "settings": routing.routing_settings(),
    }


# ============================================================
# Web UI
# ============================================================

def _render_index_html() -> str:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("/* LOCAL_MODE */ false", "true" if LOCAL_MODE else "false")
    return html.replace("__APP_VERSION__", VERSION)


@app.get("/")
async def index(request: Request):
    response = HTMLResponse(
        _render_index_html(),
        headers={"Cache-Control": "no-store", "Content-Security-Policy": "frame-ancestors 'none'"},
    )
    return response


@app.get("/web/vendor/{filename}")
async def web_vendor(filename: str):
    """本地前端依赖（Vue）。jsdelivr 等境外 CDN 在国内网络下会被重置连接，改成本地文件。"""
    if filename != "vue.global.prod.min.js":
        raise HTTPException(status_code=404, detail="not found")
    path = WEB_DIR / "vendor" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="vendor asset missing")
    return FileResponse(path, media_type="text/javascript", headers={"Cache-Control": "public, max-age=86400"})


# ============================================================
# 启动
# ============================================================

def _instance_id():
    return hashlib.sha256(str(db.DB_PATH.resolve()).encode()).hexdigest()


def _lock_database():
    path = Path(str(db.DB_PATH.resolve()) + ".instance.lock.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("This database is already in use by another Buddy2api instance")
    return handle


def _open_when_ready(server, url):
    while not server.started and not server.should_exit:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)


def _debugger_replaced_asyncio_run() -> bool:
    """pydevd/nest_asyncio 会换掉 asyncio.run，且不接受 uvicorn 0.52 的 loop_factory。"""
    return getattr(asyncio.run, "__module__", "") != "asyncio.runners"


def _serve_without_uvicorn_runner(server, listener) -> None:
    """只在调试器换掉 asyncio.run 时绕开 Server.run，仍走 get_loop_factory。"""
    loop_factory = server.config.get_loop_factory()
    loop = loop_factory() if loop_factory else asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve(sockets=[listener]))
    finally:
        try:
            asyncio.set_event_loop(None)
        finally:
            loop.close()


def main():
    global ADMIN_TOKEN, ALLOW_NO_ADMIN_AUTH, LOCAL_MODE

    ap = argparse.ArgumentParser(description="Buddy 2 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--admin-token", default=os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
                    help="Explicit management token; required for non-loopback listeners.")
    ap.add_argument("--no-browser", action="store_true", help="Do not open the local management page")
    ap.add_argument("--no-admin-auth", action="store_true",
                    help="Use automatic local management access with request-origin validation.")
    ap.add_argument("--log-level", default="warning", choices=["debug","info","warning","error"],
                    help="Log level")
    args = ap.parse_args()
    if not 1 <= args.port <= 65535:
        ap.error("--port must be between 1 and 65535")

    if args.no_admin_auth and args.host not in {"127.0.0.1", "localhost", "::1"}:
        ap.error("--no-admin-auth can only be used with a loopback host")

    local_host = args.host in {"127.0.0.1", "localhost", "::1"}
    if not local_host and not args.admin_token:
        ap.error("Remote access requires --admin-token or CB_GATEWAY_ADMIN_TOKEN")
    LOCAL_MODE = local_host and (args.no_admin_auth or not args.admin_token)
    ALLOW_NO_ADMIN_AUTH = False
    ADMIN_TOKEN = "" if LOCAL_MODE else args.admin_token

    host = "127.0.0.1" if args.host == "localhost" else args.host
    url_host = f"[{host}]" if ":" in host else host
    url = f"http://{url_host}:{args.port}"
    listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, args.port))
        listener.listen(128)
    except OSError as exc:
        listener.close()
        if local_host:
            import httpx
            for attempt in range(20):
                try:
                    with httpx.Client(trust_env=False, timeout=0.5) as client:
                        reply = client.get(url + "/health")
                    info = reply.json()
                    if reply.status_code == 200 and isinstance(info, dict) and info.get("instance") == _instance_id():
                        sys.stderr.write(f"Buddy2api is already running: {url}\n")
                        if not args.no_browser:
                            webbrowser.open(url)
                        return
                    break
                except (httpx.HTTPError, ValueError):
                    time.sleep(0.25)
        ap.error(f"Cannot listen on {url}: {exc}")
    try:
        instance_lock = _lock_database()
    except RuntimeError as exc:
        listener.close()
        ap.error(str(exc))

    db.init_db()

    startup = control_plane.startup_scan()
    sys.stderr.write(f"[startup] discover: {startup}\n")

    accounts = db.list_accounts()
    sys.stderr.write(f"\n")
    sys.stderr.write(f"  Buddy 2 API v{VERSION}\n")
    sys.stderr.write(f"  ========================\n")
    sys.stderr.write(f"  监听: http://{args.host}:{args.port}\n")
    sys.stderr.write(f"  账号: {len(accounts)} 个 ({sum(1 for a in accounts if a['status']=='active')} active)\n")
    sys.stderr.write(f"  通道: {', '.join(providers.enabled_provider_ids())}\n")
    retired = providers.ignored_retired_ids()
    if retired:
        sys.stderr.write("  已下架: QClaw。积分已转到 WorkBuddy，环境变量里的 qclaw 已忽略。\n")
    sys.stderr.write(
        f"  启动导入: {'on' if control_plane.auto_import_enabled() else 'off (CB_GATEWAY_AUTO_IMPORT=1 可打开)'}\n"
    )
    sys.stderr.write(
        "  自动化: "
        f"保活 {'on' if control_plane.keepalive_enabled() else 'off'}"
        f" @{control_plane.keepalive_hour():02d}:00 · "
        f"领取 {'on' if control_plane.auto_checkin_enabled() else 'off'}"
        f" @{control_plane.auto_checkin_hour():02d}:00\n"
    )
    sys.stderr.write(f"  Admin: {'local automatic access' if LOCAL_MODE else 'token required'}\n")
    if ADMIN_TOKEN:
        sys.stderr.write("  Admin Token: configured (hidden)\n")
    sys.stderr.write(f"  ========================\n\n")

    config = uvicorn.Config(app, host=host, port=args.port, log_level=args.log_level, proxy_headers=False)
    server = uvicorn.Server(config)
    bind_runtime_server(server)
    if local_host and not args.no_browser:
        threading.Thread(target=_open_when_ready, args=(server, url), daemon=True).start()
    try:
        if _debugger_replaced_asyncio_run():
            _serve_without_uvicorn_runner(server, listener)
        else:
            server.run(sockets=[listener])
    finally:
        listener.close()
        instance_lock.close()


if __name__ == "__main__":
    main()
