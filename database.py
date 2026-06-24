"""
database.py — SQLite 数据层

表结构：
  - accounts:   WorkBuddy 账号（auth 凭据）
  - api_keys:   客户端 API Key
  - logs:       请求日志
  - settings:   系统设置（key-value）
"""

import hashlib
import json
import sqlite3
import threading
import time
import os
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(os.environ.get("CB_GATEWAY_DB_PATH", Path(__file__).parent / "codebuddy_gateway.db"))
_lock = threading.Lock()


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _key_prefix(key: str) -> str:
    if len(key) <= 16:
        return key[:6] + "..."
    return f"{key[:12]}...{key[-4:]}"


def _today_start_ts() -> int:
    now = time.localtime()
    return int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst)))


def _load_allowed_models(value: Any) -> Optional[list]:
    if not value:
        return None
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with _lock:
        conn = get_conn()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            uid             TEXT,
            nickname        TEXT,
            phone           TEXT,
            account_type    TEXT DEFAULT 'personal',
            access_token    TEXT,
            refresh_token   TEXT,
            expires_at      INTEGER,
            refresh_expires_at INTEGER,
            domain          TEXT DEFAULT 'www.codebuddy.cn',
            enterprise_id   TEXT,
            session_state   TEXT,
            status          TEXT DEFAULT 'active',
            weight          INTEGER DEFAULT 1,
            priority        INTEGER DEFAULT 0,
            last_used_at    INTEGER,
            total_requests  INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            total_credits   REAL DEFAULT 0,
            created_at      INTEGER,
            updated_at      INTEGER
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key_prefix      TEXT,
            key_hash        TEXT UNIQUE,
            name            TEXT,
            status          TEXT DEFAULT 'active',
            allowed_models  TEXT,
            daily_limit     INTEGER DEFAULT 0,
            total_requests  INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            created_at      INTEGER,
            last_used_at    INTEGER
        );

        CREATE TABLE IF NOT EXISTS logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_id      INTEGER,
            api_key_name    TEXT,
            account_id      INTEGER,
            account_name    TEXT,
            model           TEXT,
            stream          INTEGER,
            prompt_tokens   INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            credit          REAL DEFAULT 0,
            finish_reason   TEXT,
            duration_ms     INTEGER,
            status_code     INTEGER,
            error_msg       TEXT,
            created_at      INTEGER
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_logs_api_key ON logs(api_key_id);
        CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id);
        """)
        _migrate_accounts(conn)
        _migrate_api_keys(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        conn.commit()
        conn.close()


def _migrate_accounts(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "weight" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN weight INTEGER DEFAULT 1")
    if "priority" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN priority INTEGER DEFAULT 0")
    conn.execute("UPDATE accounts SET weight=1 WHERE weight IS NULL OR weight < 1")
    conn.execute("UPDATE accounts SET priority=0 WHERE priority IS NULL")


def _migrate_api_keys(conn: sqlite3.Connection):
    """Keep older plaintext-key databases usable while moving to hash-only storage."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "key" in cols:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
        conn.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy")
        conn.execute("""
        CREATE TABLE api_keys (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key_prefix      TEXT,
            key_hash        TEXT UNIQUE,
            name            TEXT,
            status          TEXT DEFAULT 'active',
            allowed_models  TEXT,
            daily_limit     INTEGER DEFAULT 0,
            total_requests  INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            created_at      INTEGER,
            last_used_at    INTEGER
        )
        """)
        for row in rows:
            d = dict(row)
            raw_key = d.get("key") or ""
            conn.execute("""
                INSERT INTO api_keys
                    (id, key_prefix, key_hash, name, status, allowed_models, daily_limit,
                     total_requests, total_tokens, created_at, last_used_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                d.get("id"),
                d.get("key_prefix") or _key_prefix(raw_key),
                d.get("key_hash") or (_hash_api_key(raw_key) if raw_key else None),
                d.get("name", ""),
                d.get("status", "active"),
                d.get("allowed_models"),
                d.get("daily_limit") or 0,
                d.get("total_requests") or 0,
                d.get("total_tokens") or 0,
                d.get("created_at"),
                d.get("last_used_at"),
            ))
        conn.execute("DROP TABLE api_keys_legacy")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}

    if "key_prefix" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_prefix TEXT")
    if "key_hash" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_hash TEXT")
    if "daily_limit" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN daily_limit INTEGER DEFAULT 0")


# ============================================================
# Accounts
# ============================================================

def add_account(data: dict) -> int:
    now = int(time.time())
    weight = max(1, int(data.get("weight", 1) or 1))
    priority = int(data.get("priority", 0) or 0)
    with _lock:
        conn = get_conn()
        cur = conn.execute("""
            INSERT INTO accounts
                (name, uid, nickname, phone, account_type, access_token, refresh_token,
                 expires_at, refresh_expires_at, domain, enterprise_id, session_state,
                 status, weight, priority, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("name", ""),
            data.get("uid", ""),
            data.get("nickname", ""),
            data.get("phone", ""),
            data.get("account_type", "personal"),
            data.get("access_token", ""),
            data.get("refresh_token", ""),
            data.get("expires_at", 0),
            data.get("refresh_expires_at", 0),
            data.get("domain", "www.codebuddy.cn"),
            data.get("enterprise_id", ""),
            data.get("session_state", ""),
            data.get("status", "active"),
            weight,
            priority,
            now, now,
        ))
        aid = cur.lastrowid
        conn.commit()
        conn.close()
        return aid


def update_account(aid: int, data: dict):
    now = int(time.time())
    fields = []
    values = []
    for k in ["name", "uid", "nickname", "phone", "account_type", "access_token",
              "refresh_token", "expires_at", "refresh_expires_at", "domain",
              "enterprise_id", "session_state", "status", "weight", "priority"]:
        if k in data:
            if k == "weight":
                data[k] = max(1, int(data[k] or 1))
            elif k == "priority":
                data[k] = int(data[k] or 0)
            fields.append(f"{k}=?")
            values.append(data[k])
    if not fields:
        return
    fields.append("updated_at=?")
    values.append(now)
    values.append(aid)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE accounts SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
        conn.close()


def delete_account(aid: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
        conn.commit()
        conn.close()


def get_account(aid: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_accounts() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_accounts() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM accounts
        WHERE status='active'
        ORDER BY priority DESC,
                 (CAST(total_requests AS REAL) / CASE WHEN weight > 0 THEN weight ELSE 1 END) ASC,
                 total_requests ASC,
                 id ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def account_increment_usage(aid: int, tokens: int, credit: float):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute("""
            UPDATE accounts SET
                total_requests = total_requests + 1,
                total_tokens = total_tokens + ?,
                total_credits = total_credits + ?,
                last_used_at = ?,
                updated_at = ?
            WHERE id=?
        """, (tokens, credit, now, now, aid))
        conn.commit()
        conn.close()


# ============================================================
# API Keys
# ============================================================

def add_api_key(key: str, name: str, allowed_models: Optional[list] = None,
                daily_limit: Optional[int] = None) -> int:
    now = int(time.time())
    models_json = json.dumps(allowed_models) if allowed_models else None
    limit = int(daily_limit or 0)
    with _lock:
        conn = get_conn()
        cur = conn.execute("""
            INSERT INTO api_keys (key_prefix, key_hash, name, status, allowed_models, daily_limit, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (_key_prefix(key), _hash_api_key(key), name, "active", models_json, limit, now))
        kid = cur.lastrowid
        conn.commit()
        conn.close()
        return kid


def update_api_key(kid: int, data: dict):
    fields = []
    values = []
    for k in ["name", "status", "allowed_models", "daily_limit"]:
        if k in data:
            val = data[k]
            if k == "allowed_models" and isinstance(val, list):
                val = json.dumps(val) if val else None
            fields.append(f"{k}=?")
            values.append(val)
    if not fields:
        return
    values.append(kid)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE api_keys SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
        conn.close()


def delete_api_key(kid: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM api_keys WHERE id=?", (kid,))
        conn.commit()
        conn.close()


def get_api_key_by_key(key: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash=? AND status='active'", (_hash_api_key(key),)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d.pop("key_hash", None)
    d.pop("key", None)
    d["allowed_models"] = _load_allowed_models(d.get("allowed_models"))
    return d


def list_api_keys() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM api_keys ORDER BY id DESC").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d.pop("key_hash", None)
        d.pop("key", None)
        d["allowed_models"] = _load_allowed_models(d.get("allowed_models"))
        d["today_requests"] = get_api_key_daily_requests(d["id"])
        result.append(d)
    return result


def get_api_key_daily_requests(kid: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM logs WHERE api_key_id=? AND created_at>=?",
        (kid, _today_start_ts()),
    ).fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def api_key_increment_usage(kid: int, tokens: int):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute("""
            UPDATE api_keys SET
                total_requests = total_requests + 1,
                total_tokens = total_tokens + ?,
                last_used_at = ?
            WHERE id=?
        """, (tokens, now, kid))
        conn.commit()
        conn.close()


# ============================================================
# Logs
# ============================================================

def add_log(data: dict):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO logs
                (api_key_id, api_key_name, account_id, account_name, model, stream,
                 prompt_tokens, completion_tokens, total_tokens, credit,
                 finish_reason, duration_ms, status_code, error_msg, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("api_key_id"), data.get("api_key_name"),
            data.get("account_id"), data.get("account_name"),
            data.get("model", ""), data.get("stream", 0),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("total_tokens", 0), data.get("credit", 0),
            data.get("finish_reason", ""), data.get("duration_ms", 0),
            data.get("status_code", 200), data.get("error_msg", ""),
            now,
        ))
        conn.commit()
        conn.close()


def list_logs(limit: int = 100, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_conn()
    total_requests = conn.execute("SELECT COUNT(*) as c FROM logs").fetchone()["c"]
    total_tokens = conn.execute("SELECT COALESCE(SUM(total_tokens),0) as s FROM logs").fetchone()["s"]
    total_credit = conn.execute("SELECT COALESCE(SUM(credit),0) as s FROM logs").fetchone()["s"]
    success_requests = conn.execute("""
        SELECT COUNT(*) as c FROM logs
        WHERE status_code BETWEEN 200 AND 299
          AND finish_reason NOT IN ('error', 'content_filter')
    """).fetchone()["c"]
    error_requests = conn.execute("SELECT COUNT(*) as c FROM logs WHERE status_code < 200 OR status_code >= 300 OR finish_reason='error'").fetchone()["c"]
    filtered_requests = conn.execute("SELECT COUNT(*) as c FROM logs WHERE finish_reason='content_filter'").fetchone()["c"]
    avg_duration_ms = conn.execute("SELECT COALESCE(AVG(duration_ms),0) as v FROM logs WHERE duration_ms IS NOT NULL").fetchone()["v"]
    active_accounts = conn.execute("SELECT COUNT(*) as c FROM accounts WHERE status='active'").fetchone()["c"]
    total_accounts = conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"]
    active_keys = conn.execute("SELECT COUNT(*) as c FROM api_keys WHERE status='active'").fetchone()["c"]
    total_keys = conn.execute("SELECT COUNT(*) as c FROM api_keys").fetchone()["c"]

    today_start = _today_start_ts()
    today = conn.execute("""
        SELECT COUNT(*) as requests,
               COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms
        FROM logs WHERE created_at >= ?
    """, (today_start,)).fetchone()
    today_success = conn.execute("""
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ?
          AND status_code BETWEEN 200 AND 299
          AND finish_reason NOT IN ('error', 'content_filter')
    """, (today_start,)).fetchone()["c"]
    today_errors = conn.execute("""
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ? AND (status_code < 200 OR status_code >= 300 OR finish_reason='error')
    """, (today_start,)).fetchone()["c"]
    today_filtered = conn.execute(
        "SELECT COUNT(*) as c FROM logs WHERE created_at >= ? AND finish_reason='content_filter'",
        (today_start,),
    ).fetchone()["c"]

    # 最近 7 天每日统计
    seven_days_ago = int(time.time()) - 7 * 86400
    daily = conn.execute("""
        SELECT date(created_at, 'unixepoch', 'localtime') as date,
               COUNT(*) as requests,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(credit), 0) as credits
        FROM logs WHERE created_at >= ?
        GROUP BY date ORDER BY date
    """, (seven_days_ago,)).fetchall()

    # 模型使用统计
    model_stats = conn.execute("""
        SELECT model, COUNT(*) as count, COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms
        FROM logs GROUP BY model ORDER BY count DESC LIMIT 10
    """).fetchall()

    key_stats = conn.execute("""
        SELECT api_key_name as name, COUNT(*) as count, COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit, MAX(created_at) as last_used_at
        FROM logs
        WHERE api_key_id IS NOT NULL
        GROUP BY api_key_id, api_key_name
        ORDER BY count DESC LIMIT 5
    """).fetchall()

    account_stats = conn.execute("""
        SELECT id, name, nickname, status, total_requests, total_tokens, total_credits, last_used_at
        FROM accounts
        ORDER BY status='active' DESC, total_requests DESC, id ASC
        LIMIT 5
    """).fetchall()

    recent_logs = conn.execute("""
        SELECT id, api_key_name, account_name, model, stream, total_tokens, credit,
               finish_reason, duration_ms, status_code, error_msg, created_at
        FROM logs ORDER BY id DESC LIMIT 8
    """).fetchall()

    conn.close()
    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_credit": round(total_credit, 4),
        "success_requests": success_requests,
        "error_requests": error_requests,
        "filtered_requests": filtered_requests,
        "success_rate": round((success_requests / total_requests * 100) if total_requests else 0, 2),
        "avg_duration_ms": int(avg_duration_ms or 0),
        "today": {
            "requests": int(today["requests"] or 0),
            "tokens": int(today["tokens"] or 0),
            "credit": round(float(today["credit"] or 0), 4),
            "success": int(today_success or 0),
            "errors": int(today_errors or 0),
            "filtered": int(today_filtered or 0),
            "success_rate": round((today_success / today["requests"] * 100) if today["requests"] else 0, 2),
            "avg_duration_ms": int(today["avg_duration_ms"] or 0),
        },
        "active_accounts": active_accounts,
        "total_accounts": total_accounts,
        "active_keys": active_keys,
        "total_keys": total_keys,
        "daily": [dict(r) for r in daily],
        "model_stats": [dict(r) for r in model_stats],
        "key_stats": [dict(r) for r in key_stats],
        "account_stats": [dict(r) for r in account_stats],
        "recent_logs": [dict(r) for r in recent_logs],
    }


# ============================================================
# Settings
# ============================================================

def get_setting(key: str, default: Any = None) -> Any:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    val = row["value"]
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def set_setting(key: str, value: Any):
    val = json.dumps(value) if not isinstance(value, str) else value
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, val),
        )
        conn.commit()
        conn.close()


def get_all_settings() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            result[r["key"]] = r["value"]
    return result
