"""TraeWork check-in and official credit usage."""

from __future__ import annotations

import httpx

from tls_context import TLS_CONTEXT

from providers.protocol import QuotaSnapshot
from providers.traework.constants import (
    CHANNEL_ID,
    CHECKIN_CLAIM_PATH,
    CHECKIN_STATUS_PATH,
    REQ_SOURCE,
    UG_API,
    USAGE_PATH,
)
from providers.traework.token import TraeWorkAuthError, auth_headers, extra_of, is_token_expired, refresh_account

LOGIN_EXPIRED = "TraeWork 登录已失效，请打开官方客户端重新登录后再导入"


def _host(account: dict) -> str:
    extra = extra_of(account)
    return str(extra.get("host") or UG_API).rstrip("/") or UG_API


def _checkin_row(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    claimed: bool = False,
    already_claimed: bool = False,
    credit: float = 0,
    today_checked_in: bool | None = None,
    extra: dict | None = None,
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
        "today_checked_in": already_claimed if today_checked_in is None else today_checked_in,
        "today_credit": credit,
        "channel": CHANNEL_ID,
        **(extra or {}),
    }


def _read_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _is_auth_failure(status_code: int, data: dict) -> bool:
    if status_code in (401, 403):
        return True
    if data.get("code") == 1001:
        return True
    message = str(data.get("message") or "").lower()
    return "not able to authenticate" in message or "authenticate you" in message


def _refresh_failure_message(exc: TraeWorkAuthError) -> str:
    text = str(exc)
    dead = (
        "no refresh_token" in text
        or "no device private key" in text
        or "HTTP 401" in text
        or "HTTP 403" in text
        or "missing Token" in text
    )
    if dead:
        return LOGIN_EXPIRED
    return text[:240]


def _credit_amount(data: dict) -> float:
    raw = data.get("credits")
    if raw is None:
        raw = data.get("credit")
    number = _number(raw)
    if number is None or number <= 0:
        return 0.0
    return number


def _pack_quota(pack: dict) -> dict:
    base = pack.get("entitlement_base_info")
    base = base if isinstance(base, dict) else {}
    quota = base.get("quota")
    if isinstance(quota, dict) and "credits_limit" in quota:
        return quota
    extra = base.get("product_extra")
    extra = extra if isinstance(extra, dict) else {}
    subscription = extra.get("subscription_extra")
    subscription = subscription if isinstance(subscription, dict) else {}
    nested = subscription.get("quota")
    return nested if isinstance(nested, dict) else {}


def parse_credit_usage(data: dict) -> tuple[float | None, float | None, float | None, bool]:
    """Return remaining, used, limit, unlimited.

    Official 0.1.56 sums user_entitlement_pack_list. credits_limit -1 is unlimited.
    usage_summary.total_amount is only a fallback; the client does not read it.
    """
    if not isinstance(data, dict):
        return None, None, None, False
    packs = data.get("user_entitlement_pack_list")
    if isinstance(packs, list) and packs:
        limit_sum = 0.0
        used_sum = 0.0
        remaining_sum = 0.0
        saw_limit = False
        unlimited = False
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            limit = _number(_pack_quota(pack).get("credits_limit"))
            usage = pack.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            amount = _number(usage.get("credits_amount")) or 0.0
            if limit == -1:
                unlimited = True
                saw_limit = True
            elif limit is not None and limit > 0:
                saw_limit = True
                limit_sum += limit
                remaining_sum += max(limit - amount, 0.0)
            if limit is not None and limit != 0:
                used_sum += amount
        if saw_limit:
            if unlimited:
                return None, used_sum, None, True
            return remaining_sum, used_sum, limit_sum, False
    summary = data.get("usage_summary")
    summary = summary if isinstance(summary, dict) else {}
    total = _number(summary.get("total_amount"))
    if total is None:
        return None, None, None, False
    consumed = _number(summary.get("consumed_amount")) or 0.0
    return total - consumed, consumed, total, False


async def _refresh_or_error(account: dict) -> tuple[dict, str]:
    if not account.get("id"):
        return account, LOGIN_EXPIRED
    try:
        return await refresh_account(account), ""
    except TraeWorkAuthError as exc:
        return account, _refresh_failure_message(exc)


async def _post_checked(
    account: dict,
    path: str,
    body: dict,
    timeout: float,
) -> tuple[dict, httpx.Response | None, dict, str]:
    current = account
    refreshed = False
    if is_token_expired(current):
        current, error = await _refresh_or_error(current)
        if error:
            return current, None, {}, error
        refreshed = True
    url = f"{_host(current)}{path}"
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=timeout) as client:
            response = await client.post(url, headers=auth_headers(current), json=body)
    except httpx.HTTPError as exc:
        return current, None, {}, str(exc)[:240]
    data = _read_json(response)
    if refreshed or not _is_auth_failure(response.status_code, data):
        return current, response, data, ""
    current, error = await _refresh_or_error(current)
    if error:
        return current, response, data, error
    url = f"{_host(current)}{path}"
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=timeout) as client:
            response = await client.post(url, headers=auth_headers(current), json=body)
    except httpx.HTTPError as exc:
        return current, None, {}, str(exc)[:240]
    return current, response, _read_json(response), ""


def _failure_message(error: str, status_code: int, data: dict) -> str:
    if error:
        return error[:240]
    if _is_auth_failure(status_code, data):
        return LOGIN_EXPIRED
    return str(data.get("message") or f"HTTP {status_code}")[:240]


def _status_row(account: dict, response: httpx.Response | None, data: dict, error: str) -> dict:
    if error and response is None:
        return _checkin_row(account, ok=False, message=error)
    status_code = response.status_code if response is not None else 0
    if error or status_code >= 400 or data.get("code") not in (None, 0):
        return _checkin_row(
            account,
            ok=False,
            status_code=status_code,
            message=_failure_message(error, status_code, data),
        )
    checked = bool(data.get("checked_in") or data.get("checkedIn"))
    return _checkin_row(
        account,
        ok=True,
        status_code=status_code,
        already_claimed=checked,
        today_checked_in=checked,
        credit=_credit_amount(data),
        message=str(data.get("message") or "success"),
        extra={"enable": bool(data.get("enable", True))},
    )


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    current, response, data, error = await _post_checked(account, CHECKIN_STATUS_PATH, {}, 20.0)
    return _status_row(current, response, data, error)


async def claim_checkin(account: dict) -> dict:
    current, response, data, error = await _post_checked(account, CHECKIN_STATUS_PATH, {}, 20.0)
    status = _status_row(current, response, data, error)
    if not status.get("ok"):
        return status
    if status.get("already_claimed") or status.get("today_checked_in"):
        status["already_claimed"] = True
        status["message"] = "今日已领取"
        return status
    current, response, data, error = await _post_checked(current, CHECKIN_CLAIM_PATH, {}, 30.0)
    if error and response is None:
        return _checkin_row(current, ok=False, message=error)
    status_code = response.status_code if response is not None else 0
    if error or status_code >= 400 or data.get("code") not in (None, 0):
        return _checkin_row(
            current,
            ok=False,
            status_code=status_code,
            message=_failure_message(error, status_code, data),
        )
    return _checkin_row(
        current,
        ok=True,
        status_code=status_code,
        claimed=True,
        credit=_credit_amount(data) or float(status.get("credit") or 0),
        message=str(data.get("message") or "success"),
    )


def _quota_snapshot(
    account: dict,
    *,
    ok: bool,
    remaining: float | None = None,
    used: float | None = None,
    limit: float | None = None,
    unlimited: bool = False,
    message: str = "",
    unsupported: bool = False,
) -> QuotaSnapshot:
    return QuotaSnapshot(
        ok=ok,
        channel=CHANNEL_ID,
        account_id=int(account.get("id") or 0),
        unit="credit",
        remaining=remaining,
        extra={"consumed": used, "total": limit, "used": used, "limit": limit, "unlimited": unlimited},
        unsupported=unsupported,
        message=message,
    )


async def fetch_quota(account: dict) -> QuotaSnapshot:
    current, response, data, error = await _post_checked(
        account,
        USAGE_PATH,
        {"require_usage": True, "req_source": REQ_SOURCE},
        30.0,
    )
    if error and response is None:
        return _quota_snapshot(current, ok=False, message=error)
    status_code = response.status_code if response is not None else 0
    if error or status_code >= 400 or data.get("code") not in (None, 0):
        return _quota_snapshot(current, ok=False, message=_failure_message(error, status_code, data))
    remaining, used, limit, unlimited = parse_credit_usage(data)
    if unlimited:
        return _quota_snapshot(
            current,
            ok=True,
            used=used,
            unlimited=True,
            message="无限额度",
        )
    return _quota_snapshot(
        current,
        ok=True,
        remaining=remaining,
        used=used,
        limit=limit,
        unsupported=remaining is None,
        message="" if remaining is not None else "quota unit unknown",
    )
