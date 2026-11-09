"""QwenWork daily credit status.

The 1.3.1 claim route is gone. Official docs say the daily bucket is issued at
00:00 for a valid QwenWork login and expires the same day. This module only
reads wallets and reports whether that bucket is present.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from tls_context import TLS_CONTEXT

import database as db
from providers.qwenwork.constants import (
    CHANNEL_ID,
    CLAIM_APP_MODE,
    CLAIM_BUILD,
    CLAIM_IDE_VERSION,
    CLAIM_RELEASE_VERSION,
    CLAIM_USER_AGENT,
    GATEWAY,
    WALLETS_PATH,
)
from providers.qwenwork.token import QwenWorkAuthError, is_token_expired, refresh_account

SHANGHAI = timezone(timedelta(hours=8))
NOT_ISSUED_MESSAGE = "今日每日积分尚未到账。千问办公在 0 点按官方登录会话发放，网关不能代领。"
WALLET_EXPIRE_SOURCE = "qwenwork-wallet"


def parse_daily_credit_notice(payload) -> dict:
    """Keep the old desktop parser for tests. Claim no longer uses this route."""
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("data")
    if isinstance(nested, dict):
        data = nested
        inner = data.get("data")
        if isinstance(inner, dict):
            data = inner
    credits = data.get("credits") if isinstance(data, dict) else None
    amount = 0.0
    if isinstance(credits, int) and not isinstance(credits, bool) and credits > 0:
        amount = float(credits)
    should_show = isinstance(data, dict) and data.get("should_show") is True and amount > 0
    return {"should_show": should_show, "credits": amount}


def _next_shanghai_midnight(now: datetime | None = None) -> datetime:
    current = now.astimezone(SHANGHAI) if now else datetime.now(SHANGHAI)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(days=1)


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def _positive_balance(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if value <= 0:
        return 0.0
    return float(value)


def _wallet_rows(payload) -> list[dict]:
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("data")
    if isinstance(nested, dict):
        data = nested
        inner = data.get("data")
        if isinstance(inner, dict):
            data = inner
    active = data.get("active_wallets") if isinstance(data, dict) else None
    rows = active.get("wallets") if isinstance(active, dict) else None
    if rows is None and isinstance(data, dict):
        rows = data.get("wallets")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def daily_credit_balance(payload, now: datetime | None = None) -> float:
    """Sum wallets that expire at the next Shanghai midnight."""
    midnight = _next_shanghai_midnight(now)
    total = 0.0
    for row in _wallet_rows(payload):
        expiry = _parse_time(row.get("valid_to") or row.get("validTo"))
        if expiry is None:
            continue
        if abs((expiry.astimezone(SHANGHAI) - midnight).total_seconds()) > 60:
            continue
        total += _positive_balance(row.get("balance"))
    return total


def wallet_expiry(payload, now: datetime | None = None) -> float | None:
    """The soonest wallet deadline still worth routing on, as a unix timestamp.

    Wallets are the only place this channel reports a deadline: the daily bucket
    expires at the next Shanghai midnight, longer packs carry their own
    ``valid_to``. Wallets that are already expired or already spent are not
    deadlines, so they are skipped.
    """
    moment = (now or datetime.now(SHANGHAI)).timestamp()
    soonest = None
    for row in _wallet_rows(payload):
        expiry = _parse_time(row.get("valid_to") or row.get("validTo"))
        if expiry is None or _positive_balance(row.get("balance")) <= 0:
            continue
        stamp = expiry.timestamp()
        if stamp <= moment:
            continue
        if soonest is None or stamp < soonest:
            soonest = stamp
    return soonest


def _row(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    claimed: bool = False,
    already_claimed: bool = False,
    credit: float = 0,
    no_campaign: bool = False,
    next_expire_ts: float | None = None,
    expire_source: str = "",
) -> dict:
    row = {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "no_campaign": no_campaign,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": True,
        "today_checked_in": already_claimed or claimed,
        "today_credit": credit,
        "channel": CHANNEL_ID,
    }
    if next_expire_ts:
        # Callers persist this; the row keeps its old shape when nothing is known.
        row["next_expire_ts"] = next_expire_ts
        row["expire_source"] = expire_source
    return row


def _remember(account: dict, result: dict) -> dict:
    account_id = account.get("id")
    if account_id and result.get("ok") and (result.get("claimed") or result.get("already_claimed")):
        db.upsert_account_checkin_cache(int(account_id), result)
    return result


def _cached_today(account: dict) -> dict | None:
    account_id = account.get("id")
    if not account_id:
        return None
    cached = db.get_account_checkin_cache(int(account_id))
    if not isinstance(cached, dict) or not cached.get("ok"):
        return None
    if not (cached.get("claimed") or cached.get("already_claimed") or cached.get("today_checked_in")):
        return None
    cached["claimed"] = False
    cached["already_claimed"] = True
    cached["today_checked_in"] = True
    cached["no_campaign"] = False
    cached["stale"] = False
    cached["channel"] = CHANNEL_ID
    return cached


def _status_headers(access: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": CLAIM_USER_AGENT,
        "X-QwenWork-Version": CLAIM_IDE_VERSION,
        "X-QwenWork-Release-Version": CLAIM_RELEASE_VERSION,
        "X-QwenWork-Build": CLAIM_BUILD,
        "X-QwenWork-Platform": "win32",
        "X-QwenWork-Arch": "x64",
        "X-QwenWork-Channel": "stable",
        "X-QwenWork-App-Mode": CLAIM_APP_MODE,
    }
    if access:
        headers["Authorization"] = f"Bearer {access}"
    return headers


def _error_detail(payload: dict, status: int) -> str:
    detail = ""
    if isinstance(payload, dict):
        nested = payload.get("error")
        if isinstance(nested, dict):
            detail = str(nested.get("message") or nested.get("code") or "")
        detail = detail or str(
            payload.get("errorMessage") or payload.get("errorCode") or payload.get("message") or ""
        )
    text = f"QwenWork 钱包 HTTP {status}"
    if detail and detail != "None":
        text += f" {detail}"
    return text[:240]


async def _get_wallets(account: dict) -> tuple[int, dict, str]:
    headers = _status_headers(str(account.get("access_token") or ""))
    url = f"{GATEWAY}{WALLETS_PATH}"
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=20.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return 0, {}, str(exc)[:240]
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}, ""


def _from_wallets(account: dict, status: int, payload: dict) -> dict:
    credit = daily_credit_balance(payload)
    # Reported even when the daily bucket is missing: a long-lived pack keeps
    # its own valid_to, and that is a routing deadline too.
    expires = wallet_expiry(payload)
    if credit > 0:
        return _row(
            account,
            ok=True,
            status_code=status,
            already_claimed=True,
            credit=credit,
            message=f"今日每日积分已到账 {credit:g}",
            next_expire_ts=expires,
            expire_source=WALLET_EXPIRE_SOURCE,
        )
    return _row(
        account,
        ok=True,
        status_code=status,
        no_campaign=True,
        message=NOT_ISSUED_MESSAGE,
        next_expire_ts=expires,
        expire_source=WALLET_EXPIRE_SOURCE,
    )


async def _read_daily_credit(account: dict) -> tuple[dict, dict]:
    current = account
    if is_token_expired(current) or not str(current.get("access_token") or ""):
        try:
            current = await refresh_account(current)
        except QwenWorkAuthError as exc:
            return current, _row(account, ok=False, message=str(exc)[:240])
    status, payload, error = await _get_wallets(current)
    if error or status in {401, 403}:
        try:
            current = await refresh_account(current)
            status, payload, error = await _get_wallets(current)
        except QwenWorkAuthError as exc:
            return current, _row(account, ok=False, status_code=status, message=(error or str(exc))[:240])
    if error:
        return current, _row(account, ok=False, message=error)
    if status >= 400:
        return current, _row(account, ok=False, status_code=status, message=_error_detail(payload, status))
    return current, _from_wallets(current, status, payload)


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    if not force:
        cached = _cached_today(account)
        if cached:
            return cached
    current, result = await _read_daily_credit(account)
    if result.get("already_claimed"):
        return _remember(current, result)
    return result


async def claim_checkin(account: dict) -> dict:
    current, result = await _read_daily_credit(account)
    if result.get("already_claimed"):
        return _remember(current, result)
    return result
