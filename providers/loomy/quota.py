"""Loomy points. Daily balance is granted by the upstream and resets each day."""

from __future__ import annotations

import httpx

from tls_context import TLS_CONTEXT

import database as db
from providers.loomy.constants import CHANNEL_ID, MODEL_BASE
from providers.loomy.headers import model_headers
from providers.loomy.token import LoomyAuthError, refresh_account
from providers.protocol import QuotaSnapshot


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_quota(payload) -> dict:
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
    if not data and isinstance(payload, dict):
        data = payload
    return {
        "balance": _number(data.get("balance")),
        "daily_balance": _number(data.get("dailyBalance") if "dailyBalance" in data else data.get("daily_balance")),
        "available": _number(
            data.get("availableBalance") if "availableBalance" in data else data.get("available")
        ),
    }


def _row(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    claimed: bool = False,
    already_claimed: bool = False,
    credit: float = 0,
) -> dict:
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": True,
        "today_checked_in": already_claimed or claimed,
        "today_credit": credit,
        "channel": CHANNEL_ID,
    }


async def _points(account: dict) -> tuple[int, dict]:
    session = str(account.get("access_token") or "")
    if not session:
        raise LoomyAuthError("Loomy account has no session")
    url = f"{MODEL_BASE}/points/records?record_type=all&page_no=1&page_size=1"
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=20.0) as client:
        response = await client.get(url, headers=model_headers(session, purpose="chat.message"))
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


async def fetch_quota(account: dict) -> QuotaSnapshot:
    account_id = int(account.get("id") or 0)
    try:
        if not account.get("access_token"):
            account = await refresh_account(account)
        status, payload = await _points(account)
    except Exception as exc:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=account_id,
            unit="credit",
            remaining=None,
            message=str(exc)[:240],
        )
    if status >= 400:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=account_id,
            unit="credit",
            remaining=None,
            message=f"Loomy points HTTP {status}",
        )
    parsed = parse_quota(payload)
    remaining = parsed["available"]
    if remaining is None:
        remaining = parsed["daily_balance"]
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=account_id,
        unit="credit",
        remaining=remaining,
        extra=parsed,
        message="",
    )


def _remember_claimed(account: dict, result: dict) -> dict:
    account_id = account.get("id")
    if account_id and result.get("ok") and (result.get("already_claimed") or result.get("today_checked_in")):
        db.upsert_account_checkin_cache(int(account_id), result)
    return result


def _cached_claimed(account: dict, message: str) -> dict | None:
    """Keep today's confirmed balance when a later status read flakes."""
    account_id = account.get("id")
    if not account_id:
        return None
    cached = db.get_account_checkin_cache(int(account_id))
    if not isinstance(cached, dict) or not cached.get("ok"):
        return None
    if not (cached.get("already_claimed") or cached.get("today_checked_in") or cached.get("claimed")):
        return None
    cached["stale"] = True
    cached["ok"] = True
    cached["already_claimed"] = True
    cached["today_checked_in"] = True
    if message:
        cached["message"] = message[:240]
    return cached


async def _read_points(account: dict) -> tuple[int, dict, str]:
    try:
        status, payload = await _points(account)
    except Exception as exc:
        return 0, {}, str(exc)[:240]
    return status, payload, ""


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    del force
    current = account
    status, payload, error = await _read_points(current)
    if error or status in {401, 403} or not str(current.get("access_token") or ""):
        try:
            current = await refresh_account(current)
            status, payload, error = await _read_points(current)
        except LoomyAuthError as exc:
            error = error or str(exc)[:240]
    if error:
        return _cached_claimed(account, error) or _row(account, ok=False, message=error)
    if status >= 400:
        message = f"Loomy points HTTP {status}"
        return _cached_claimed(account, message) or _row(
            account, ok=False, status_code=status, message=message
        )
    parsed = parse_quota(payload)
    daily = parsed["daily_balance"]
    if daily is None:
        cached = _cached_claimed(account, "上游未返回每日额度")
        if cached:
            return cached
        return _row(account, ok=True, status_code=status, message="上游未返回每日额度")
    return _remember_claimed(
        current,
        _row(
            current,
            ok=True,
            status_code=status,
            already_claimed=True,
            credit=daily,
            message=f"每日额度已在账，剩余 {daily:g}",
        ),
    )


async def claim_checkin(account: dict) -> dict:
    try:
        account = await refresh_account(account)
    except LoomyAuthError as exc:
        cached = _cached_claimed(account, str(exc)[:240])
        if cached:
            return cached
        return _row(account, ok=False, message=str(exc)[:240])
    return await fetch_checkin(account, force=True)
