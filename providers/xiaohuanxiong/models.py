"""Read the Xiaohuanxiong model catalog."""

from __future__ import annotations

import httpx

from tls_context import TLS_CONTEXT
from model_capacity import capacity_fields

from providers.xiaohuanxiong.constants import LLM_BASE
from providers.xiaohuanxiong.headers import client_headers


def parse_supplier_models(payload) -> list[dict]:
    rows = _flatten(_rows(payload))
    models: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("visible") is False:
            continue
        mid = str(row.get("name") or row.get("model_name") or row.get("id") or "").strip()
        name = str(row.get("display_name") or row.get("model_name") or row.get("name") or mid)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item = {"id": mid, "name": name or mid}
        window = _intish(row.get("context_window") or row.get("max_input_tokens"))
        output = _intish(row.get("max_tokens") or row.get("max_output_tokens"))
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        window = window or _intish(params.get("context_window"))
        output = output or _intish(params.get("max_tokens"))
        raw = dict(row)
        if window:
            raw["context_window"] = window
        if output:
            raw["max_output_tokens"] = output
        item.update(capacity_fields(raw))
        models.append(item)
    return models


def _intish(value) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if "data" in payload else payload
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("categories", "models", "list", "items"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    return []


def _flatten(rows: list) -> list:
    flat: list = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("models"), list):
            flat.extend(row["models"])
            continue
        flat.append(row)
    return flat


async def fetch_supplier_models(account: dict) -> list[dict]:
    token = str(account.get("access_token") or "")
    if not token:
        raise RuntimeError("小浣熊账号没有 access_token")
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=20.0) as client:
        response = await client.get(f"{LLM_BASE}/model_catalog", headers=client_headers(token))
    if response.status_code >= 400:
        raise RuntimeError(f"小浣熊模型目录 HTTP {response.status_code}")
    return parse_supplier_models(response.json())
