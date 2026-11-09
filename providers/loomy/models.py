"""Read the Loomy supplier model list."""

from __future__ import annotations

import httpx

from tls_context import TLS_CONTEXT
from model_capacity import capacity_fields

from providers.loomy.constants import MODEL_BASE
from providers.loomy.headers import model_headers


def parse_supplier_models(payload) -> list[dict]:
    rows = _rows(payload)
    models: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
            name = mid
            raw = {}
        elif isinstance(row, dict):
            mid = str(row.get("id") or row.get("name") or row.get("model") or "").strip()
            name = str(row.get("name") or row.get("display_name") or mid)
            raw = row
        else:
            continue
        if mid.startswith("provider/"):
            mid = mid.split("/", 1)[1]
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item = {"id": mid, "name": name or mid}
        window = raw.get("context_window") or raw.get("context_length")
        if type(window) is int and window > 0:
            raw = {**raw, "context_window": window}
        item.update(capacity_fields(raw))
        models.append(item)
    return models


def _rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("models", "list", "items"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    for key in ("models", "list", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return rows
    return []


async def fetch_supplier_models(account: dict) -> list[dict]:
    session = str(account.get("access_token") or "")
    if not session:
        raise RuntimeError("Loomy account has no session")
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30.0) as client:
        response = await client.get(f"{MODEL_BASE}/models", headers=model_headers(session, purpose="chat.message"))
    if response.status_code >= 400:
        raise RuntimeError(f"Loomy /models HTTP {response.status_code}")
    return parse_supplier_models(response.json())
