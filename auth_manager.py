"""
auth_manager.py — 多账号凭据管理

功能：
  - 从本机 auth 文件扫描导入账号
  - 手动添加账号（粘贴 auth JSON）
  - Token 自动刷新（提前 60s 判定过期）
  - 账号轮换（最少使用优先，负载均衡）
  - 凭据缓存与线程安全
"""

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

import database as db

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "codebuddy-gateway/1.0"

_lock = threading.Lock()
_token_locks: dict[int, threading.Lock] = {}


def _get_token_lock(aid: int) -> threading.Lock:
    if aid not in _token_locks:
        _token_locks[aid] = threading.Lock()
    return _token_locks[aid]


# ============================================================
# Auth 文件扫描
# ============================================================

def _expand_auth_path(path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    value = str(path).strip().strip('"')
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for p in paths:
        key = str(p.resolve(strict=False))
        if os.name == "nt":
            key = key.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def _mask_value(value: str, left: int = 6, right: int = 4) -> str:
    value = value or ""
    if not value:
        return ""
    if len(value) <= left + right:
        return value[:2] + "..." if len(value) > 2 else "***"
    return f"{value[:left]}...{value[-right:]}"


def candidate_auth_dirs(auth_dir: Optional[str] = None) -> list[Path]:
    """返回会被扫描的 auth 目录候选项，包括不存在的路径。"""
    custom = _expand_auth_path(auth_dir)
    if custom:
        return [custom.parent if custom.suffix.lower() == ".info" else custom]

    explicit = _expand_auth_path(os.environ.get("CB_AUTH_DIR"))
    if explicit:
        return [explicit.parent if explicit.suffix.lower() == ".info" else explicit]

    home = Path.home()
    plat = sys.platform
    dirs = []
    if plat == "darwin":
        dirs.append(home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs.append(local / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    dirs.append(xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    return _dedupe_paths(dirs)


def scan_auth_dirs(auth_dir: Optional[str] = None) -> list[Path]:
    """返回所有存在的 auth 目录路径。"""
    return [d for d in candidate_auth_dirs(auth_dir) if d.is_dir()]


def find_auth_files(auth_dir: Optional[str] = None) -> list[Path]:
    """扫描所有 auth 目录下的 *.info 文件。"""
    custom = _expand_auth_path(auth_dir)
    if custom and custom.is_file():
        return [custom] if custom.suffix.lower() == ".info" else []

    files = []
    for d in scan_auth_dirs(auth_dir):
        try:
            files.extend(sorted(d.glob("*.info")))
        except OSError:
            continue
    return _dedupe_paths(files)


def _safe_auth_file_meta(path: Path, existing_uids: set[str]) -> dict:
    meta = {
        "name": path.name,
        "path": str(path),
        "dir": str(path.parent),
        "size": 0,
        "mtime": None,
        "valid": False,
        "reason": "",
        "account_name": "",
        "uid_masked": "",
        "domain": "",
        "expires_at": 0,
        "already_imported": False,
    }
    try:
        st = path.stat()
        meta["size"] = st.st_size
        meta["mtime"] = int(st.st_mtime)
    except OSError as e:
        meta["reason"] = f"无法读取文件: {e}"
        return meta

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        meta["reason"] = "不是有效 JSON"
        return meta
    except OSError as e:
        meta["reason"] = f"无法读取文件: {e}"
        return meta

    account = data.get("account", {}) if isinstance(data, dict) else {}
    auth = data.get("auth", {}) if isinstance(data, dict) else {}
    if not auth.get("accessToken"):
        meta["reason"] = "未发现 accessToken"
        return meta

    uid = account.get("uid", "")
    meta.update({
        "valid": True,
        "reason": "ok",
        "account_name": account.get("nickname", "") or path.stem,
        "uid_masked": _mask_value(uid),
        "domain": auth.get("domain", DEFAULT_DOMAIN),
        "expires_at": auth.get("expiresAt", 0),
        "already_imported": bool(uid and uid in existing_uids),
    })
    return meta


def discover_auth_files(auth_dir: Optional[str] = None) -> dict:
    """返回本机 auth 文件的安全元信息，不返回任何 token 内容。"""
    candidates = candidate_auth_dirs(auth_dir)
    existing_dirs = [d for d in candidates if d.is_dir()]
    visible_dirs = existing_dirs or candidates

    dirs = []
    for d in visible_dirs:
        info_files = []
        exists = d.is_dir()
        if exists:
            try:
                info_files = sorted(d.glob("*.info"))
            except OSError:
                info_files = []
        dirs.append({
            "path": str(d),
            "exists": exists,
            "file_count": len(info_files),
        })

    existing_uids = {a.get("uid", "") for a in db.list_accounts() if a.get("uid")}
    files = [_safe_auth_file_meta(f, existing_uids) for f in find_auth_files(auth_dir)]
    return {
        "dirs": dirs,
        "files": files,
        "file_count": len(files),
        "valid_count": sum(1 for f in files if f.get("valid")),
        "importable_count": sum(
            1 for f in files if f.get("valid") and not f.get("already_imported")
        ),
    }


def parse_auth_file(path: Path) -> Optional[dict]:
    """解析 auth 文件，返回结构化凭据。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    account = data.get("account", {})
    auth = data.get("auth", {})
    if not auth.get("accessToken"):
        return None

    return {
        "name": account.get("nickname", "") or path.stem,
        "uid": account.get("uid", ""),
        "nickname": account.get("nickname", ""),
        "phone": account.get("phoneNumber", ""),
        "account_type": account.get("type", "personal"),
        "access_token": auth.get("accessToken", ""),
        "refresh_token": auth.get("refreshToken", ""),
        "expires_at": auth.get("expiresAt", 0),
        "refresh_expires_at": auth.get("refreshExpiresAt", 0),
        "domain": auth.get("domain", DEFAULT_DOMAIN),
        "enterprise_id": account.get("enterpriseId", ""),
        "session_state": auth.get("sessionState", ""),
    }


def import_auth_file(path: Path) -> Optional[int]:
    """扫描并导入 auth 文件到数据库。如果 uid 已存在则更新。"""
    parsed = parse_auth_file(path)
    if not parsed:
        return None

    # 检查是否已存在（按 uid 去重）
    existing = db.list_accounts()
    for acc in existing:
        if acc.get("uid") == parsed["uid"]:
            db.update_account(acc["id"], parsed)
            return acc["id"]

    return db.add_account(parsed)


def auto_scan_and_import(auth_dir: Optional[str] = None) -> dict:
    """自动扫描本机 auth 文件并导入。返回 {imported, updated, skipped}。"""
    result = {"imported": 0, "updated": 0, "skipped": 0, "errors": []}
    for f in find_auth_files(auth_dir):
        parsed = parse_auth_file(f)
        if not parsed:
            result["skipped"] += 1
            continue
        existing = db.list_accounts()
        found = False
        for acc in existing:
            if acc.get("uid") == parsed["uid"]:
                db.update_account(acc["id"], parsed)
                result["updated"] += 1
                found = True
                break
        if not found:
            db.add_account(parsed)
            result["imported"] += 1
    return result


# ============================================================
# Token 刷新
# ============================================================

def refresh_token(account: dict) -> bool:
    """调后端刷新 token，写回数据库。返回是否成功。"""
    aid = account["id"]
    lock = _get_token_lock(aid)
    with lock:
        headers = build_headers(account)
        headers["X-Refresh-Token"] = account.get("refresh_token", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"

        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            print(f"[auth_manager] 刷新 token 网络失败 (account={aid}): {e}", file=sys.stderr)
            return False

        if data.get("code") != 0 or not data.get("data"):
            print(f"[auth_manager] 刷新 token 失败 (account={aid}): {data.get('msg', data)}", file=sys.stderr)
            # 标记账号为过期
            db.update_account(aid, {"status": "expired"})
            return False

        new_auth = data["data"]
        now_ms = int(time.time() * 1000)
        next_status = "inactive" if account.get("status") == "inactive" else "active"
        update_data = {
            "access_token": new_auth.get("accessToken", ""),
            "refresh_token": new_auth.get("refreshToken", ""),
            "expires_at": new_auth.get("expiresAt") or (
                now_ms + new_auth.get("expiresIn", 0) * 1000
            ),
            "refresh_expires_at": new_auth.get("refreshExpiresAt") or (
                now_ms + new_auth.get("refreshExpiresIn", 0) * 1000
            ),
            "domain": new_auth.get("domain", DEFAULT_DOMAIN),
            "status": next_status,
        }
        db.update_account(aid, update_data)
        return True


def is_token_expired(account: dict) -> bool:
    expires_at = account.get("expires_at", 0)
    if not expires_at:
        return True
    return time.time() * 1000 >= (expires_at - 60_000)


def ensure_token_valid(account: dict) -> bool:
    """如果 token 快过期则刷新。返回是否有效。"""
    if not is_token_expired(account):
        return True
    return refresh_token(account)


# ============================================================
# Header 构造
# ============================================================

def build_headers(account: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {account.get('access_token', '')}",
        "X-User-Id": account.get("uid", ""),
        "X-Enterprise-Id": account.get("enterprise_id", ""),
        "X-Tenant-Id": account.get("enterprise_id", ""),
        "X-Domain": account.get("domain", DEFAULT_DOMAIN),
        "User-Agent": USER_AGENT,
    }


def get_valid_headers(account: dict) -> Optional[dict]:
    """确保 token 有效后返回 header。失败返回 None。"""
    if not ensure_token_valid(account):
        return None
    # 重新从数据库读取最新凭据
    fresh = db.get_account(account["id"])
    if not fresh:
        return None
    return build_headers(fresh)


# ============================================================
# 每日积分领取
# ============================================================

def _checkin_result(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    payload: Optional[dict] = None,
    claimed: bool = False,
    already_claimed: bool = False,
) -> dict:
    payload = payload or {}
    credit = payload.get("credit", payload.get("today_credit", 0)) or 0
    try:
        credit = float(credit)
    except (TypeError, ValueError):
        credit = 0
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": payload.get("active"),
        "today_checked_in": payload.get("today_checked_in"),
        "today_credit": payload.get("today_credit"),
        "streak_days": payload.get("streak_days"),
        "is_streak_day": payload.get("is_streak_day"),
    }


def _unwrap_response(data: object) -> tuple[bool, str, dict]:
    if not isinstance(data, dict):
        return False, "响应不是 JSON 对象", {}
    code = data.get("code")
    msg = str(data.get("msg") or data.get("message") or "")
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if code not in (None, 0):
        return False, msg or f"code={code}", payload
    return True, msg or "OK", payload


# ============================================================
# 官方额度资源
# ============================================================

def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_resource_time(value) -> tuple[int | None, str]:
    """把官方资源时间统一为秒级时间戳和原始可读字符串。"""
    if value in (None, "", 0, "0", "9999-99-99 99:99:99"):
        return None, str(value or "")

    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000
        return int(raw), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw))

    text = str(value).strip()
    if not text:
        return None, ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return int(dt.timestamp()), text
        except ValueError:
            continue
    return None, text


def _safe_resource_item(item: dict, now_ts: int) -> dict:
    cycle_end_ts, cycle_end = _parse_resource_time(item.get("CycleEndTime"))
    cycle_start_ts, cycle_start = _parse_resource_time(item.get("CycleStartTime"))
    deduction_end_ts, deduction_end = _parse_resource_time(item.get("DeductionEndTime"))
    deduction_start_ts, deduction_start = _parse_resource_time(item.get("DeductionStartTime"))
    expired_ts, expired_time = _parse_resource_time(item.get("ExpiredTime"))

    remain = _to_float(item.get("CapacityRemainPrecise"), _to_float(item.get("CapacityRemain")))
    used = _to_float(item.get("CapacityUsedPrecise"), _to_float(item.get("CapacityUsed")))
    size = _to_float(item.get("CapacitySizePrecise"), _to_float(item.get("CapacitySize")))
    cycle_remain = _to_float(item.get("CycleCapacityRemainPrecise"), _to_float(item.get("CycleCapacityRemain")))
    cycle_used = _to_float(item.get("CycleCapacityUsedPrecise"), _to_float(item.get("CycleCapacityUsed")))
    cycle_size = _to_float(item.get("CycleCapacitySizePrecise"), _to_float(item.get("CycleCapacitySize"), size))
    effective_remain = cycle_remain if cycle_remain > 0 or cycle_used > 0 else remain

    expire_ts = expired_ts or deduction_end_ts or cycle_end_ts
    days_to_expire = None
    if expire_ts:
        days_to_expire = int((expire_ts - now_ts) / 86400)

    package_name = str(item.get("PackageName") or item.get("DealName") or item.get("ProductName") or "额度包")
    product_name = str(item.get("ProductName") or item.get("SubProductName") or "")
    status = _to_int(item.get("Status"))
    is_expired = bool(expire_ts and expire_ts < now_ts)

    return {
        "package_name": package_name,
        "product_name": product_name,
        "package_type": str(item.get("PackageType") or ""),
        "resource_type": str(item.get("ResourceType") or ""),
        "capacity_unit": str(item.get("CapacityUnit") or item.get("OriginUnit") or "credit"),
        "status": status,
        "remaining": round(remain, 4),
        "remaining_precise": round(effective_remain, 4),
        "used": round(used, 4),
        "size": round(size, 4),
        "cycle_remaining": round(cycle_remain, 4),
        "cycle_used": round(cycle_used, 4),
        "cycle_size": round(cycle_size, 4),
        "cycle_start": cycle_start,
        "cycle_start_ts": cycle_start_ts,
        "cycle_end": cycle_end,
        "cycle_end_ts": cycle_end_ts,
        "deduction_start": deduction_start,
        "deduction_start_ts": deduction_start_ts,
        "deduction_end": deduction_end,
        "deduction_end_ts": deduction_end_ts,
        "expired_time": expired_time,
        "expired_ts": expired_ts,
        "expire_ts": expire_ts,
        "expire_time": expired_time or deduction_end or cycle_end,
        "days_to_expire": days_to_expire,
        "expired": is_expired,
        "auto_renew": bool(_to_int(item.get("AutoRenewFlag"))),
        "remain_cycles": _to_int(item.get("RemainCycles")),
        "total_cycles": _to_int(item.get("TotalCycles")),
    }


def _resource_failure(
    account: dict,
    *,
    message: str,
    status_code: int = 0,
    allow_stale: bool = True,
) -> dict:
    cached = db.get_account_resource_cache(account.get("id")) if allow_stale and account.get("id") else None
    if cached:
        cached["stale"] = True
        cached["message"] = message
        cached["status_code"] = status_code
        return cached
    return {
        "ok": False,
        "status_code": status_code,
        "message": message,
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "total_dosage": 0,
        "resource_count": 0,
        "package_count": 0,
        "active_package_count": 0,
        "expired_package_count": 0,
        "expiring_package_count": 0,
        "available_total": 0,
        "expiring_7d_total": 0,
        "expiring_30d_total": 0,
        "next_expire_time": "",
        "next_expire_ts": None,
        "next_expire_amount": 0,
        "next_expire_days": None,
        "updated_at": int(time.time()),
        "cached": False,
        "stale": False,
        "age_seconds": 0,
        "packages": [],
        "expiring_packages": [],
    }


def fetch_account_resources(
    account: dict,
    *,
    force: bool = False,
    max_age_seconds: int = 60,
    allow_stale: bool = True,
) -> dict:
    """查询官方额度资源，只返回安全摘要和额度包明细。"""
    if account.get("id") and not force:
        cached = db.get_account_resource_cache(account["id"])
        if cached and int(cached.get("age_seconds") or 0) <= max_age_seconds:
            cached["stale"] = False
            return cached

    headers = get_valid_headers(account)
    if not headers:
        return _resource_failure(
            account,
            message="token refresh failed or account credentials are invalid",
            allow_stale=allow_stale,
        )

    try:
        with httpx.Client(timeout=25) as c:
            r = c.post(f"{BACKEND}/v2/billing/meter/get-user-resource", headers=headers, json={})
            data = r.json()
    except Exception as e:
        return _resource_failure(
            account,
            message=str(e)[:240],
            allow_stale=allow_stale,
        )

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False

    response = payload.get("Response") if isinstance(payload.get("Response"), dict) else {}
    raw_data = response.get("Data") if isinstance(response.get("Data"), dict) else {}
    raw_items = raw_data.get("Accounts") if isinstance(raw_data.get("Accounts"), list) else []
    now_ts = int(time.time())
    packages = [_safe_resource_item(x, now_ts) for x in raw_items if isinstance(x, dict)]
    packages.sort(key=lambda x: (
        x.get("expired", False),
        x.get("expire_ts") or 9_999_999_999,
        -float(x.get("remaining_precise") or 0),
    ))

    active_packages = [p for p in packages if not p.get("expired")]
    expiring_packages = [
        p for p in active_packages
        if p.get("expire_ts") and 0 <= (p["expire_ts"] - now_ts) <= 7 * 86400
        and float(p.get("remaining_precise") or 0) > 0
    ]
    expiring_30d_packages = [
        p for p in active_packages
        if p.get("expire_ts") and 0 <= (p["expire_ts"] - now_ts) <= 30 * 86400
        and float(p.get("remaining_precise") or 0) > 0
    ]
    next_expiring = next(
        (
            p for p in active_packages
            if p.get("expire_ts") and float(p.get("remaining_precise") or 0) > 0
        ),
        None,
    )
    expired_packages = [p for p in packages if p.get("expired")]

    result = {
        "ok": ok,
        "status_code": r.status_code,
        "message": msg,
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "total_dosage": round(_to_float(raw_data.get("TotalDosage")), 4),
        "resource_count": _to_int(raw_data.get("TotalCount"), len(packages)),
        "package_count": len(packages),
        "active_package_count": len(active_packages),
        "expired_package_count": len(expired_packages),
        "expiring_package_count": len(expiring_packages),
        "available_total": round(sum(float(p.get("remaining_precise") or 0) for p in active_packages), 4),
        "expiring_7d_total": round(sum(float(p.get("remaining_precise") or 0) for p in expiring_packages), 4),
        "expiring_30d_total": round(sum(float(p.get("remaining_precise") or 0) for p in expiring_30d_packages), 4),
        "next_expire_time": next_expiring.get("expire_time") if next_expiring else "",
        "next_expire_ts": next_expiring.get("expire_ts") if next_expiring else None,
        "next_expire_amount": round(float(next_expiring.get("remaining_precise") or 0), 4) if next_expiring else 0,
        "next_expire_days": next_expiring.get("days_to_expire") if next_expiring else None,
        "updated_at": now_ts,
        "cached": False,
        "stale": False,
        "age_seconds": 0,
        "packages": packages,
        "expiring_packages": expiring_packages,
    }
    if ok and account.get("id"):
        db.upsert_account_resource_cache(account["id"], result)
    if not ok:
        return _resource_failure(
            account,
            message=msg,
            status_code=r.status_code,
            allow_stale=allow_stale,
        )
    return result


def _checkin_failure(
    account: dict,
    *,
    message: str,
    status_code: int = 0,
    allow_stale: bool = True,
) -> dict:
    cached = db.get_account_checkin_cache(account.get("id")) if allow_stale and account.get("id") else None
    if cached:
        cached["stale"] = True
        cached["message"] = message
        cached["status_code"] = status_code
        return cached
    return _checkin_result(account, ok=False, status_code=status_code, message=message)


def fetch_checkin_status(
    account: dict,
    *,
    force: bool = False,
    max_age_seconds: int = 300,
    allow_stale: bool = True,
) -> dict:
    """查询每日积分领取状态。只返回安全摘要，不返回凭据。"""
    if account.get("id") and not force:
        cached = db.get_account_checkin_cache(account["id"])
        if cached and int(cached.get("age_seconds") or 0) <= max_age_seconds:
            cached["stale"] = False
            return cached

    headers = get_valid_headers(account)
    if not headers:
        return _checkin_failure(
            account,
            message="token refresh failed or account credentials are invalid",
            allow_stale=allow_stale,
        )

    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(f"{BACKEND}/v2/billing/meter/checkin-activity-status", headers=headers, json={})
            data = r.json()
    except Exception as e:
        return _checkin_failure(account, status_code=0, message=str(e)[:240], allow_stale=allow_stale)

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False
    result = _checkin_result(
        account,
        ok=ok,
        status_code=r.status_code,
        message=msg,
        payload=payload,
        already_claimed=bool(payload.get("today_checked_in")),
    )
    result["updated_at"] = int(time.time())
    result["cached"] = False
    result["stale"] = False
    result["age_seconds"] = 0
    if ok and account.get("id"):
        db.upsert_account_checkin_cache(account["id"], result)
    if not ok:
        return _checkin_failure(account, status_code=r.status_code, message=msg, allow_stale=allow_stale)
    return result


def claim_daily_checkin(account: dict) -> dict:
    """手动领取单个账号的每日积分。不会绕过验证或做自动定时。"""
    status = fetch_checkin_status(account, force=True, allow_stale=False)
    if not status.get("ok"):
        return status
    if status.get("active") is False:
        status["ok"] = False
        status["message"] = "活动当前不可用"
        return status
    if status.get("today_checked_in"):
        status["already_claimed"] = True
        status["message"] = "今日已领取"
        return status

    fresh = db.get_account(account["id"])
    if not fresh:
        return _checkin_result(account, ok=False, message="account not found")
    headers = get_valid_headers(fresh)
    if not headers:
        return _checkin_result(
            account,
            ok=False,
            message="token refresh failed or account credentials are invalid",
        )

    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{BACKEND}/v2/billing/meter/daily-checkin", headers=headers, json={})
            data = r.json()
    except Exception as e:
        return _checkin_result(account, ok=False, status_code=0, message=str(e)[:240])

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False
    credit = payload.get("credit", payload.get("today_credit", 0)) or 0
    try:
        credit = float(credit)
    except (TypeError, ValueError):
        credit = 0
    claimed = bool(ok and credit > 0)
    if claimed and float(fresh.get("credit_limit") or 0) > 0:
        db.update_account(fresh["id"], {"credit_limit": float(fresh.get("credit_limit") or 0) + credit})

    result = _checkin_result(
        account,
        ok=ok,
        status_code=r.status_code,
        message=("领取成功" if claimed else msg),
        payload=payload,
        claimed=claimed,
        already_claimed=bool(payload.get("today_checked_in")) and not claimed,
    )
    result["updated_at"] = int(time.time())
    result["cached"] = False
    result["stale"] = False
    result["age_seconds"] = 0
    if ok and account.get("id"):
        db.upsert_account_checkin_cache(account["id"], result)
    return result


# ============================================================
# 账号轮换（负载均衡）
# ============================================================

def pick_account(exclude_ids: set[int] = None) -> Optional[dict]:
    """选择一个可用账号。优先级越高越先用，同优先级按加权负载最低优先。"""
    exclude_ids = exclude_ids or set()
    accounts = db.get_active_accounts()
    candidates = [a for a in accounts if a["id"] not in exclude_ids]
    if not candidates:
        return None
    candidates.sort(key=lambda a: (
        -int(a.get("priority") or 0),
        (a.get("total_requests", 0) or 0) / max(1, int(a.get("weight") or 1)),
        a.get("total_requests", 0) or 0,
        a["id"],
    ))
    return candidates[0]


def pick_account_with_fallback(exclude_ids: set[int] = None) -> Optional[dict]:
    """选账号，如果全部过期则尝试刷新过期账号。"""
    account = pick_account(exclude_ids)
    if account:
        return account

    # 尝试过期账号，但不碰 inactive/disabled 账号。
    conn = db.get_conn()
    rows = conn.execute(
        """
        SELECT * FROM accounts
        WHERE status='expired'
        ORDER BY priority DESC,
                 (CAST(total_requests AS REAL) / CASE WHEN weight > 0 THEN weight ELSE 1 END) ASC,
                 total_requests ASC,
                 id ASC
        """
    ).fetchall()
    conn.close()
    for r in rows:
        a = dict(r)
        if a["id"] in (exclude_ids or set()):
            continue
        if refresh_token(a):
            return db.get_account(a["id"])
    return None


# ============================================================
# 账号状态检查
# ============================================================

def get_account_status(account: dict) -> dict:
    """返回账号状态摘要。"""
    expired = is_token_expired(account)
    now_ms = int(time.time() * 1000)
    remaining_hours = 0
    if account.get("expires_at"):
        remaining_hours = max(0, int((account["expires_at"] - now_ms) / 1000 / 3600))
    credit_snapshot = max(0.0, float(account.get("credit_limit") or 0))
    credit_baseline = max(0.0, float(account.get("credit_baseline") or 0))
    total_credits = round(float(account.get("total_credits") or 0), 4)
    credit_since_snapshot = round(max(0.0, total_credits - credit_baseline), 4)
    credit_remaining = None
    credit_used_pct = 0
    if credit_snapshot > 0:
        credit_remaining = round(max(0.0, credit_snapshot - credit_since_snapshot), 4)
        credit_used_pct = min(100, round(credit_since_snapshot / credit_snapshot * 100, 1))

    return {
        "id": account["id"],
        "name": account.get("name", ""),
        "nickname": account.get("nickname", ""),
        "uid": account.get("uid", ""),
        "status": account.get("status", "unknown"),
        "weight": int(account.get("weight") or 1),
        "priority": int(account.get("priority") or 0),
        "token_expired": expired,
        "remaining_hours": remaining_hours,
        "total_requests": account.get("total_requests", 0),
        "total_tokens": account.get("total_tokens", 0),
        "total_credits": total_credits,
        "credit_limit": round(credit_snapshot, 4),
        "credit_snapshot": round(credit_snapshot, 4),
        "credit_baseline": round(credit_baseline, 4),
        "credit_since_snapshot": credit_since_snapshot,
        "credit_remaining": credit_remaining,
        "credit_used_pct": credit_used_pct,
        "credit_source": "local_snapshot" if credit_snapshot > 0 else "usage_only",
        "last_used_at": account.get("last_used_at"),
    }


def check_all_accounts() -> list[dict]:
    """检查所有账号状态。"""
    accounts = db.list_accounts()
    return [get_account_status(a) for a in accounts]
