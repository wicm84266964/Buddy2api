"""Read official TraeWork storage.json. Never scans WorkBuddy CB_AUTH_DIR."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from credential_crypto import CredentialCryptoError
from providers.traework.constants import (
    AUTH_DEVICE_PREFIX,
    AUTH_STORAGE_KEY,
    CHANNEL_ID,
    IDE_VERSION,
    STORAGE_FILENAME,
    UG_API,
)
from providers.traework.crypto import decrypt_tc_b64


APP_SUPPORT_NAME = "TRAE SOLO CN"


def traework_user_data_dir() -> Path:
    override = os.environ.get("CB_TRAEWORK_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_SUPPORT_NAME
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(appdata) / APP_SUPPORT_NAME
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return xdg / APP_SUPPORT_NAME


def traework_auth_dirs() -> list[Path]:
    dirs: list[Path] = []
    explicit = os.environ.get("CB_TRAEWORK_AUTH_DIR", "").strip()
    if explicit:
        dirs.append(Path(explicit).expanduser())
    dirs.append(traework_user_data_dir() / "User" / "globalStorage")
    seen: set[str] = set()
    out: list[Path] = []
    for item in dirs:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def iso_to_ms(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1000
    text = str(value).strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _jwt_exp_ms(token: str) -> int:
    try:
        import base64

        parts = token.split(".")
        if len(parts) < 2:
            return 0
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return iso_to_ms(data.get("exp"))
    except Exception:
        return 0


def _read_storage(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise CredentialCryptoError("TraeWork storage.json is not an object")
    return payload


def _device_keys(storage: dict) -> tuple[str, str, str, str]:
    public_pem = ""
    private_pem = ""
    device_id = ""
    for key, value in storage.items():
        if not str(key).startswith(AUTH_DEVICE_PREFIX):
            continue
        device_id = str(key)[len(AUTH_DEVICE_PREFIX) :]
        if not isinstance(value, str):
            continue
        try:
            blob = json.loads(decrypt_tc_b64(value))
        except Exception:
            continue
        if isinstance(blob, dict):
            public_pem = str(blob.get("publicKeyPEM") or "")
            private_pem = str(blob.get("privateKeyPEM") or "")
            break
    machine = str(storage.get("telemetry.machineId") or "")
    return device_id, public_pem, private_pem, machine


def session_to_account(document: dict, storage: dict | None = None, source: str = "") -> dict:
    account = document.get("account") if isinstance(document.get("account"), dict) else {}
    uid = str(document.get("userId") or account.get("userId") or "")
    name = str(account.get("username") or document.get("username") or "")
    access = str(document.get("token") or document.get("access_token") or "")
    refresh = str(document.get("refreshToken") or document.get("refresh_token") or "")
    host = str(document.get("host") or UG_API)
    device_id = public_pem = private_pem = machine_id = ""
    if storage:
        device_id, public_pem, private_pem, machine_id = _device_keys(storage)
    extra = {
        "host": host,
        "device_id": device_id,
        "machine_id": machine_id,
        "public_key_pem": public_pem,
        "private_key_pem": private_pem,
        "user_tag": str(account.get("userTag") or ""),
        "store_region": str(account.get("storeRegion") or ""),
        "source": source or "import",
        "client_version": IDE_VERSION,
    }
    return {
        "name": name or f"traework-{(uid or 'user')[:8]}",
        "uid": uid,
        "nickname": name,
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": iso_to_ms(document.get("expiredAt")) or _jwt_exp_ms(access),
        "refresh_expires_at": iso_to_ms(document.get("refreshExpiredAt")),
        "provider": CHANNEL_ID,
        "domain": host.replace("https://", "").replace("http://", ""),
        "extra": extra,
        "status": "active",
    }


def parse_credentials(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("TraeWork credentials must be a JSON object")
    nested = body.get("account") if isinstance(body.get("account"), dict) else {}
    document = {
        "token": body.get("token") or body.get("access_token") or "",
        "refreshToken": body.get("refreshToken") or body.get("refresh_token") or "",
        "expiredAt": body.get("expiredAt") or body.get("expires_at"),
        "refreshExpiredAt": body.get("refreshExpiredAt") or body.get("refresh_expires_at"),
        "userId": body.get("userId") or body.get("uid") or nested.get("userId") or "",
        "host": body.get("host") or UG_API,
        "account": {
            "username": nested.get("username") or body.get("nickname") or body.get("name") or "",
            "userTag": nested.get("userTag") or "",
            "storeRegion": nested.get("storeRegion") or "",
        },
    }
    parsed = session_to_account(document, source="paste")
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["device_id"] = str(body.get("device_id") or extra.get("device_id") or "")
    extra["machine_id"] = str(body.get("machine_id") or extra.get("machine_id") or "")
    extra["public_key_pem"] = str(body.get("public_key_pem") or extra.get("public_key_pem") or "")
    extra["private_key_pem"] = str(body.get("private_key_pem") or extra.get("private_key_pem") or "")
    parsed["extra"] = extra
    if not parsed["access_token"] and not parsed["refresh_token"]:
        raise ValueError("TraeWork credentials need token / refreshToken")
    return parsed


def import_discovered(path: str) -> dict:
    target = Path(path)
    allowed = [folder.resolve() for folder in traework_auth_dirs() if folder.exists()]
    resolved = target.resolve()
    if allowed and not any(_is_relative_to(resolved, root) for root in allowed):
        raise ValueError("path is outside CB_TRAEWORK_AUTH_DIR / official TraeWork data dir")
    if target.suffix.lower() == ".json" and target.name != STORAGE_FILENAME:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return parse_credentials(payload if isinstance(payload, dict) else {})
    storage = _read_storage(target)
    blob = storage.get(AUTH_STORAGE_KEY)
    if not isinstance(blob, str) or not blob:
        raise ValueError("TraeWork storage.json has no iCubeAuthInfo")
    document = json.loads(decrypt_tc_b64(blob))
    if not isinstance(document, dict):
        raise ValueError("TraeWork auth JSON is not an object")
    parsed = session_to_account(document, storage=storage, source=str(resolved))
    extra = parsed.get("extra") if isinstance(parsed.get("extra"), dict) else {}
    extra["auth_path"] = str(resolved)
    parsed["extra"] = extra
    if not parsed.get("access_token") and not parsed.get("refresh_token"):
        raise ValueError("failed to decrypt official TraeWork storage.json")
    return parsed


def discover() -> dict:
    import database as db

    dirs_info = []
    files: list[dict] = []
    existing = {
        str(row.get("uid"))
        for row in db.list_accounts(provider=CHANNEL_ID)
        if row.get("uid")
    }
    for folder in traework_auth_dirs():
        exists = folder.is_dir()
        dirs_info.append({"path": str(folder), "exists": exists, "file_count": 0})
        if not exists:
            continue
        count = 0
        path = folder / STORAGE_FILENAME
        if path.is_file():
            count += 1
            files.append(_file_meta(path, existing))
        dirs_info[-1]["file_count"] = count
    return {
        "dirs": dirs_info,
        "files": files,
        "file_count": len(files),
        "valid_count": sum(1 for item in files if item.get("valid")),
        "importable_count": sum(
            1 for item in files if item.get("valid") and not item.get("already_imported")
        ),
        "channel": CHANNEL_ID,
    }


def _file_meta(path: Path, existing_uids: set[str]) -> dict:
    reason = ""
    valid = False
    uid = ""
    name = path.name
    try:
        parsed = import_discovered(str(path))
        valid = bool(parsed.get("access_token") or parsed.get("refresh_token"))
        uid = str(parsed.get("uid") or "")
        name = parsed.get("nickname") or name
        if not valid:
            reason = "missing token"
    except Exception as exc:
        reason = str(exc)[:160]
        valid = False
    masked = (uid[:6] + "…") if len(uid) > 6 else uid
    return {
        "channel": CHANNEL_ID,
        "path": str(path),
        "valid": valid,
        "reason": reason,
        "account_name": name,
        "uid_masked": masked,
        "already_imported": bool(uid and uid in existing_uids),
    }


def upsert_account(parsed: dict) -> dict:
    import database as db

    uid = str(parsed.get("uid") or "")
    if uid:
        for row in db.list_accounts(provider=CHANNEL_ID):
            if str(row.get("uid") or "") == uid:
                patch = {
                    "access_token": parsed.get("access_token") or "",
                    "refresh_token": parsed.get("refresh_token") or "",
                    "expires_at": parsed.get("expires_at") or 0,
                    "refresh_expires_at": parsed.get("refresh_expires_at") or 0,
                    "nickname": parsed.get("nickname") or row.get("nickname") or "",
                    "name": parsed.get("name") or row.get("name") or "",
                    "extra": parsed.get("extra") or row.get("extra") or {},
                    "status": "active",
                }
                db.update_account(row["id"], patch)
                return {"id": row["id"], "updated": True}
    aid = db.add_account(parsed)
    return {"id": aid, "updated": False}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
