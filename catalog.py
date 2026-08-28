"""Per-channel supplier model catalogs.

Fetch+parse of each source's list is separate from persist and from chat I/O.
WorkBuddy and QwenWork have no supplier-list HTTP; they stay on the existing
static/admin catalog and are reported as fallback.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Awaitable, Callable

import database as db

CATALOG_SETTING = "channel_catalogs"
REFRESH_SETTING = "channel_catalog_refresh"

Fetcher = Callable[[dict], Awaitable[list[dict]]]


def _load_map(key: str) -> dict:
    try:
        value = db.get_setting(key, {}) or {}
    except sqlite3.OperationalError:
        return {}
    return value if isinstance(value, dict) else {}


def stored_catalog(channel: str) -> list[dict] | None:
    items = _load_map(CATALOG_SETTING).get(channel)
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, dict) and item.get("id")]
    return None


def save_catalog(channel: str, models: list[dict]) -> None:
    catalogs = _load_map(CATALOG_SETTING)
    catalogs[channel] = models
    db.set_setting(CATALOG_SETTING, catalogs)


def normalize_models(rows: Any) -> list[dict]:
    if not isinstance(rows, list):
        return []
    models: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
            name = mid
            description = ""
        elif isinstance(row, dict):
            mid = str(
                row.get("id")
                or row.get("model_id")
                or row.get("model_name")
                or row.get("name")
                or ""
            ).strip()
            name = str(row.get("name") or row.get("display_id") or row.get("display_name") or mid)
            description = str(row.get("description") or "")
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item = {"id": mid, "name": name or mid}
        if description:
            item["description"] = description
        models.append(item)
    return models


def models_for(channel: str, fallback: list[dict]) -> list[dict]:
    stored = stored_catalog(channel)
    if stored:
        return stored
    return list(fallback)


def workbuddy_fallback_models() -> list[dict]:
    import proxy

    try:
        models = db.get_setting("models", proxy.DEFAULT_MODELS)
    except sqlite3.OperationalError:
        return list(proxy.DEFAULT_MODELS)
    if isinstance(models, list) and models:
        return models
    return list(proxy.DEFAULT_MODELS)


def _fallback_models(channel: str, provider) -> list[dict]:
    if channel == "workbuddy":
        return workbuddy_fallback_models()
    if provider is not None:
        stored = stored_catalog(channel)
        if stored:
            return stored
        try:
            return list(provider.list_models())
        except Exception:
            pass
    return []


def _status_row(
    channel: str,
    *,
    mode: str,
    models: list[dict],
    message: str = "",
    display_name: str = "",
) -> dict:
    return {
        "channel": channel,
        "display_name": display_name or channel,
        "mode": mode,
        "message": message,
        "count": len(models),
        "models": models,
        "updated_at": int(time.time()),
    }


async def _pick_account(provider) -> dict | None:
    if provider is None:
        return None
    picker = getattr(provider, "pick_account_with_fallback", None)
    if picker is None:
        return None
    return await picker()


async def _fetch_qclaw(account: dict) -> list[dict]:
    from providers.qclaw.jprx import fetch_supplier_models

    return await fetch_supplier_models(account)


async def _fetch_traework(account: dict) -> list[dict]:
    from providers.traework.models import fetch_supplier_models

    return await fetch_supplier_models(account)


LIVE_FETCHERS: dict[str, Fetcher] = {
    "qclaw": _fetch_qclaw,
    "traework": _fetch_traework,
}


async def refresh_one(channel: str) -> dict:
    import providers

    provider = providers.get_provider(channel)
    display_name = getattr(provider, "display_name", channel) if provider else channel
    fallback = _fallback_models(channel, provider)
    fetcher = LIVE_FETCHERS.get(channel)
    if fetcher is None:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="no supplier-list API",
            display_name=display_name,
        )
    account = await _pick_account(provider)
    if not account:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="no usable account",
            display_name=display_name,
        )
    try:
        fetched = normalize_models(await fetcher(account))
    except Exception as exc:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message=str(exc)[:240],
            display_name=display_name,
        )
    if not fetched:
        return _status_row(
            channel,
            mode="fallback",
            models=fallback,
            message="empty supplier list",
            display_name=display_name,
        )
    save_catalog(channel, fetched)
    return _status_row(
        channel,
        mode="live",
        models=fetched,
        message="",
        display_name=display_name,
    )


async def refresh_supplier_catalogs() -> dict:
    import providers

    sources = []
    for channel in providers.enabled_provider_ids():
        sources.append(await refresh_one(channel))
    db.set_setting(
        REFRESH_SETTING,
        {
            item["channel"]: {
                "mode": item["mode"],
                "message": item["message"],
                "count": item["count"],
                "updated_at": item["updated_at"],
            }
            for item in sources
        },
    )
    return {"sources": sources}


def catalog_snapshot() -> dict:
    import providers

    refresh = _load_map(REFRESH_SETTING)
    sources = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        if channel == "workbuddy":
            models = workbuddy_fallback_models()
        elif provider is not None:
            models = list(provider.list_models())
        else:
            models = []
        meta = refresh.get(channel) if isinstance(refresh.get(channel), dict) else {}
        sources.append(
            {
                "channel": channel,
                "display_name": getattr(provider, "display_name", channel) if provider else channel,
                "mode": meta.get("mode") or ("fallback" if channel not in LIVE_FETCHERS else "static"),
                "message": meta.get("message") or "",
                "count": len(models),
                "models": models,
                "updated_at": meta.get("updated_at"),
            }
        )
    return {"sources": sources}
