"""Xiaohuanxiong balance and daily points grant."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from tls_context import TLS_CONTEXT

from providers.protocol import QuotaSnapshot
from providers.xiaohuanxiong.constants import CHANNEL_ID, ORIGIN, POINTS_BASE
from providers.xiaohuanxiong.headers import client_headers
from providers.xiaohuanxiong.token import (
    XiaohuanxiongAuthError,
    ensure_ready,
    is_token_expired,
    refresh_account,
)


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_balance(payload) -> dict:
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
    if not data and isinstance(payload, dict):
        data = payload
    return {
        "available": _number(data.get("available_points")),
        "daily": _number(data.get("daily_points")),
        "monthly": _number(data.get("monthly_points")),
        "reward": _number(data.get("reward_points")),
    }


def parse_bills(payload) -> list[dict]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data if isinstance(data, list) else []
    if isinstance(data, dict):
        for key in ("bills", "list", "items"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "points": _number(row.get("points")) or 0,
                "created_at": str(row.get("created_at") or ""),
                "event_name": str(row.get("event_name") or ""),
            }
        )
    return out


def _today_points(bills: list[dict], now: datetime | None = None) -> float:
    current = now or datetime.now().astimezone()
    day = current.date()
    total = 0.0
    for row in bills:
        text = row.get("created_at") or ""
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            if text[:10] == day.isoformat():
                total += max(0.0, float(row.get("points") or 0))
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.astimezone(current.tzinfo).date() == day and float(row.get("points") or 0) > 0:
            total += float(row["points"])
    return total


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


async def _get(url: str, token: str) -> tuple[int, dict]:
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=20.0) as client:
        response = await client.get(url, headers=client_headers(token))
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}


async def _ensure(account: dict) -> dict:
    return await ensure_ready(account)


async def fetch_quota(account: dict) -> QuotaSnapshot:
    account_id = int(account.get("id") or 0)
    try:
        account = await _ensure(account)
        status, payload = await _get(f"{POINTS_BASE}/balance", str(account.get("access_token") or ""))
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
            message=f"小浣熊积分 HTTP {status}",
        )
    parsed = parse_balance(payload)
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=account_id,
        unit="credit",
        remaining=parsed["available"],
        extra=parsed,
    )


async def _bills(account: dict) -> list[dict]:
    status, payload = await _get(
        f"{ORIGIN}/api/web/points/v1/bills?paging.limit=20&paging.offset=0",
        str(account.get("access_token") or ""),
    )
    if status >= 400:
        return []
    return parse_bills(payload)


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    del force
    try:
        account = await _ensure(account)
        bills = await _bills(account)
    except Exception as exc:
        return _row(account, ok=False, message=str(exc)[:240])
    credit = _today_points(bills)
    if credit > 0:
        return _row(account, ok=True, already_claimed=True, credit=credit, message="今日积分已入账")
    return _row(account, ok=True, message="今日尚未领到积分")


async def claim_checkin(account: dict) -> dict:
    try:
        account = await ensure_ready(account)
        before = _today_points(await _bills(account))
        status, payload = await _get(
            f"{ORIGIN}/api/web/office/v3/setting_info",
            str(account.get("access_token") or ""),
        )
    except (httpx.HTTPError, XiaohuanxiongAuthError, RuntimeError) as exc:
        return _row(account, ok=False, message=str(exc)[:240])
    code = payload.get("code") if isinstance(payload, dict) else None
    if status >= 400 or code not in (None, 0):
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("msg") or "")
        return _row(account, ok=False, status_code=status, message=message or f"每日发放 HTTP {status}")
    after_bills = await _bills(account)
    after = _today_points(after_bills)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    triggered = bool(data.get("point_grant_popups") or data.get("point_grant_toast"))
    if after > before:
        return _row(account, ok=True, status_code=status, claimed=True, credit=after - before, message="领取成功")
    if after > 0 or before > 0:
        return _row(
            account,
            ok=True,
            status_code=status,
            already_claimed=True,
            credit=after or before,
            message="今日已领取",
        )
    if triggered:
        return _row(account, ok=True, status_code=status, claimed=True, message="已触发每日发放")
    return _row(account, ok=True, status_code=status, already_claimed=True, message="已触发每日发放")
