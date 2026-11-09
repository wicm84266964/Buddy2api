"""Cross-channel admin orchestration. Never sends chat; never failovers vendors."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import database as db
import providers
import auth_manager
import resource_sync


async def _gather_limited(accounts: list[dict], operation, limit: int = 2) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(account: dict):
        async with semaphore:
            return await operation(account)

    if not accounts:
        return []
    return list(await asyncio.gather(*(run(account) for account in accounts)))

_PREVIEW_TTL_SEC = 600
_previews: dict[str, dict] = {}
_TOKEN_FIELDS = (
    "access_token",
    "refresh_token",
    "expires_at",
    "refresh_expires_at",
    "session_state",
    "extra",
    "nickname",
    "name",
    "phone",
)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def auto_import_enabled() -> bool:
    return _env_flag("CB_GATEWAY_AUTO_IMPORT", False)


def checkin_gap_seconds() -> float:
    raw = os.environ.get("CB_GATEWAY_CHECKIN_GAP_MS", "800")
    try:
        return max(0, int(raw)) / 1000.0
    except (TypeError, ValueError):
        return 0.8


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_override(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return as_bool(raw, False)


def _saved_hour(env_name: str, setting_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip() != "":
        try:
            return max(0, min(23, int(raw)))
        except ValueError:
            pass
    try:
        return max(0, min(23, int(db.get_setting(setting_name, default))))
    except (TypeError, ValueError):
        return default


def keepalive_enabled() -> bool:
    override = _env_override("CB_GATEWAY_KEEPALIVE")
    if override is not None:
        return override
    return as_bool(db.get_setting("keepalive_enabled", False), False)


def keepalive_hour() -> int:
    return _saved_hour("CB_GATEWAY_KEEPALIVE_HOUR", "keepalive_hour", 22)


def auto_checkin_enabled() -> bool:
    override = _env_override("CB_GATEWAY_AUTO_CHECKIN")
    if override is not None:
        return override
    return as_bool(db.get_setting("auto_checkin_enabled", False), False)


def auto_checkin_hour() -> int:
    return _saved_hour("CB_GATEWAY_AUTO_CHECKIN_HOUR", "auto_checkin_hour", 9)


def automation_due(now: datetime, hour: int) -> bool:
    """Daily jobs fire only near the top of the configured hour."""
    return now.hour == int(hour) and now.minute < 10


def seconds_until_hour(hour: int, now: datetime | None = None) -> float:
    moment = now or datetime.now()
    target = moment.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    if target <= moment:
        target += timedelta(days=1)
    return max(1.0, (target - moment).total_seconds())


def path_hash(path: str) -> str:
    return hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()


def _purge_previews():
    now = time.time()
    dead = [key for key, item in _previews.items() if item["expires"] <= now]
    for key in dead:
        _previews.pop(key, None)


def issue_preview(channel: str, files: list[dict]) -> str:
    _purge_previews()
    token = secrets.token_urlsafe(24)
    paths_by_hash = {}
    for item in files:
        path = item.get("path")
        if not path:
            continue
        paths_by_hash[path_hash(path)] = path
    _previews[token] = {
        "channel": channel,
        "hashes": set(paths_by_hash),
        "paths": paths_by_hash,
        "expires": time.time() + _PREVIEW_TTL_SEC,
    }
    return token


def lookup_preview(token: str, channel: str) -> dict:
    _purge_previews()
    item = _previews.get(token or "")
    if not item or item["channel"] != channel:
        raise ValueError("preview_token is invalid or expired")
    return item


_WINDOWS_DPAPI_CHANNELS = frozenset({"qclaw", "qwenwork"})


def _attach_runtime(payload: dict, channel: str) -> dict:
    runtime = dict(payload.get("runtime") or {})
    in_container = auth_manager._running_in_container()
    runtime["container"] = bool(runtime.get("container") or in_container)
    runtime["host_auth_limited"] = bool(
        runtime["container"] and channel in _WINDOWS_DPAPI_CHANNELS
    )
    payload["runtime"] = runtime
    payload["channel"] = channel
    return payload


def workbuddy_discover(auth_dir: str | None = None, channel: str = "workbuddy") -> dict:
    wanted = "workbuddy-i" if channel == "workbuddy-i" else "workbuddy"
    payload = auth_manager.discover_auth_files(auth_dir)
    files = []
    for item in payload.get("files") or []:
        row = dict(item)
        if auth_manager.workbuddy_channel_for_domain(str(row.get("domain") or "")) != wanted:
            continue
        row["channel"] = wanted
        files.append(row)
    payload["files"] = files
    payload["file_count"] = len(files)
    payload["valid_count"] = sum(1 for item in files if item.get("valid"))
    payload["importable_count"] = sum(
        1 for item in files if item.get("valid") and not item.get("already_imported")
    )
    payload["preview_token"] = issue_preview(wanted, files)
    return _attach_runtime(payload, wanted)


def discover(channel: str | None = None, auth_dir: str | None = None) -> dict:
    if not channel or channel in {"workbuddy", "workbuddy-i"}:
        return workbuddy_discover(auth_dir, channel or "workbuddy")
    if providers.is_retired_channel(channel):
        raise ValueError(providers.retired_message(channel))
    provider = providers.get_provider(channel)
    if provider is None:
        raise ValueError(f"Channel '{channel}' is not enabled")
    discover_fn = getattr(provider, "discover", None)
    if discover_fn is None:
        raise ValueError(f"Channel '{channel}' does not support discover")
    payload = discover_fn()
    if not isinstance(payload, dict):
        payload = {"files": [], "dirs": []}
    files = payload.get("files") or []
    payload["preview_token"] = issue_preview(channel, files)
    return _attach_runtime(payload, channel)


def _allowed_roots_workbuddy(auth_dir: str | None) -> list[Path]:
    return [path.resolve() for path in auth_manager.candidate_auth_dirs(auth_dir)]


def _is_under(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _upsert_workbuddy(parsed: dict, auth_path: str) -> str:
    parsed = dict(parsed)
    parsed["provider"] = auth_manager.workbuddy_channel_for_domain(str(parsed.get("domain") or ""))
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = auth_path
    parsed["extra"] = extra
    uid = str(parsed.get("uid") or "")
    if uid:
        rows = db.list_accounts(provider="workbuddy") + db.list_accounts(provider="workbuddy-i")
        for row in rows:
            if str(row.get("uid") or "") == uid:
                patch = {key: parsed[key] for key in _TOKEN_FIELDS if key in parsed}
                db.update_account(row["id"], patch)
                return "updated"
    db.add_account(parsed)
    return "imported"


def import_workbuddy(paths: list[str], preview: dict, auth_dir: str | None) -> dict:
    roots = _allowed_roots_workbuddy(auth_dir)
    result = {"imported": 0, "updated": 0, "skipped": 0, "errors": []}
    hashes = preview.get("hashes") or set()
    for raw in paths:
        path = Path(raw)
        digest = path_hash(str(path))
        if digest not in hashes:
            result["errors"].append({"path": raw, "error": "path is not in the current preview"})
            result["skipped"] += 1
            continue
        if not _is_under(path, roots):
            result["errors"].append({"path": raw, "error": "path is outside WorkBuddy auth dirs"})
            result["skipped"] += 1
            continue
        parsed = auth_manager.parse_auth_file(path)
        if not parsed:
            result["skipped"] += 1
            continue
        action = _upsert_workbuddy(parsed, str(path.resolve()))
        result[action] += 1
    return result


def import_channel(channel: str, preview_token: str, paths: list[str] | None, auth_dir: str | None = None) -> dict:
    preview = lookup_preview(preview_token, channel)
    if not paths:
        paths = list((preview.get("paths") or {}).values())
    if channel in {"workbuddy", "workbuddy-i"}:
        return import_workbuddy(paths, preview, auth_dir)
    if providers.is_retired_channel(channel):
        raise ValueError(providers.retired_message(channel))
    provider = providers.get_provider(channel)
    if provider is None:
        raise ValueError(f"Channel '{channel}' is not enabled")
    import_path = getattr(provider, "import_path", None)
    parse_credentials = getattr(provider, "parse_credentials", None)
    upsert = getattr(provider, "upsert_account", None)
    if import_path is None:
        raise ValueError(f"Channel '{channel}' does not support import")
    if not paths:
        discovered = discover(channel)
        paths = [item["path"] for item in discovered.get("files") or [] if item.get("valid")]
    result = {"imported": 0, "updated": 0, "skipped": 0, "errors": []}
    for raw in paths:
        digest = path_hash(raw)
        if digest not in (preview.get("hashes") or set()):
            result["errors"].append({"path": raw, "error": "path is not in the current preview"})
            result["skipped"] += 1
            continue
        try:
            parsed = import_path(raw)
            if upsert:
                info = upsert(parsed)
            elif channel == "qclaw":
                from providers.qclaw.store import upsert_account as qclaw_upsert

                info = qclaw_upsert(parsed)
            else:
                uid = str(parsed.get("uid") or "")
                info = {"updated": False}
                if uid:
                    for row in db.list_accounts(provider=channel):
                        if str(row.get("uid") or "") == uid:
                            patch = {key: parsed[key] for key in _TOKEN_FIELDS if key in parsed}
                            db.update_account(row["id"], patch)
                            info = {"updated": True}
                            break
                if not info.get("updated"):
                    db.add_account({**parsed, "provider": channel})
            if info.get("updated"):
                result["updated"] += 1
            else:
                result["imported"] += 1
        except Exception as exc:
            result["errors"].append({"path": raw, "error": str(exc)[:200]})
            result["skipped"] += 1
    return result


def startup_scan() -> dict:
    summary = {"auto_import": auto_import_enabled(), "channels": []}
    for channel in providers.enabled_provider_ids():
        try:
            preview = discover(channel)
        except Exception as exc:
            summary["channels"].append({"channel": channel, "error": str(exc)[:200]})
            continue
        files = preview.get("files") or []
        valid = sum(1 for item in files if item.get("valid"))
        entry = {
            "channel": channel,
            "dirs": preview.get("dirs") or [],
            "file_count": preview.get("file_count") or len(files),
            "valid_count": preview.get("valid_count") or valid,
        }
        if auto_import_enabled():
            imported = import_channel(channel, preview.get("preview_token") or "", None)
            entry["import"] = imported
        summary["channels"].append(entry)
    return summary


async def _channel_accounts(channel: str, status: str = "active") -> list[dict]:
    rows = [
        account
        for account in db.list_accounts(provider=channel)
        if not status or account.get("status") == status
    ]
    rows.sort(key=lambda item: int(item.get("id") or 0))
    return rows


_DASHBOARD_QUOTA_CACHE_SEC = 300
_QUOTA_SNAPSHOT_TTL_SEC = 300
_quota_snapshots: dict[tuple[str, int], tuple[float, object]] = {}


def _snapshot_ok(snapshot) -> bool:
    return bool(getattr(snapshot, "ok", None) if not isinstance(snapshot, dict) else snapshot.get("ok"))


def _mark_snapshot_cached(snapshot) -> None:
    if isinstance(snapshot, dict):
        snapshot["_from_cache"] = True
    else:
        try:
            snapshot._from_cache = True
        except Exception:
            pass


def _snapshot_from_cache(snapshot) -> bool:
    if isinstance(snapshot, dict):
        return bool(snapshot.get("_from_cache"))
    return bool(getattr(snapshot, "_from_cache", False))


async def _fetch_quota_cached(channel: str, account: dict, fetch_quota, force: bool):
    """Serve a channel's quota snapshot from a short TTL cache.

    Opening the Dashboard used to hit every provider's quota API on every load
    (only the workbuddy family had a freshness cache). Cache the measured
    snapshot per channel+account; ``force`` (the page's refresh button) always
    refetches, and failures are never cached so a flaky upstream retries on the
    next look.
    """
    account_id = int(account.get("id") or 0)
    if not force and account_id:
        hit = _quota_snapshots.get((channel, account_id))
        if hit is not None and time.time() - hit[0] < _QUOTA_SNAPSHOT_TTL_SEC:
            _mark_snapshot_cached(hit[1])
            return hit[1]
    snapshot = await fetch_quota(account)
    if account_id and _snapshot_ok(snapshot):
        _quota_snapshots[(channel, account_id)] = (time.time(), snapshot)
    return snapshot


async def _summarize_workbuddy_credit(provider, accounts: list[dict], force: bool) -> tuple[dict, list]:
    resources = []
    if accounts:
        resources = await _gather_limited(
            accounts,
            lambda account: auth_manager.fetch_account_resources(
                account,
                force=force,
                max_age_seconds=0 if force else _DASHBOARD_QUOTA_CACHE_SEC,
            ),
            limit=2,
        )
    ok = [row for row in resources if row.get("ok")]
    remaining = round(sum(float(row.get("total_dosage") or row.get("available_total") or 0) for row in ok), 4)
    row = {
        "id": getattr(provider, "id", "workbuddy"),
        "display_name": getattr(provider, "display_name", "workbuddy"),
        "unit": "credit",
        "remaining": remaining,
        "ok": True,
        "accounts": len(accounts),
        "ok_accounts": len(ok),
        "unsupported": False,
        "expiring_7d_total": round(sum(float(item.get("expiring_7d_total") or 0) for item in ok), 4),
        "expiring_30d_total": round(sum(float(item.get("expiring_30d_total") or 0) for item in ok), 4),
        "package_count": sum(int(item.get("package_count") or 0) for item in ok),
    }
    return row, resources


def _cache_quota_reading(account, remaining) -> None:
    """Best-effort: cache a measured quota balance so routing can gate on it.

    A dashboard refresh is a real observation; dropping it on the floor is how a
    dry account kept its slot in the pool. Never let a cache write break the
    report, and never write a figure that was not measured.
    """
    if not isinstance(account, dict) or account.get("id") in (None, ""):
        return
    try:
        resource_sync.store_quota_reading(account, remaining=float(remaining), source="dashboard-quota-check")
    except Exception:
        pass


async def _summarize_quota_channel(channel: str, provider, accounts: list[dict], force: bool = False) -> dict:
    fetch_quota = getattr(provider, "fetch_quota", None) if provider else None
    if fetch_quota is None:
        return {
            "id": channel,
            "display_name": getattr(provider, "display_name", channel) if provider else channel,
            "unit": "unknown",
            "remaining": None,
            "ok": True,
            "accounts": len(accounts),
            "unsupported": True,
            "message": "quota API not available",
        }
    by_id = {
        str(account.get("id")): account
        for account in accounts
        if isinstance(account, dict) and account.get("id") not in (None, "")
    }
    snapshots = await _gather_limited(
        accounts,
        lambda account: _fetch_quota_cached(channel, account, fetch_quota, force),
        limit=2,
    )
    remaining_values = []
    used_values = []
    limit_values = []
    ok_count = 0
    unsupported = False
    message = ""
    channel_unit = "unknown"
    for snapshot in snapshots:
        unit = str(
            (getattr(snapshot, "unit", None) if not isinstance(snapshot, dict) else snapshot.get("unit"))
            or "unknown"
        )
        ok = bool(getattr(snapshot, "ok", None) if not isinstance(snapshot, dict) else snapshot.get("ok"))
        snap_unsupported = bool(
            getattr(snapshot, "unsupported", False) if not isinstance(snapshot, dict) else snapshot.get("unsupported")
        )
        extra = getattr(snapshot, "extra", None) if not isinstance(snapshot, dict) else snapshot.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        if channel_unit == "unknown" and unit in {"credit", "token"}:
            channel_unit = unit
        if snap_unsupported:
            unsupported = True
            message = (
                getattr(snapshot, "message", "") if not isinstance(snapshot, dict) else snapshot.get("message") or ""
            )
        if ok:
            ok_count += 1
        value = getattr(snapshot, "remaining", None) if not isinstance(snapshot, dict) else snapshot.get("remaining")
        if value is not None and unit == "credit" and ok and not snap_unsupported and not _snapshot_from_cache(snapshot):
            # Let routing see what this dashboard just measured. Opening the
            # overview is often the only quota check that ever runs, and the
            # gate reads nothing but the cache. Best effort by design. A cache
            # hit is not a measurement; re-stamping it would fake freshness.
            snap_account_id = (
                getattr(snapshot, "account_id", None)
                if not isinstance(snapshot, dict)
                else snapshot.get("account_id")
            )
            _cache_quota_reading(by_id.get(str(snap_account_id)), value)
        if unit == channel_unit and value is not None and not snap_unsupported:
            remaining_values.append(float(value))
        if unit == "token" and not snap_unsupported:
            if extra.get("used") is not None:
                used_values.append(float(extra["used"]))
            if extra.get("limit") is not None:
                limit_values.append(float(extra["limit"]))
    remaining = round(sum(remaining_values), 4) if remaining_values else None
    if channel_unit == "unknown":
        channel_unit = "credit" if channel != "qclaw" else "token"
    return {
        "id": channel,
        "display_name": getattr(provider, "display_name", channel),
        "unit": channel_unit,
        "remaining": remaining,
        "used": round(sum(used_values), 4) if used_values else None,
        "limit": round(sum(limit_values), 4) if limit_values else None,
        "ok": True,
        "accounts": len(accounts),
        "ok_accounts": ok_count,
        "unsupported": remaining is None and not used_values and not limit_values,
        "message": message or ("no quota number" if remaining is None else ""),
    }


async def credit_summary(force: bool = False) -> dict:
    async def one_channel(channel: str):
        provider = providers.get_provider(channel)
        accounts = await _channel_accounts(channel)
        if _workbuddy_family(channel):
            row, resources = await _summarize_workbuddy_credit(provider, accounts, force)
            return row, resources
        return await _summarize_quota_channel(channel, provider, accounts, force), []

    gathered = await asyncio.gather(
        *(one_channel(channel) for channel in providers.enabled_provider_ids())
    )
    channels = []
    workbuddy_resources = []
    for row, resources in gathered:
        channels.append(row)
        if _workbuddy_family(str(row.get("id") or "")):
            for item in resources:
                if isinstance(item, dict) and not item.get("channel"):
                    item["channel"] = row.get("id")
            workbuddy_resources.extend(resources)
    now_ts = int(time.time())
    ok_resources = [row for row in workbuddy_resources if row.get("ok")]
    stale_count = sum(1 for row in workbuddy_resources if row.get("stale"))
    failed_count = sum(1 for row in workbuddy_resources if not row.get("ok"))
    low_accounts = []
    expiring_accounts = []
    for row in workbuddy_resources:
        balance = float(row.get("total_dosage") or row.get("available_total") or 0)
        item = {
            "account_id": row.get("account_id"),
            "account_name": row.get("account_name"),
            "channel": row.get("channel") or "workbuddy",
            "balance": round(balance, 4),
            "expiring_30d_total": round(float(row.get("expiring_30d_total") or 0), 4),
            "next_expire_time": row.get("next_expire_time") or "",
            "next_expire_days": row.get("next_expire_days"),
            "ok": bool(row.get("ok")),
            "stale": bool(row.get("stale")),
            "age_seconds": int(row.get("age_seconds") or 0),
        }
        if row.get("ok") and balance <= 300:
            low_accounts.append(item)
        if row.get("ok") and float(row.get("expiring_30d_total") or 0) > 0:
            expiring_accounts.append(item)
    low_accounts.sort(key=lambda item: (item["balance"], item["account_id"] or 0))
    expiring_accounts.sort(
        key=lambda item: (item["next_expire_days"] if item["next_expire_days"] is not None else 9999, -item["expiring_30d_total"])
    )
    active_total = sum(item.get("accounts") or 0 for item in channels)
    return {
        "ok": failed_count == 0,
        "updated_at": now_ts,
        "active_accounts": active_total,
        "resource_accounts": len(workbuddy_resources),
        "ok_accounts": len(ok_resources),
        "failed_accounts": failed_count,
        "stale_accounts": stale_count,
        "total_balance": None,
        "channels": channels,
        "expiring_7d_total": round(sum(float(row.get("expiring_7d_total") or 0) for row in ok_resources), 4),
        "expiring_30d_total": round(sum(float(row.get("expiring_30d_total") or 0) for row in ok_resources), 4),
        "package_count": sum(int(row.get("package_count") or 0) for row in ok_resources),
        "low_accounts": low_accounts[:8],
        "expiring_accounts": expiring_accounts[:8],
        "accounts": workbuddy_resources,
    }


def _checkin_channel_error(account: dict, channel: str, message: str) -> dict:
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": False,
        "claimed": False,
        "already_claimed": False,
        "status_code": 400,
        "message": message,
        "credit": 0,
        "channel": channel,
    }


def _workbuddy_family(channel: str) -> bool:
    return channel in {"workbuddy", "workbuddy-i"}


def _store_checkin_expiry(account: dict, result) -> None:
    """Cache a deadline a check-in response revealed, best effort.

    Providers know the expiry ("valid_to", "benefit.validity.days") but only
    report it in their result row; this is the one place that turns it into the
    resource cache routing reads. A cache write must never fail a check-in, and
    an absent deadline must not be invented.
    """
    if not isinstance(result, dict):
        return
    expires = result.get("next_expire_ts")
    if not expires:
        return
    try:
        resource_sync.store_quota_expiry(
            account,
            expires_at=expires,
            source=str(result.get("expire_source") or ""),
        )
    except Exception:
        pass


async def checkin_status_one(account: dict, force: bool = False) -> dict:
    channel = str(account.get("provider") or "workbuddy")
    if not _workbuddy_family(channel):
        provider = providers.get_provider(channel)
        if provider is None:
            return _checkin_channel_error(account, channel, f"Channel '{channel}' is not enabled")
        fetch_fn = getattr(provider, "fetch_checkin", None)
        if fetch_fn is None:
            return _checkin_channel_error(account, channel, f"Channel '{channel}' does not support check-in")
        result = await fetch_fn(account, force=force)
        if isinstance(result, dict):
            result["channel"] = channel
            _store_checkin_expiry(account, result)
        return result
    return await auth_manager.fetch_checkin_status(account, force=force)


async def claim_one(account: dict) -> dict:
    channel = str(account.get("provider") or "workbuddy")
    if not _workbuddy_family(channel):
        provider = providers.get_provider(channel)
        if provider is None:
            return _checkin_channel_error(account, channel, f"Channel '{channel}' is not enabled")
        claim_fn = getattr(provider, "claim_checkin", None)
        if claim_fn is None:
            return _checkin_channel_error(account, channel, f"Channel '{channel}' does not support check-in")
        result = await claim_fn(account)
        if isinstance(result, dict):
            result["channel"] = channel
            _store_checkin_expiry(account, result)
        return result
    result = await auth_manager.claim_daily_checkin(account)
    if result.get("ok"):
        result["resources"] = await auth_manager.fetch_account_resources(account, force=True)
    return result


async def checkin_status_all(force: bool = False) -> dict:
    results = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        if provider is None or not getattr(provider, "checkin_supported", False):
            continue
        accounts = await _channel_accounts(channel)
        if not accounts:
            continue
        fetch_checkin = getattr(provider, "fetch_checkin", None)
        if fetch_checkin:
            chunk = await _gather_limited(
                accounts,
                lambda account, fn=fetch_checkin: fn(account, force=force),
                limit=2,
            )
        else:
            chunk = await _gather_limited(
                accounts,
                lambda account: auth_manager.fetch_checkin_status(account, force=force),
                limit=2,
            )
        for account, item in zip(accounts, chunk):
            if isinstance(item, dict):
                item["channel"] = channel
                # _gather_limited preserves the account order, so rows line up.
                _store_checkin_expiry(account, item)
        results.extend(chunk)
    return {
        "total": len(results),
        "ok": sum(1 for row in results if row.get("ok")),
        "claimed": sum(1 for row in results if row.get("claimed")),
        "already_claimed": sum(1 for row in results if row.get("already_claimed") or row.get("today_checked_in")),
        "available": sum(
            1
            for row in results
            if row.get("ok")
            and not row.get("no_campaign")
            and not (row.get("already_claimed") or row.get("today_checked_in"))
        ),
        "failed": sum(1 for row in results if not row.get("ok")),
        "stale": sum(1 for row in results if row.get("stale")),
        "results": results,
    }


def _public_text(value, limit: int = 240) -> str:
    return db.public_automation_text(value, limit)


def _event_parts(row: dict) -> tuple[int | None, str, str, int | None]:
    raw_id = row.get("account_id")
    try:
        account_id = int(raw_id) if raw_id not in (None, "") else None
    except (TypeError, ValueError):
        account_id = None
    raw_status = row.get("status_code")
    try:
        status_code = int(raw_status) if raw_status not in (None, "") else None
    except (TypeError, ValueError):
        status_code = None
    name = _public_text(row.get("account_name") or row.get("nickname") or row.get("name") or "", 80)
    channel = _public_text(row.get("channel") or row.get("provider") or "", 32)
    return account_id, name, channel, status_code


def _checkin_outcome(row: dict) -> str:
    if row.get("skipped"):
        return "skipped"
    if not row.get("ok"):
        return "failed"
    if row.get("claimed"):
        return "claimed"
    if row.get("already_claimed") or row.get("today_checked_in"):
        return "already"
    if row.get("no_campaign"):
        return "no_campaign"
    return "ok"


def _keepalive_outcome(row: dict) -> str:
    if row.get("skipped"):
        return "skipped"
    if row.get("ok"):
        return "refreshed"
    return "failed"


def _record_automation(job: str, source: str, rows: list[dict], outcome_for) -> None:
    run_id = secrets.token_hex(8)
    now = int(time.time())
    events = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        account_id, name, channel, status_code = _event_parts(row)
        outcome = outcome_for(row)
        message = _public_text(row.get("message") or "")
        events.append(
            {
                "created_at": now,
                "source": source or "manual",
                "job": job,
                "run_id": run_id,
                "account_id": account_id,
                "account_name": name,
                "channel": channel,
                "outcome": outcome,
                "ok": outcome in {"refreshed", "claimed", "already", "ok"},
                "status_code": status_code,
                "message": message,
            }
        )
        if outcome in {"failed", "error"}:
            sys.stderr.write(
                f"[{job}] failed source={source or 'manual'} channel={channel or '-'} "
                f"account={name or '-'} status={status_code or 0} message={message or '-'}\n"
            )
    if not events:
        return
    try:
        db.add_automation_events(events)
    except Exception as exc:
        sys.stderr.write(f"[{job}] log failed: {type(exc).__name__}\n")


def record_automation_error(job: str, source: str, exc: Exception) -> None:
    message = _public_text(exc) or type(exc).__name__
    _record_automation(
        job,
        source,
        [{"ok": False, "message": message, "outcome_error": True}],
        lambda _row: "error",
    )
    sys.stderr.write(f"[{job}] {source} failed: {type(exc).__name__}\n")


async def checkin_all(channel_filter: list[str] | None = None, source: str = "manual") -> dict:
    wanted = set(channel_filter) if channel_filter else None
    results = []
    gap = checkin_gap_seconds()
    skipped = []
    first = True
    for channel in providers.enabled_provider_ids():
        if wanted is not None and channel not in wanted:
            continue
        provider = providers.get_provider(channel)
        if provider is None:
            continue
        if not getattr(provider, "checkin_supported", False):
            skipped.append({"channel": channel, "reason": "checkin_unsupported"})
            continue
        accounts = await _channel_accounts(channel)
        for account in accounts:
            if not first and gap:
                await asyncio.sleep(gap)
            first = False
            try:
                result = await claim_one(account)
            except Exception as exc:
                result = {
                    "ok": False,
                    "account_id": account.get("id"),
                    "account_name": _account_label(account),
                    "status_code": 0,
                    "message": _public_text(exc) or type(exc).__name__,
                }
            if not isinstance(result, dict):
                result = {
                    "ok": False,
                    "account_id": account.get("id"),
                    "account_name": _account_label(account),
                    "message": "领取结果无效",
                }
            result["channel"] = channel
            results.append(result)
    _record_automation("checkin", source, results, _checkin_outcome)
    wb_credit = round(
        sum(float(row.get("credit") or 0) for row in results if row.get("claimed") and row.get("channel") == "workbuddy"),
        4,
    )
    return {
        "total": len(results),
        "claimed": sum(1 for row in results if row.get("claimed")),
        "already_claimed": sum(1 for row in results if row.get("already_claimed")),
        "failed": sum(1 for row in results if not row.get("ok")),
        "credit": wb_credit,
        "credit_deprecated": True,
        "skipped": skipped,
        "results": results,
    }


def _account_label(account: dict) -> str:
    return account.get("nickname") or account.get("name") or str(account.get("id"))


async def refresh_one(account: dict, *, allow_inactive: bool = True) -> dict:
    channel = str(account.get("provider") or "workbuddy")
    base = {
        "account_id": account.get("id"),
        "account_name": _account_label(account),
        "provider": channel,
    }
    if not allow_inactive and account.get("status") == "inactive":
        return {**base, "ok": False, "skipped": True, "reason": "inactive", "message": "账号已禁用，未刷新", "status": "inactive"}
    if _workbuddy_family(channel):
        result = await auth_manager.refresh_token_result(account)
        result["status_text"] = "ok" if result.get("ok") else "failed"
        return result
    provider = providers.get_provider(channel)
    if provider is None:
        return {
            **base,
            "ok": False,
            "skipped": True,
            "reason": "disabled_channel",
            "message": f"通道 {channel} 未启用",
            "status": account.get("status") or "",
        }
    refresh = getattr(provider, "refresh", None)
    if refresh is None:
        return {
            **base,
            "ok": False,
            "skipped": True,
            "reason": "unsupported",
            "message": f"通道 {channel} 不支持保活",
            "status": account.get("status") or "",
        }
    try:
        await refresh(account)
    except Exception as exc:
        text = " ".join(str(exc).split())[:180] or "刷新失败"
        return {**base, "ok": False, "reason": "rejected", "message": text, "status": account.get("status") or ""}
    fresh = db.get_account(account["id"]) or account
    return {**base, "ok": True, "reason": "refreshed", "message": "已续期", "status": fresh.get("status") or "active"}


_keepalive_guard = asyncio.Lock()
_keepalive_running = False
_keepalive_task: asyncio.Task | None = None
_keepalive_stop: asyncio.Event | None = None


def _job_busy(source: str) -> dict:
    return {
        "ok": False,
        "running": True,
        "source": source,
        "message": "已有保活或领取任务在进行",
        "total": 0,
        "refreshed": 0,
        "failed": 0,
        "skipped": 0,
        "results": [],
    }


async def _begin_job(source: str) -> dict | None:
    global _keepalive_running
    async with _keepalive_guard:
        if _keepalive_running:
            return _job_busy(source)
        _keepalive_running = True
        return None


async def _end_job() -> None:
    global _keepalive_running
    async with _keepalive_guard:
        _keepalive_running = False


async def keepalive_all(
    channel_filter: list[str] | None = None,
    source: str = "manual",
    *,
    claim: bool = False,
) -> dict:
    busy = await _begin_job(source)
    if busy:
        _record_automation(
            "keepalive",
            source,
            [{"ok": False, "message": "已有保活或领取任务在进行，本次未执行"}],
            _keepalive_outcome,
        )
        return busy
    try:
        wanted = set(channel_filter) if channel_filter else None
        results = []
        gap = checkin_gap_seconds()
        first = True
        for channel in providers.enabled_provider_ids():
            if wanted is not None and channel not in wanted:
                continue
            accounts = await _channel_accounts(channel, status="")
            for account in accounts:
                if not first and gap:
                    await asyncio.sleep(gap)
                first = False
                try:
                    result = await refresh_one(account, allow_inactive=False)
                except Exception as exc:
                    result = {
                        "ok": False,
                        "account_id": account.get("id"),
                        "account_name": _account_label(account),
                        "provider": channel,
                        "status_code": 0,
                        "message": _public_text(exc) or type(exc).__name__,
                    }
                if not isinstance(result, dict):
                    result = {
                        "ok": False,
                        "account_id": account.get("id"),
                        "account_name": _account_label(account),
                        "message": "续期结果无效",
                    }
                result["channel"] = channel
                result["status_text"] = "ok" if result.get("ok") else "failed"
                results.append(result)
        refreshed = sum(1 for row in results if row.get("ok"))
        skipped = sum(1 for row in results if row.get("skipped"))
        failed = sum(1 for row in results if not row.get("ok") and not row.get("skipped"))
        _record_automation("keepalive", source, results, _keepalive_outcome)
        claim_summary = None
        if claim:
            claim_summary = await checkin_all(channel_filter, source=source)
        claim_failed = int((claim_summary or {}).get("failed") or 0)
        claimed = int((claim_summary or {}).get("claimed") or 0)
        already = int((claim_summary or {}).get("already_claimed") or 0)
        if claim_summary:
            message = f"已续期 {refreshed} · 领取 {claimed} · 已领 {already} · 失败 {failed + claim_failed}"
        else:
            message = f"已续期 {refreshed} · 失败 {failed} · 跳过 {skipped}"
        return {
            "ok": failed == 0 and claim_failed == 0,
            "running": False,
            "source": source,
            "message": message,
            "total": len(results),
            "refreshed": refreshed,
            "failed": failed,
            "skipped": skipped,
            "claimed": claimed,
            "already_claimed": already,
            "claim_failed": claim_failed,
            "claim": claim_summary,
            "results": results,
        }
    finally:
        await _end_job()


def _log_keepalive(summary: dict) -> None:
    sys.stderr.write(
        "[keepalive] source={source} refreshed={refreshed} failed={failed} skipped={skipped}\n".format(
            source=summary.get("source") or "manual",
            refreshed=summary.get("refreshed") or 0,
            failed=summary.get("failed") or 0,
            skipped=summary.get("skipped") or 0,
        )
    )


async def _run_keepalive_safe(source: str) -> None:
    if not keepalive_enabled():
        return
    try:
        _log_keepalive(await keepalive_all(source=source, claim=False))
    except Exception as exc:
        record_automation_error("keepalive", source, exc)


async def _run_checkin_safe(source: str) -> None:
    if not auto_checkin_enabled():
        return
    busy = await _begin_job(source)
    if busy:
        _record_automation(
            "checkin",
            source,
            [{"ok": False, "message": "已有保活或领取任务在进行，本次未执行"}],
            _checkin_outcome,
        )
        return
    try:
        summary = await checkin_all(source=source)
        sys.stderr.write(
            "[checkin] source={source} claimed={claimed} already={already} failed={failed}\n".format(
                source=source,
                claimed=summary.get("claimed") or 0,
                already=summary.get("already_claimed") or 0,
                failed=summary.get("failed") or 0,
            )
        )
    except Exception as exc:
        record_automation_error("checkin", source, exc)
    finally:
        await _end_job()


async def keepalive_loop(stop: asyncio.Event) -> None:
    """Idle until a saved automation hour. Nothing runs unless that switch is on."""
    db.init_db()
    fired: set[str] = set()
    while not stop.is_set():
        now = datetime.now()
        today = now.date().isoformat()
        fired = {item for item in fired if item.startswith(today)}
        if keepalive_enabled() and automation_due(now, keepalive_hour()):
            slot = f"{today}:keepalive:{keepalive_hour()}"
            if slot not in fired:
                fired.add(slot)
                await _run_keepalive_safe("schedule")
        if auto_checkin_enabled() and automation_due(now, auto_checkin_hour()):
            slot = f"{today}:checkin:{auto_checkin_hour()}"
            if slot not in fired:
                fired.add(slot)
                await _run_checkin_safe("schedule")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass


def start_keepalive() -> None:
    global _keepalive_task, _keepalive_stop
    if _keepalive_task and not _keepalive_task.done():
        return
    _keepalive_stop = asyncio.Event()
    _keepalive_task = asyncio.create_task(keepalive_loop(_keepalive_stop), name="keepalive")


async def stop_keepalive() -> None:
    global _keepalive_task, _keepalive_stop
    if _keepalive_stop is not None:
        _keepalive_stop.set()
    task = _keepalive_task
    _keepalive_task = None
    _keepalive_stop = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
