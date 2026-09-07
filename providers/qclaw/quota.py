"""QClaw daily token cap. Not credits — never mix into credit totals."""

from __future__ import annotations

import httpx

from providers.protocol import QuotaSnapshot
from providers.qclaw.constants import CHANNEL_ID
from providers.qclaw.jprx import JprxError, today_tokens

_USED_KEYS = (
    "daily_token_used",
    "today_used",
    "used_tokens",
    "token_used",
    "tokens_used",
    "used",
    "consumed",
    "today_tokens",
)
_LIMIT_KEYS = (
    "daily_token_limit",
    "today_limit",
    "total_tokens",
    "token_limit",
    "token_quota",
    "limit",
    "quota",
    "cap",
    "max",
)
_REMAIN_KEYS = ("remaining", "remain", "left", "available", "surplus")


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            return None
        return number
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if number > 10_000_000_000:
            return None
        return number
    return None


def _pick(data: dict, keys: tuple[str, ...]) -> float | None:
    lower = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        if key in lower:
            number = _number(lower[key])
            if number is not None:
                return number
    return None


def parse_today_tokens(data: dict | None) -> tuple[float | None, float | None, float | None]:
    """Return (used, limit, remaining) from a jprx 4075 payload."""
    if not isinstance(data, dict):
        return None, None, None
    layers = [data]
    for key in ("data", "resp", "usage", "today", "token", "tokens"):
        nested = data.get(key)
        if isinstance(nested, dict):
            layers.append(nested)
    used = limit = remaining = None
    for layer in layers:
        if used is None:
            used = _pick(layer, _USED_KEYS)
        if limit is None:
            limit = _pick(layer, _LIMIT_KEYS)
        if remaining is None:
            remaining = _pick(layer, _REMAIN_KEYS)
    if remaining is None and used is not None and limit is not None:
        remaining = max(0.0, limit - used)
    return used, limit, remaining


async def fetch_quota(account: dict) -> QuotaSnapshot:
    account_id = int(account.get("id") or 0)
    try:
        data = await today_tokens(account)
    except JprxError as exc:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=account_id,
            unit="token",
            remaining=None,
            message=str(exc)[:240],
        )
    except httpx.HTTPError as exc:
        return QuotaSnapshot(
            ok=False,
            channel=CHANNEL_ID,
            account_id=account_id,
            unit="token",
            remaining=None,
            message=str(exc)[:240],
        )
    used, limit, remaining = parse_today_tokens(data if isinstance(data, dict) else {})
    extra = {
        "used": used,
        "limit": limit,
        "raw_keys": sorted(data.keys())[:12] if isinstance(data, dict) else [],
    }
    if used is None and limit is None and remaining is None:
        return QuotaSnapshot(
            ok=True,
            channel=CHANNEL_ID,
            account_id=account_id,
            unit="token",
            remaining=None,
            extra=extra,
            unsupported=True,
            message="today token fields unknown",
        )
    return QuotaSnapshot(
        ok=True,
        channel=CHANNEL_ID,
        account_id=account_id,
        unit="token",
        remaining=remaining,
        extra=extra,
        unsupported=False,
    )
