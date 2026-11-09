"""Fetch the live Qoder model list for one region."""

from __future__ import annotations

import httpx

from tls_context import TLS_CONTEXT

from providers.qoder import cosy
from providers.qoder.constants import inference_base, upstream_key
from providers.qoder.token import is_token_expired, refresh_account


class QoderModelsError(RuntimeError):
    pass


def parse_supplier_models(payload) -> list[dict]:
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        grouped = payload.get("chat")
        if isinstance(grouped, list):
            rows = grouped
        else:
            data = payload.get("data") if isinstance(payload.get("data"), (list, dict)) else payload
            if isinstance(data, dict):
                for key in ("chat", "models", "list", "items"):
                    if isinstance(data.get(key), list):
                        rows = data[key]
                        break
            elif isinstance(data, list):
                rows = data
    models = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("enable") is False:
            continue
        mid = str(row.get("display_name") or row.get("displayName") or row.get("key") or row.get("id") or "").strip()
        key = str(row.get("key") or row.get("value") or mid).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append({"id": mid, "name": mid, "upstream_key": key})
    return models


async def fetch_supplier_models(account: dict) -> list[dict]:
    channel = str(account.get("provider") or "qoder")
    if is_token_expired(account):
        account = await refresh_account(account)
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    base = inference_base(channel, str(account.get("access_token") or "")).rstrip("/")
    url = f"{base}/algo/api/v2/model/list?Encode=1"
    headers = cosy.auth_headers(
        url=url,
        body=b"",
        uid=str(account.get("uid") or ""),
        name=str(account.get("nickname") or account.get("name") or ""),
        email=str(extra.get("email") or ""),
        access_token=str(account.get("access_token") or ""),
        machine_id=str(extra.get("machine_id") or ""),
    )
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=20.0, trust_env=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise QoderModelsError(str(exc)[:180]) from exc
    if response.status_code >= 400:
        raise QoderModelsError(f"Qoder 模型目录 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise QoderModelsError("Qoder 模型目录不是 JSON") from exc
    models = parse_supplier_models(payload)
    if not models:
        raise QoderModelsError("Qoder 模型目录为空")
    for item in models:
        if not item.get("upstream_key"):
            item["upstream_key"] = upstream_key(channel, item["id"])
    return models
