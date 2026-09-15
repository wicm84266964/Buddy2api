"""TraeWork token refresh via ExchangeToken. Isolated from WorkBuddy."""

from __future__ import annotations

import os
import sys
import time
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from providers.traework.constants import (
    CLIENT_ID,
    EXCHANGE_PATH,
    IDE_VERSION,
    PLATFORM_CODE,
    UG_API,
)
from providers.traework.store import iso_to_ms


class TraeWorkAuthError(RuntimeError):
    pass


def is_token_expired(account: dict, skew_ms: int = 300_000) -> bool:
    expires_at = int(account.get("expires_at") or 0)
    if expires_at <= 0:
        return False
    return time.time() * 1000 >= expires_at - skew_ms


def extra_of(account: dict) -> dict:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    return extra


def auth_headers(account: dict) -> dict[str, str]:
    extra = extra_of(account)
    token = str(account.get("access_token") or "")
    device_id = str(extra.get("device_id") or "")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Cloud-IDE-JWT {token}",
        "User-Agent": f"TRAE-SOLO-CN/{IDE_VERSION}",
    }
    if device_id:
        headers["x-device-id"] = device_id
    return headers


def oauth_headers(token: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "x-cloudide-token": token}


def _host(account: dict) -> str:
    extra = extra_of(account)
    host = str(extra.get("host") or UG_API).rstrip("/")
    return host or UG_API


def _os_info() -> str:
    override = os.environ.get("CB_TRAEWORK_OS_INFO", "").strip()
    if override:
        return override
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux"):
        return "linux"
    return "windows"


def _device_name() -> str:
    override = os.environ.get("CB_TRAEWORK_DEVICE_NAME", "").strip()
    if override:
        return override
    if sys.platform == "win32":
        return os.environ.get("COMPUTERNAME") or os.environ.get("USERNAME") or "PC"
    return os.environ.get("USER") or "Mac"


def _device_info(account: dict) -> dict:
    extra = extra_of(account)
    return {
        "DeviceID": str(extra.get("device_id") or ""),
        "MachineID": str(extra.get("machine_id") or ""),
        "PlatformCode": PLATFORM_CODE,
        "DeviceType": "PC",
        "DeviceName": _device_name(),
        "ClientVersion": IDE_VERSION,
        "DevicePublicKey": str(extra.get("public_key_pem") or ""),
        "OSInfo": _os_info(),
    }


def _device_proof(refresh_token: str, private_pem: str) -> dict:
    timestamp = int(time.time())
    nonce = os.urandom(16).hex()
    material = "\n".join(
        ["POST", EXCHANGE_PATH, CLIENT_ID, refresh_token, str(timestamp), nonce]
    )
    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TraeWorkAuthError("TraeWork device key is not ECDSA")
    der = key.sign(material.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    import base64

    signature = base64.b64encode(der).decode("ascii")
    return {"Signature": signature, "Timestamp": timestamp, "Nonce": nonce}


def _parse_exchange(result: dict) -> dict:
    token = str(result.get("Token") or result.get("token") or "")
    refresh = str(result.get("RefreshToken") or result.get("refreshToken") or "")
    expired_at = iso_to_ms(result.get("TokenExpireAt") or result.get("expiredAt"))
    if not expired_at and result.get("TokenExpireDuration"):
        expired_at = int(time.time() * 1000) + int(result["TokenExpireDuration"])
    refresh_expired = iso_to_ms(result.get("RefreshExpireAt") or result.get("refreshExpiredAt"))
    return {
        "access_token": token,
        "refresh_token": refresh,
        "expires_at": expired_at,
        "refresh_expires_at": refresh_expired,
    }


async def refresh_account(account: dict) -> dict:
    import database as db

    refresh = str(account.get("refresh_token") or "")
    extra = extra_of(account)
    private_pem = str(extra.get("private_key_pem") or "")
    if not refresh:
        raise TraeWorkAuthError("TraeWork account has no refresh_token")
    if not private_pem:
        raise TraeWorkAuthError("TraeWork account has no device private key")
    access = str(account.get("access_token") or "")
    proof = _device_proof(refresh, private_pem)
    body = {
        "ClientID": CLIENT_ID,
        "ClientSecret": "",
        "RefreshToken": refresh,
        "DeviceInfo": _device_info(account),
        "DeviceProof": proof,
        "IDEVersion": IDE_VERSION,
    }
    url = f"{_host(account)}{EXCHANGE_PATH}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=oauth_headers(access), json=body)
    if response.status_code >= 400:
        raise TraeWorkAuthError(f"ExchangeToken failed: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise TraeWorkAuthError("ExchangeToken returned non-JSON") from exc
    result = data.get("Result") if isinstance(data.get("Result"), dict) else data
    parsed = _parse_exchange(result if isinstance(result, dict) else {})
    if not parsed.get("access_token"):
        raise TraeWorkAuthError("ExchangeToken response missing Token")
    patch = {**parsed, "status": "active"}
    if not patch.get("refresh_token"):
        patch["refresh_token"] = refresh
    db.update_account(int(account["id"]), patch)
    fresh = db.get_account(account["id"])
    return fresh or {**account, **patch}
