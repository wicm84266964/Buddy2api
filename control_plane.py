"""Cross-channel admin orchestration. Never sends chat; never failovers vendors."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from pathlib import Path

import database as db
import providers
import auth_manager


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


def workbuddy_discover(auth_dir: str | None = None) -> dict:
    payload = auth_manager.discover_auth_files(auth_dir)
    files = []
    for item in payload.get("files") or []:
        row = dict(item)
        row["channel"] = "workbuddy"
        files.append(row)
    payload["files"] = files
    payload["preview_token"] = issue_preview("workbuddy", files)
    return _attach_runtime(payload, "workbuddy")


def discover(channel: str | None = None, auth_dir: str | None = None) -> dict:
    if not channel or channel == "workbuddy":
        return workbuddy_discover(auth_dir)
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
    parsed["provider"] = "workbuddy"
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = auth_path
    parsed["extra"] = extra
    uid = str(parsed.get("uid") or "")
    if uid:
        for row in db.list_accounts(provider="workbuddy"):
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
    if channel == "workbuddy":
        return import_workbuddy(paths, preview, auth_dir)
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


async def credit_summary(force: bool = False) -> dict:
    channels = []
    workbuddy_resources = []
    for channel in providers.enabled_provider_ids():
        provider = providers.get_provider(channel)
        accounts = await _channel_accounts(channel)
        if channel == "workbuddy":
            resources = []
            if accounts:
                resources = await _gather_limited(
                    accounts,
                    lambda account: auth_manager.fetch_account_resources(account, force=force),
                    limit=2,
                )
            workbuddy_resources = resources
            ok = [row for row in resources if row.get("ok")]
            remaining = round(sum(float(row.get("total_dosage") or row.get("available_total") or 0) for row in ok), 4)
            channels.append(
                {
                    "id": channel,
                    "display_name": getattr(provider, "display_name", channel),
                    "unit": "credit",
                    "remaining": remaining,
                    "ok": True,
                    "accounts": len(accounts),
                    "ok_accounts": len(ok),
                    "unsupported": False,
                    "expiring_7d_total": round(sum(float(row.get("expiring_7d_total") or 0) for row in ok), 4),
                    "expiring_30d_total": round(sum(float(row.get("expiring_30d_total") or 0) for row in ok), 4),
                    "package_count": sum(int(row.get("package_count") or 0) for row in ok),
                }
            )
            continue
        fetch_quota = getattr(provider, "fetch_quota", None) if provider else None
        if fetch_quota is None:
            channels.append(
                {
                    "id": channel,
                    "display_name": getattr(provider, "display_name", channel) if provider else channel,
                    "unit": "unknown",
                    "remaining": None,
                    "ok": True,
                    "accounts": len(accounts),
                    "unsupported": True,
                    "message": "quota API not available",
                }
            )
            continue
        remaining_values = []
        used_values = []
        limit_values = []
        ok_count = 0
        unsupported = False
        message = ""
        channel_unit = "unknown"
        for account in accounts:
            snapshot = await fetch_quota(account)
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
        channels.append(
            {
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
        )
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
            "channel": "workbuddy",
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
        for item in chunk:
            if isinstance(item, dict):
                item["channel"] = channel
        results.extend(chunk)
    return {
        "total": len(results),
        "ok": sum(1 for row in results if row.get("ok")),
        "claimed": sum(1 for row in results if row.get("claimed")),
        "already_claimed": sum(1 for row in results if row.get("already_claimed") or row.get("today_checked_in")),
        "available": sum(1 for row in results if row.get("ok") and not (row.get("already_claimed") or row.get("today_checked_in"))),
        "failed": sum(1 for row in results if not row.get("ok")),
        "stale": sum(1 for row in results if row.get("stale")),
        "results": results,
    }


async def checkin_all(channel_filter: list[str] | None = None) -> dict:
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
            claim_fn = getattr(provider, "claim_checkin", None)
            if claim_fn:
                result = await claim_fn(account)
            else:
                result = await auth_manager.claim_daily_checkin(account)
            if result.get("ok") and not claim_fn:
                result["resources"] = await auth_manager.fetch_account_resources(account, force=True)
            result["channel"] = channel
            results.append(result)
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
