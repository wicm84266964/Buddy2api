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
    dirs = []
    for d in candidate_auth_dirs(auth_dir):
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
        "total_credits": round(account.get("total_credits", 0), 4),
        "last_used_at": account.get("last_used_at"),
    }


def check_all_accounts() -> list[dict]:
    """检查所有账号状态。"""
    accounts = db.list_accounts()
    return [get_account_status(a) for a in accounts]
