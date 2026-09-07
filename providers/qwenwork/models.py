"""QwenWork supplier-list fetch. COSY GET with a plaintext query."""

from __future__ import annotations

import time
import uuid

import httpx

from providers.qwenwork import cosy
from providers.qwenwork.chat import static_headers
from providers.qwenwork.constants import GATEWAY, MODELS_PATH, SCENE
from providers.qwenwork.token import QwenWorkAuthError


def parse_supplier_models(payload) -> list[dict]:
    rows = _qwork_rows(payload)
    models: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("enable") is False or row.get("isEnabled") is False:
            continue
        mid = str(row.get("key") or row.get("value") or row.get("id") or row.get("modelId") or "").strip()
        name = str(row.get("display_name") or row.get("displayName") or mid)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        item = {"id": mid, "name": name or mid}
        description = str(row.get("description") or "")
        if description:
            item["description"] = description
        models.append(item)
    return models


def _qwork_rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
    scene = data.get(SCENE) if isinstance(data, dict) else None
    if isinstance(scene, list):
        return scene
    if isinstance(scene, dict):
        for key in ("models", "list", "model_list"):
            rows = scene.get(key)
            if isinstance(rows, list):
                return rows
    if isinstance(data, dict):
        for key in ("models", "model_list", "list"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    return []


async def fetch_supplier_models(account: dict) -> list[dict]:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    url = f"{GATEWAY}{MODELS_PATH}"
    request_id = uuid.uuid4().hex
    headers = static_headers("pro", request_id, str(extra.get("login_device_id") or ""))
    headers["Accept"] = "application/json"
    headers.update(
        cosy.auth_headers(
            uid=str(account.get("uid") or extra.get("uid") or ""),
            name=str(account.get("nickname") or account.get("name") or extra.get("name") or ""),
            email=str(extra.get("email") or ""),
            access_token=str(account.get("access_token") or ""),
            url=url,
            body="",
            timestamp=int(time.time()),
            request_id=request_id,
        )
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        raise QwenWorkAuthError(f"models HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise QwenWorkAuthError("models response is not JSON") from exc
    return parse_supplier_models(payload)
