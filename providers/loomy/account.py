"""Password login against the iFlytek account service."""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import httpx

from tls_context import TLS_CONTEXT
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

from providers.loomy.constants import (
    ACCESS_KEY_ID,
    ACCESS_KEY_SECRET,
    ACCOUNT_APPID,
    ACCOUNT_BASE,
    CLIENT_UA,
    CLIENT_VERSION,
    DEVICE_ID,
    SESSION_SECONDS,
    WEB_MODEL_ID,
)
from providers.loomy.signer import build_headers


class LoomyAuthError(RuntimeError):
    pass


def new_identity() -> dict[str, Any]:
    return {
        "devid": f"web-{uuid.uuid4().hex[:16]}",
        "ua": CLIENT_UA,
        "modelid": WEB_MODEL_ID,
        "version": CLIENT_VERSION,
        "created_at": int(time.time()),
    }


def _base(identity: dict | None) -> dict[str, str]:
    ident = identity if isinstance(identity, dict) else {}
    return {
        "appid": ACCOUNT_APPID,
        "modelid": str(ident.get("modelid") or WEB_MODEL_ID),
        "version": str(ident.get("version") or CLIENT_VERSION),
        "devid": str(ident.get("devid") or DEVICE_ID),
        "ua": str(ident.get("ua") or CLIENT_UA),
        "traceid": uuid.uuid4().hex,
    }


def rsa_encrypt(pukey_b64: str, password: str) -> str:
    raw = base64.b64decode(pukey_b64 + "=" * (-len(pukey_b64) % 4))
    key = load_der_public_key(raw)
    cipher = key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode()


async def _call(path: str, body: dict) -> dict:
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = build_headers(ACCESS_KEY_ID, ACCESS_KEY_SECRET, method="POST", path=path, body=text)
    url = f"{ACCOUNT_BASE}{path}"
    try:
        async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30.0) as client:
            response = await client.post(url, headers=headers, content=text.encode("utf-8"))
    except httpx.HTTPError as exc:
        raise LoomyAuthError(f"account service unreachable: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise LoomyAuthError(f"account service returned HTTP {response.status_code}") from exc
    code = str(payload.get("code") or payload.get("errorCode") or "")
    if code and code != "000000":
        message = payload.get("desc") or payload.get("message") or code
        raise LoomyAuthError(f"{path} failed: {message}")
    if response.status_code >= 400:
        raise LoomyAuthError(f"{path} HTTP {response.status_code}")
    return payload if isinstance(payload, dict) else {}


def _session_from(payload: dict) -> dict[str, str]:
    node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(node, dict):
        raise LoomyAuthError("login response has no session")
    session = str(node.get("session") or "")
    if not session:
        raise LoomyAuthError("login response has no session")
    return {
        "session": session,
        "userid": str(node.get("userid") or node.get("userId") or ""),
        "phone": str(node.get("phone") or ""),
    }


async def login_by_password(loginid: str, password: str, identity: dict | None = None) -> dict[str, str]:
    ident = identity if isinstance(identity, dict) else new_identity()
    key_payload = await _call("/login/account/getPuKey", {"base": _base(ident)})
    data = key_payload.get("data") if isinstance(key_payload.get("data"), dict) else {}
    pukey = str(data.get("pukey") or "")
    rcode = str(data.get("rcode") or "")
    if not pukey or not rcode:
        raise LoomyAuthError("getPuKey returned no public key")
    logged = await _call(
        "/login/account/byPwd",
        {
            "base": _base(ident),
            "param": {
                "loginid": loginid,
                "password": rsa_encrypt(pukey, password),
                "rcode": rcode,
                "type": 1,
                "expire": SESSION_SECONDS,
            },
        },
    )
    got = _session_from(logged)
    if not got["phone"]:
        got["phone"] = loginid
    return got
