"""Qoder campaign claim.

The list is a top-level ``campaigns`` array on the sash host. Reading only a
``data`` wrapper makes a claimed or claimable row look like an empty list.
International daily credits are listed only when the device risk headers are set.
"""

from __future__ import annotations

import time

import httpx

from tls_context import TLS_CONTEXT_NO_ENV

import database as db
from providers.qoder.constants import (
    CAMPAIGNS_PATH,
    CAMPAIGNS_QUERY,
    SASH_CLIENT_TYPE,
    SASH_USER_AGENT,
    region,
)
from providers.qoder.token import QoderAuthError, refresh_account

CAMPAIGN_EXPIRE_SOURCE = "qoder-campaign-validity"


def _headers(account: dict) -> dict[str, str]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    headers = {
        "authorization": f"Bearer {account.get('access_token') or ''}",
        "cosy-clienttype": SASH_CLIENT_TYPE,
        "accept": "application/json",
        "accept-language": "zh-CN",
        "user-agent": SASH_USER_AGENT,
    }
    machine = str(extra.get("machine_token") or extra.get("machine_id") or "")
    if machine and str(account.get("provider")) == "qoder-i":
        headers["Cosy-MachineToken"] = machine
        headers["Cosy-MachineCode"] = machine
    return headers


def campaigns_of(payload) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("campaigns")
    data = payload.get("data")
    if not isinstance(rows, list) and isinstance(data, list):
        rows = data
    if not isinstance(rows, list) and isinstance(data, dict):
        for key in ("campaigns", "list", "items"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _action(row: dict) -> str:
    return str(row.get("actionType") or row.get("action_type") or "")


def _claim_status(row: dict) -> str:
    return str(row.get("claimStatus") or row.get("claim_status") or "").upper()


def _amount(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _benefit_amount(row: dict) -> float:
    benefit = row.get("benefit") if isinstance(row.get("benefit"), dict) else {}
    return _amount(benefit.get("amount"))


def _benefit_valid_days(row: dict) -> float | None:
    """How long the granted credits last, in days.

    The campaign row states it as ``benefit.validity.days``; some payloads send
    the number itself instead of the wrapper, so both shapes are accepted. No
    duration means the credits do not expire on a known date.
    """
    benefit = row.get("benefit") if isinstance(row.get("benefit"), dict) else {}
    validity = benefit.get("validity")
    days = _amount(validity.get("days")) if isinstance(validity, dict) else _amount(validity)
    return days if days > 0 else None


def _expiry_after_days(days) -> float | None:
    if not days or days <= 0:
        return None
    return time.time() + float(days) * 86400.0


def _credit_text(amount: float) -> str:
    if amount <= 0:
        return ""
    shown = int(amount) if amount == int(amount) else amount
    return f" {shown} credits"


def _result(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    claimed: bool = False,
    already_claimed: bool = False,
    no_campaign: bool = False,
    credit: float = 0,
    next_expire_ts: float | None = None,
    expire_source: str = "",
) -> dict:
    row = {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id") or ""),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "no_campaign": no_campaign,
        "today_checked_in": already_claimed or claimed,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "channel": account.get("provider") or "qoder",
    }
    if next_expire_ts:
        # Callers persist this; the row keeps its old shape when nothing is known.
        row["next_expire_ts"] = next_expire_ts
        row["expire_source"] = expire_source
    return row


def describe_campaigns(payload) -> dict:
    """Turn a campaign list into a claim status. Does not call the network."""
    benefits = [row for row in campaigns_of(payload) if _action(row) == "CLAIM_BENEFIT"]
    claimable = next((row for row in benefits if _claim_status(row) not in {"CLAIMED", "ALREADY_CLAIMED"}), None)
    claimed = next((row for row in benefits if _claim_status(row) in {"CLAIMED", "ALREADY_CLAIMED"}), None)
    if claimable is not None:
        credit = _benefit_amount(claimable)
        return {
            "state": "claimable",
            "campaign_id": str(claimable.get("campaignId") or claimable.get("id") or ""),
            "credit": credit,
            "valid_days": _benefit_valid_days(claimable),
            "message": "可领取" + _credit_text(credit),
        }
    if claimed is not None:
        credit = _benefit_amount(claimed)
        return {
            "state": "claimed",
            "campaign_id": "",
            "credit": credit,
            "valid_days": _benefit_valid_days(claimed),
            "message": "今日已领取" + _credit_text(credit),
        }
    return {
        "state": "none",
        "campaign_id": "",
        "credit": 0,
        "valid_days": None,
        "message": "当前没有可领取的签到活动",
    }


def _from_description(account: dict, status_code: int, described: dict) -> dict:
    state = described.get("state")
    if state == "claimable":
        return _result(
            account,
            ok=True,
            status_code=status_code,
            message=described.get("message") or "可领取",
            credit=float(described.get("credit") or 0),
        )
    if state == "claimed":
        # The credits were granted today, so "now + validity" is the latest the
        # grant can expire: at most one day later than the real deadline.
        return _result(
            account,
            ok=True,
            status_code=status_code,
            already_claimed=True,
            credit=float(described.get("credit") or 0),
            message=described.get("message") or "今日已领取",
            next_expire_ts=_expiry_after_days(described.get("valid_days")),
            expire_source=CAMPAIGN_EXPIRE_SOURCE,
        )
    return _result(
        account,
        ok=True,
        status_code=status_code,
        no_campaign=True,
        message=described.get("message") or "当前没有可领取的签到活动",
    )


def _remember(account: dict, result: dict) -> dict:
    account_id = account.get("id")
    if account_id and result.get("ok") and (
        result.get("claimed") or result.get("already_claimed") or result.get("no_campaign")
    ):
        db.upsert_account_checkin_cache(int(account_id), result)
    return result


def _cached(account: dict) -> dict | None:
    account_id = account.get("id")
    if not account_id:
        return None
    cached = db.get_account_checkin_cache(int(account_id))
    if not isinstance(cached, dict) or not cached.get("ok"):
        return None
    if not (cached.get("claimed") or cached.get("already_claimed") or cached.get("no_campaign")):
        return None
    cached["channel"] = account.get("provider") or cached.get("channel")
    if cached.get("claimed"):
        cached["claimed"] = False
        cached["already_claimed"] = True
        cached["today_checked_in"] = True
    return cached


def _intl_blocked(account: dict) -> dict | None:
    if str(account.get("provider") or "") != "qoder-i":
        return None
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    if extra.get("machine_token") or extra.get("machine_id"):
        return None
    return _result(
        account,
        ok=False,
        message="国际版每日 100 Credits 需要本机 Qoder 设备风控头，当前账号没有 machine_token",
    )


async def _get_campaigns(account: dict) -> tuple[dict, int, dict]:
    current = account
    info = region(str(current.get("provider") or "qoder"))
    url = f"{info['openapi']}{CAMPAIGNS_PATH}{CAMPAIGNS_QUERY}"
    last_status = 0
    last_payload: dict = {}
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(verify=TLS_CONTEXT_NO_ENV, timeout=20.0, trust_env=False) as client:
                response = await client.get(url, headers=_headers(current))
        except Exception as exc:
            return current, 0, {"_error": str(exc)[:240]}
        last_status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        last_payload = payload if isinstance(payload, dict) else {}
        if last_status not in {401, 403} or attempt:
            return current, last_status, last_payload
        try:
            current = await refresh_account(current)
        except QoderAuthError as exc:
            last_payload = {"_error": str(exc)[:240]}
            return current, last_status, last_payload
    return current, last_status, last_payload


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    blocked = _intl_blocked(account)
    if blocked:
        return blocked
    if not force:
        cached = _cached(account)
        if cached:
            return cached
    current, status, payload = await _get_campaigns(account)
    if payload.get("_error"):
        return _result(current, ok=False, status_code=status, message=str(payload["_error"]))
    if status >= 400:
        return _result(current, ok=False, status_code=status, message=f"活动列表 HTTP {status}")
    return _remember(current, _from_description(current, status, describe_campaigns(payload)))


async def claim_checkin(account: dict) -> dict:
    blocked = _intl_blocked(account)
    if blocked:
        return blocked
    current, status, payload = await _get_campaigns(account)
    if payload.get("_error"):
        return _result(current, ok=False, status_code=status, message=str(payload["_error"]))
    if status >= 400:
        return _result(current, ok=False, status_code=status, message=f"活动列表 HTTP {status}")
    described = describe_campaigns(payload)
    if described["state"] != "claimable":
        return _remember(current, _from_description(current, status, described))
    campaign_id = described["campaign_id"]
    if not campaign_id:
        return _result(current, ok=False, status_code=status, message="签到活动缺少活动标识，无法领取")
    info = region(str(current.get("provider") or "qoder"))
    claim_url = f"{info['openapi']}{CAMPAIGNS_PATH}/{campaign_id}/claim"
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT_NO_ENV, timeout=20.0, trust_env=False) as client:
            claim = await client.post(
                claim_url,
                headers={**_headers(current), "content-type": "application/json", "origin": info["openapi"]},
                json={},
            )
    except Exception as exc:
        return _result(current, ok=False, message=str(exc)[:240])
    try:
        body = claim.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    replay = str(body.get("result") or body.get("status") or "").upper()
    if claim.status_code == 409 or replay == "ALREADY_CLAIMED" or body.get("replayed") is True:
        credit = _benefit_amount(body) or float(described.get("credit") or 0)
        valid_days = _benefit_valid_days(body) or described.get("valid_days")
        return _remember(
            current,
            _result(
                current,
                ok=True,
                status_code=claim.status_code,
                already_claimed=True,
                credit=credit,
                message="今日已领取" + _credit_text(credit),
                next_expire_ts=_expiry_after_days(valid_days),
                expire_source=CAMPAIGN_EXPIRE_SOURCE,
            ),
        )
    if claim.status_code >= 400 or str(body.get("status") or "").upper() not in {"", "CLAIMED"}:
        detail = str(body.get("message") or body.get("errorMessage") or claim.text[:180] or "")
        return _result(current, ok=False, status_code=claim.status_code, message=detail or f"领取失败 HTTP {claim.status_code}")
    credit = _benefit_amount(body) or float(described.get("credit") or 0)
    valid_days = _benefit_valid_days(body) or described.get("valid_days")
    return _remember(
        current,
        _result(
            current,
            ok=True,
            status_code=claim.status_code,
            claimed=True,
            credit=credit,
            message="已领取" + _credit_text(credit),
            next_expire_ts=_expiry_after_days(valid_days),
            expire_source=CAMPAIGN_EXPIRE_SOURCE,
        ),
    )
