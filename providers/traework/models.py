"""TraeWork supplier-list fetch. Isolated from chat I/O."""

from __future__ import annotations

import httpx

from tls_context import TLS_CONTEXT
from model_capacity import capacity_fields

from providers.traework.constants import AGENT_API, MODELS_PATH
from providers.traework.token import TraeWorkAuthError, auth_headers


def parse_supplier_models(payload) -> list[dict]:
    rows = _rows_from_payload(payload)
    models: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
            name = mid
        elif isinstance(row, dict):
            mid = str(
                row.get("id")
                or row.get("model_name")
                or row.get("name")
                or row.get("model")
                or ""
            ).strip()
            name = str(row.get("display_name") or row.get("name") or row.get("model_name") or mid)
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item = {"id": mid, "name": name or mid}
        item.update(capacity_fields(row))
        models.append(item)
    return models


def _flatten_model_groups(rows: list) -> list:
    """TraeWork returns function buckets: data.list[].models[], not a flat id list."""
    flattened: list = []
    for row in rows:
        if not isinstance(row, dict):
            flattened.append(row)
            continue
        nested = row.get("models")
        grouped = isinstance(nested, list) and not (
            row.get("id") or row.get("model_name") or row.get("model")
        )
        if grouped:
            flattened.extend(nested)
            continue
        flattened.append(row)
    return flattened


def _rows_from_payload(payload) -> list:
    if isinstance(payload, list):
        return _flatten_model_groups(payload)
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
    if isinstance(data, list):
        return _flatten_model_groups(data)
    if not isinstance(data, dict):
        return []
    for key in ("models", "model_list", "items", "list", "model_infos"):
        rows = data.get(key)
        if isinstance(rows, list):
            return _flatten_model_groups(rows)
    nested = data.get("data")
    if isinstance(nested, list):
        return _flatten_model_groups(nested)
    if isinstance(nested, dict):
        for key in ("models", "model_list", "items", "list"):
            rows = nested.get(key)
            if isinstance(rows, list):
                return _flatten_model_groups(rows)
    return []


async def fetch_supplier_models(account: dict) -> list[dict]:
    headers = auth_headers(account)
    url = f"{AGENT_API}{MODELS_PATH}"
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30.0) as client:
        # Without functions, the endpoint defaults to the legacy solo_coder
        # catalog. Work sessions use the current solo_work_remote catalog.
        response = await client.get(
            url,
            headers=headers,
            params={"functions": "solo_work_remote", "show_custom_model": "true"},
        )
    if response.status_code >= 400:
        raise TraeWorkAuthError(f"models HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise TraeWorkAuthError("models response is not JSON") from exc
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        raise TraeWorkAuthError(str(payload.get("message") or payload.get("code")))
    return parse_supplier_models(payload)
