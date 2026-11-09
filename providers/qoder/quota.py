"""Qoder spendable credits: plan quota plus resource packs."""

from __future__ import annotations

import httpx

from tls_context import TLS_CONTEXT

from providers.protocol import QuotaSnapshot
from providers.qoder.constants import CLIENT_TYPE, COSY_VERSION, USAGE_PATH, region
from providers.qoder.token import QoderAuthError, refresh_account


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _bucket(raw: dict, key: str) -> dict | None:
    value = raw.get(key)
    return value if isinstance(value, dict) else None


def _usable_addon(bucket: dict | None) -> dict | None:
    if not bucket:
        return None
    numbers = [_number(bucket.get(name)) or 0 for name in ("total", "used", "remaining")]
    if all(value <= 0 for value in numbers):
        return None
    return bucket


def _usable_org(bucket: dict | None) -> dict | None:
    if not bucket:
        return None
    available = bucket.get("available")
    cap = _number(bucket.get("cap"))
    if cap is None:
        cap = _number(bucket.get("total"))
    if available is False:
        return None
    if available is None and not any(
        (_number(bucket.get(name)) or 0) > 0 for name in ("cap", "total", "used", "remaining")
    ):
        return None
    if cap is not None and cap <= 0 and available is not True:
        remaining = _number(bucket.get("remaining")) or 0
        if remaining <= 0:
            return None
    return bucket


def _package(name: str, bucket: dict) -> dict:
    total = _number(bucket.get("total"))
    if total is None:
        total = _number(bucket.get("cap"))
    return {
        "package_name": name,
        "remaining_precise": _number(bucket.get("remaining")),
        "used": _number(bucket.get("used")),
        "size": total,
        "cycle_size": total,
        "expired": False,
    }


def parse_usage(payload) -> dict:
    raw = payload if isinstance(payload, dict) else {}
    nested = raw.get("data")
    if isinstance(nested, dict) and ("userQuota" in nested or "addOnQuota" in nested):
        raw = nested
    rows = []
    remaining_parts = []
    plan = _bucket(raw, "userQuota")
    if plan:
        rows.append(_package("套餐额度", plan))
        value = _number(plan.get("remaining"))
        if value is not None:
            remaining_parts.append(value)
    addon = _usable_addon(_bucket(raw, "addOnQuota"))
    if addon:
        rows.append(_package("个人资源包", addon))
        value = _number(addon.get("remaining"))
        if value is not None:
            remaining_parts.append(value)
    org = _usable_org(_bucket(raw, "orgResourcePackage"))
    if org:
        rows.append(_package("组织资源包", org))
        value = _number(org.get("remaining"))
        if value is not None:
            remaining_parts.append(value)
    remaining = sum(remaining_parts) if remaining_parts else None
    parts = []
    for row in rows:
        amount = row.get("remaining_precise")
        if amount is None:
            continue
        shown = int(amount) if amount == int(amount) else amount
        parts.append(f"{row['package_name']} {shown}")
    unit = "credits"
    if plan and isinstance(plan.get("unit"), str) and plan.get("unit"):
        unit = plan["unit"]
    message = " · ".join(parts)
    if message:
        message = f"{message} {unit}"
    return {
        "remaining": remaining,
        "packages": rows,
        "message": message or "未读到额度数字",
        "unit": unit,
    }


def _headers(account: dict) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {account.get('access_token') or ''}",
        "Cosy-Version": COSY_VERSION,
        "Cosy-ClientType": CLIENT_TYPE,
        "Accept": "application/json",
        "User-Agent": "Qoder",
    }


async def fetch_quota(account: dict) -> QuotaSnapshot:
    channel = str(account.get("provider") or "qoder")
    account_id = int(account.get("id") or 0)
    current = account
    info = region(channel)
    url = f"{info['openapi']}{USAGE_PATH}"
    response = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=20.0, trust_env=False) as client:
                response = await client.get(url, headers=_headers(current))
        except Exception as exc:
            return QuotaSnapshot(
                ok=False,
                channel=channel,
                account_id=account_id,
                unit="credit",
                remaining=None,
                message=str(exc)[:180],
            )
        if response.status_code not in {401, 403} or attempt:
            break
        try:
            current = await refresh_account(current)
        except QoderAuthError as exc:
            return QuotaSnapshot(
                ok=False,
                channel=channel,
                account_id=account_id,
                unit="credit",
                remaining=None,
                message=str(exc)[:180],
            )
    try:
        payload = response.json() if response is not None else {}
    except ValueError:
        payload = {}
    status = response.status_code if response is not None else 0
    if status >= 400:
        return QuotaSnapshot(
            ok=False,
            channel=channel,
            account_id=account_id,
            unit="credit",
            remaining=None,
            message=f"额度查询 HTTP {status}",
        )
    parsed = parse_usage(payload)
    return QuotaSnapshot(
        ok=parsed["remaining"] is not None,
        channel=channel,
        account_id=account_id,
        unit="credit",
        remaining=parsed["remaining"],
        extra={"packages": parsed["packages"], "unit_label": parsed["unit"]},
        unsupported=parsed["remaining"] is None,
        message=parsed["message"],
    )
