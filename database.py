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
import re
import sqlite3
import threading
import time
import os
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import credential_crypto

DB_PATH = Path(os.environ.get("CB_GATEWAY_DB_PATH", Path(__file__).parent / "codebuddy_gateway.db"))
_lock = threading.Lock()
_CREDENTIAL_FIELDS = ("access_token", "refresh_token", "session_state")


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _key_prefix(key: str) -> str:
    if len(key) <= 16:
        return key[:6] + "..."
    return f"{key[:12]}...{key[-4:]}"


STATS_PERIODS = ("today", "yesterday", "7d", "30d")


def _today_start_ts() -> int:
    now = time.localtime()
    return int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst)))


def _local_midnight_ts(day: date) -> int:
    return int(time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)))


def _stats_period_window(period: str) -> dict:
    if period not in STATS_PERIODS:
        period = "today"
    today = date.today()
    today_start = _today_start_ts()
    if period == "today":
        start_day = today
        end_exclusive = today + timedelta(days=1)
        start_ts = today_start
        end_ts = _local_midnight_ts(end_exclusive)
        grain = "hour"
        days = 1
    elif period == "yesterday":
        start_day = today - timedelta(days=1)
        end_exclusive = today
        start_ts = _local_midnight_ts(start_day)
        end_ts = today_start
        grain = "hour"
        days = 1
    elif period == "7d":
        start_day = today - timedelta(days=6)
        end_exclusive = today + timedelta(days=1)
        start_ts = _local_midnight_ts(start_day)
        end_ts = _local_midnight_ts(end_exclusive)
        grain = "day"
        days = 7
    else:
        start_day = today - timedelta(days=29)
        end_exclusive = today + timedelta(days=1)
        start_ts = _local_midnight_ts(start_day)
        end_ts = _local_midnight_ts(end_exclusive)
        grain = "day"
        days = 30
    end_inclusive = end_exclusive - timedelta(days=1)
    return {
        "period": period,
        "grain": grain,
        "days": days,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "start_date": start_day.isoformat(),
        "end_date": end_inclusive.isoformat(),
    }


def _query_range_totals(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) as requests,
               COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms
        FROM logs WHERE created_at >= ? AND created_at < ?
        """,
        (start_ts, end_ts),
    ).fetchone()
    success = conn.execute(
        """
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ? AND created_at < ?
          AND status_code BETWEEN 200 AND 299
          AND finish_reason NOT IN ('error', 'content_filter')
        """,
        (start_ts, end_ts),
    ).fetchone()["c"]
    errors = conn.execute(
        """
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ? AND created_at < ?
          AND (status_code < 200 OR status_code >= 300 OR finish_reason='error')
        """,
        (start_ts, end_ts),
    ).fetchone()["c"]
    filtered = conn.execute(
        """
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ? AND created_at < ? AND finish_reason='content_filter'
        """,
        (start_ts, end_ts),
    ).fetchone()["c"]
    requests = int(row["requests"] or 0)
    return {
        "requests": requests,
        "tokens": int(row["tokens"] or 0),
        "credit": round(float(row["credit"] or 0), 4),
        "success": int(success or 0),
        "errors": int(errors or 0),
        "filtered": int(filtered or 0),
        "success_rate": round((success / requests * 100) if requests else 0, 2),
        "avg_duration_ms": int(row["avg_duration_ms"] or 0),
    }


def _fill_hourly(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> list[dict]:
    hourly_rows = conn.execute(
        """
        SELECT CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) as hour,
               COUNT(*) as requests,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(credit), 0) as credit
        FROM logs WHERE created_at >= ? AND created_at < ?
        GROUP BY hour ORDER BY hour
        """,
        (start_ts, end_ts),
    ).fetchall()
    hourly_by_hour = {int(r["hour"]): dict(r) for r in hourly_rows}
    hourly = []
    for hour in range(24):
        row = hourly_by_hour.get(hour, {})
        hourly.append({
            "hour": hour,
            "label": f"{hour:02d}:00",
            "requests": int(row.get("requests") or 0),
            "tokens": int(row.get("tokens") or 0),
            "credit": round(float(row.get("credit") or 0), 4),
        })
    return hourly


def _period_slice(conn: sqlite3.Connection, period: str) -> dict:
    window = _stats_period_window(period)
    start_ts = window["start_ts"]
    end_ts = window["end_ts"]
    start_day = date.fromisoformat(window["start_date"])
    totals = _query_range_totals(conn, start_ts, end_ts)
    hourly = _fill_hourly(conn, start_ts, end_ts) if window["grain"] == "hour" else []
    daily = _fill_daily(conn, start_day, window["days"], start_ts, end_ts)
    model_stats = conn.execute(
        """
        SELECT COALESCE(resolved_model, model) as model, COUNT(*) as count,
               COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms
        FROM logs
        WHERE created_at >= ? AND created_at < ?
        GROUP BY COALESCE(resolved_model, model) ORDER BY count DESC LIMIT 10
        """,
        (start_ts, end_ts),
    ).fetchall()
    key_stats = conn.execute(
        """
        SELECT api_key_name as name, COUNT(*) as count, COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit, MAX(created_at) as last_used_at
        FROM logs
        WHERE api_key_id IS NOT NULL AND created_at >= ? AND created_at < ?
        GROUP BY api_key_id, api_key_name
        ORDER BY count DESC LIMIT 5
        """,
        (start_ts, end_ts),
    ).fetchall()
    return {
        **window,
        **totals,
        "hourly": hourly,
        "daily": daily,
        "model_stats": [dict(r) for r in model_stats],
        "key_stats": [dict(r) for r in key_stats],
    }


def _fill_daily(conn: sqlite3.Connection, start_day: date, days: int, start_ts: int, end_ts: int) -> list[dict]:
    daily_rows = conn.execute(
        """
        SELECT date(created_at, 'unixepoch', 'localtime') as date,
               COUNT(*) as requests,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(credit), 0) as credits
        FROM logs WHERE created_at >= ? AND created_at < ?
        GROUP BY date ORDER BY date
        """,
        (start_ts, end_ts),
    ).fetchall()
    daily_by_date = {r["date"]: dict(r) for r in daily_rows}
    daily = []
    for i in range(days):
        day = (start_day + timedelta(days=i)).isoformat()
        row = daily_by_date.get(day, {})
        credits = round(float(row.get("credits") or 0), 4)
        daily.append({
            "date": day,
            "label": day[5:],
            "requests": int(row.get("requests") or 0),
            "tokens": int(row.get("tokens") or 0),
            "credits": credits,
            "credit": credits,
        })
    return daily


def _load_allowed_models(value: Any) -> Optional[list]:
    if not value:
        return None
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _load_domain_filter(value: Any) -> Optional[list]:
    """API Key level account domain filter: JSON array string <-> list[str]. Empty/bad -> None."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(value, (list, tuple)):
        return None
    out = [str(item).strip() for item in value if str(item).strip()]
    return out or None


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def connection():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def _account_text(value) -> Optional[str]:
    """把账号字段收敛成 TEXT；dict/list 这类上游畸形值降级为空串。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return ""


def _epoch_int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _extra_text(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        return value
    return str(value)


def _protect_account_data(data: dict) -> dict:
    protected = dict(data)
    for field in _CREDENTIAL_FIELDS:
        if field in protected:
            protected[field] = credential_crypto.encrypt_secret(protected.get(field), DB_PATH)
    return protected


def _account_dict(row: sqlite3.Row) -> dict:
    account = dict(row)
    for field in _CREDENTIAL_FIELDS:
        if field in account:
            account[field] = credential_crypto.decrypt_secret(account.get(field), DB_PATH)
    extra = account.get("extra")
    if isinstance(extra, str) and extra:
        try:
            account["extra"] = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            account["extra"] = {}
    elif extra is None:
        account["extra"] = {}
    if not account.get("provider"):
        account["provider"] = "workbuddy"
    return account


def init_db():
    with _lock:
        conn = get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
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
            credit_limit    REAL DEFAULT 0,
            credit_baseline REAL DEFAULT 0,
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
            key_secret      TEXT,
            name            TEXT,
            status          TEXT DEFAULT 'active',
            allowed_models  TEXT,
            daily_limit     INTEGER DEFAULT 0,
            client_type     TEXT DEFAULT 'custom',
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

        CREATE TABLE IF NOT EXISTS account_resource_cache (
            account_id INTEGER PRIMARY KEY,
            payload    TEXT NOT NULL,
            updated_at INTEGER,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS account_checkin_cache (
            account_id   INTEGER PRIMARY KEY,
            checkin_date TEXT,
            payload      TEXT NOT NULL,
            updated_at   INTEGER,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_logs_api_key ON logs(api_key_id);
        CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id);
        CREATE INDEX IF NOT EXISTS idx_logs_model ON logs(model);
        CREATE INDEX IF NOT EXISTS idx_logs_status ON logs(status_code, finish_reason);
        CREATE INDEX IF NOT EXISTS idx_resource_cache_updated ON account_resource_cache(updated_at);
        CREATE INDEX IF NOT EXISTS idx_checkin_cache_date ON account_checkin_cache(checkin_date);
        """)
        _migrate_accounts(conn)
        _migrate_api_keys(conn)
        _migrate_workbuddy_regions(conn)
        _migrate_logs_provider(conn)
        _migrate_logs_resolved_model(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_key_daily_usage (
                api_key_id    INTEGER NOT NULL,
                usage_date    TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(api_key_id, usage_date),
                FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
            )
        """)
        _migrate_account_credentials(conn)
        _migrate_daily_usage(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS automation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                job TEXT NOT NULL,
                run_id TEXT NOT NULL,
                account_id INTEGER,
                account_name TEXT,
                channel TEXT,
                outcome TEXT NOT NULL,
                ok INTEGER NOT NULL,
                status_code INTEGER,
                message TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_automation_events_created ON automation_events(created_at)"
        )
        _prune_automation_events(conn)
        _prune_logs(conn)
        conn.execute("PRAGMA optimize")
        conn.commit()
        conn.close()


def _migrate_accounts(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "provider" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN provider TEXT NOT NULL DEFAULT 'workbuddy'")
    if "extra" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN extra TEXT")
    conn.execute("UPDATE accounts SET provider='workbuddy' WHERE provider IS NULL OR provider=''")
    _dedupe_accounts_provider_uid(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider_uid
            ON accounts(provider, uid)
            WHERE uid IS NOT NULL AND uid != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_accounts_provider_status
            ON accounts(provider, status, priority, id)
        """
    )
    if "weight" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN weight INTEGER DEFAULT 1")
    if "priority" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN priority INTEGER DEFAULT 0")
    if "credit_limit" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN credit_limit REAL DEFAULT 0")
    if "credit_baseline" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN credit_baseline REAL DEFAULT 0")
        conn.execute("""
            UPDATE accounts
            SET credit_baseline=COALESCE(total_credits, 0)
            WHERE credit_limit IS NOT NULL AND credit_limit > 0
        """)
    # NULL means "use the automatic value"; routing.urgency_days() falls back to
    # the channel default. A number is a manual override that wins outright.
    if "urgency_days" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN urgency_days REAL")
    conn.execute("UPDATE accounts SET weight=1 WHERE weight IS NULL OR weight < 1")
    conn.execute("UPDATE accounts SET priority=0 WHERE priority IS NULL")
    conn.execute("UPDATE accounts SET credit_limit=0 WHERE credit_limit IS NULL OR credit_limit < 0")
    conn.execute("UPDATE accounts SET credit_baseline=0 WHERE credit_baseline IS NULL OR credit_baseline < 0")


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
            key_secret      TEXT,
            name            TEXT,
            status          TEXT DEFAULT 'active',
            allowed_models  TEXT,
            daily_limit     INTEGER DEFAULT 0,
            client_type     TEXT DEFAULT 'custom',
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
                    (id, key_prefix, key_hash, key_secret, name, status, allowed_models,
                     daily_limit, client_type, total_requests, total_tokens, created_at,
                     last_used_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                d.get("id"),
                d.get("key_prefix") or _key_prefix(raw_key),
                d.get("key_hash") or (_hash_api_key(raw_key) if raw_key else None),
                d.get("key_secret") or (
                    credential_crypto.encrypt_secret(raw_key, DB_PATH) if raw_key else None
                ),
                d.get("name", ""),
                d.get("status", "active"),
                d.get("allowed_models"),
                d.get("daily_limit") or 0,
                d.get("client_type") or "custom",
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
    if "key_secret" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_secret TEXT")
    if "daily_limit" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN daily_limit INTEGER DEFAULT 0")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "client_type" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN client_type TEXT DEFAULT 'custom'")
    # 归一化历史数据：v1.4.0 之前 UI 允许落库 opencode/openclaw/cherry/nextchat，
    # 后端仅有 codex/custom 两种行为，其余一律归为 custom。
    conn.execute(
        "UPDATE api_keys SET client_type='custom' "
        "WHERE client_type IS NULL OR client_type NOT IN ('custom','codex')"
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "default_channel" not in cols:
        conn.execute(
            "ALTER TABLE api_keys ADD COLUMN default_channel TEXT NOT NULL DEFAULT 'workbuddy'"
        )
    conn.execute(
        "UPDATE api_keys SET default_channel='workbuddy' "
        "WHERE default_channel IS NULL OR default_channel=''"
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "account_domain_filter" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN account_domain_filter TEXT")


def _intl_only_filter(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        patterns = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(patterns, list) or not patterns:
        return False
    for item in patterns:
        text = str(item or "").strip().lower()
        if text in {"*.workbuddy.ai", "workbuddy.ai", "www.workbuddy.ai"}:
            continue
        if text.endswith(".workbuddy.ai") and "*" not in text:
            continue
        return False
    return True


def _migrate_workbuddy_regions(conn: sqlite3.Connection):
    """Move international WorkBuddy accounts and intl-only keys onto workbuddy-i."""
    conn.execute(
        """
        UPDATE accounts
           SET provider='workbuddy-i'
         WHERE provider='workbuddy'
           AND (
                lower(domain)='workbuddy.ai'
                OR lower(domain) LIKE '%.workbuddy.ai'
           )
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "account_domain_filter" not in cols:
        return
    rows = conn.execute(
        "SELECT id, default_channel, account_domain_filter FROM api_keys"
    ).fetchall()
    for row in rows:
        if str(row["default_channel"] or "") == "workbuddy" and _intl_only_filter(row["account_domain_filter"]):
            conn.execute(
                "UPDATE api_keys SET default_channel='workbuddy-i', account_domain_filter=NULL WHERE id=?",
                (row["id"],),
            )
        elif row["account_domain_filter"]:
            conn.execute(
                "UPDATE api_keys SET account_domain_filter=NULL WHERE id=?",
                (row["id"],),
            )


def _dedupe_accounts_provider_uid(conn: sqlite3.Connection):
    dupes = conn.execute(
        """
        SELECT provider, uid, MIN(id) AS keep_id
        FROM accounts
        WHERE uid IS NOT NULL AND uid != ''
        GROUP BY provider, uid
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in dupes:
        conn.execute(
            """
            DELETE FROM accounts
            WHERE provider=? AND uid=? AND id!=?
            """,
            (row["provider"], row["uid"], row["keep_id"]),
        )


def _migrate_logs_provider(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "provider" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN provider TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_provider ON logs(provider)")


def _migrate_logs_resolved_model(conn: sqlite3.Connection):
    # Schema DDL must not ride an already-open transaction: init_db's
    # executescript leaves one open (Python 3.13 no longer commits for it), and
    # SQLite answers CREATE INDEX with "unable to open database file" when a
    # RENAME COLUMN happened in that same uncommitted transaction.
    if conn.in_transaction:
        conn.commit()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "resolved_model" not in cols:
        if "model_resolved" in cols:
            # Our old spelling; the upstream column name won (RENAME COLUMN
            # keeps every row, needs SQLite >= 3.25; bundled here is 3.45.3).
            conn.execute("ALTER TABLE logs RENAME COLUMN model_resolved TO resolved_model")
        else:
            conn.execute("ALTER TABLE logs ADD COLUMN resolved_model TEXT")
    conn.execute("DROP INDEX IF EXISTS idx_logs_model_resolved")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_resolved_model ON logs(resolved_model)")
    conn.commit()


def _migrate_account_credentials(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT id, access_token, refresh_token, session_state FROM accounts"
    ).fetchall()
    for row in rows:
        updates = {}
        for field in _CREDENTIAL_FIELDS:
            value = row[field]
            if value and not credential_crypto.is_encrypted(value):
                updates[field] = credential_crypto.encrypt_secret(value, DB_PATH)
        if updates:
            fields = ", ".join(f"{field}=?" for field in updates)
            conn.execute(
                f"UPDATE accounts SET {fields} WHERE id=?",
                [*updates.values(), row["id"]],
            )


def _migrate_daily_usage(conn: sqlite3.Connection):
    conn.execute(
        """
        INSERT INTO api_key_daily_usage (api_key_id, usage_date, request_count)
        SELECT logs.api_key_id, date(logs.created_at, 'unixepoch', 'localtime'), COUNT(*)
        FROM logs
        WHERE logs.api_key_id IS NOT NULL
          AND logs.created_at >= ?
          AND EXISTS (SELECT 1 FROM api_keys WHERE api_keys.id = logs.api_key_id)
        GROUP BY logs.api_key_id, date(logs.created_at, 'unixepoch', 'localtime')
        ON CONFLICT(api_key_id, usage_date) DO UPDATE SET
            request_count=MAX(api_key_daily_usage.request_count, excluded.request_count)
        """,
        (_today_start_ts(),),
    )


def _prune_logs(conn: sqlite3.Connection):
    try:
        retention_days = max(1, int(os.environ.get("CB_GATEWAY_LOG_RETENTION_DAYS", "90")))
    except ValueError:
        retention_days = 90
    cutoff_ts = int(time.time()) - retention_days * 86400
    cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff_ts,))
    conn.execute("DELETE FROM api_key_daily_usage WHERE usage_date < ?", (cutoff_date,))


# ============================================================
# Accounts
# ============================================================

def add_account(data: dict) -> int:
    data = _protect_account_data(data)
    now = int(time.time())
    weight = max(1, int(data.get("weight", 1) or 1))
    priority = int(data.get("priority", 0) or 0)
    credit_limit = max(0.0, float(data.get("credit_limit", 0) or 0))
    credit_baseline = max(0.0, float(data.get("credit_baseline", 0) or 0))
    provider = str(data.get("provider") or "workbuddy").strip() or "workbuddy"
    extra_text = _extra_text(data.get("extra"))
    with _lock:
        conn = get_conn()
        cur = conn.execute("""
            INSERT INTO accounts
                (name, uid, nickname, phone, account_type, access_token, refresh_token,
                 expires_at, refresh_expires_at, domain, enterprise_id, session_state,
                 status, weight, priority, credit_limit, credit_baseline, provider, extra,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            _account_text(data.get("name")) or "",
            _account_text(data.get("uid")) or "",
            _account_text(data.get("nickname")) or "",
            _account_text(data.get("phone")) or "",
            _account_text(data.get("account_type")) or "personal",
            _account_text(data.get("access_token")) or "",
            _account_text(data.get("refresh_token")) or "",
            _epoch_int(data.get("expires_at")),
            _epoch_int(data.get("refresh_expires_at")),
            _account_text(data.get("domain")) or "www.codebuddy.cn",
            _account_text(data.get("enterprise_id")) or "",
            _account_text(data.get("session_state")) or "",
            _account_text(data.get("status")) or "active",
            weight,
            priority,
            credit_limit,
            credit_baseline,
            provider,
            extra_text,
            now, now,
        ))
        aid = cur.lastrowid
        conn.commit()
        conn.close()
        return aid


def update_account(aid: int, data: dict):
    data = _protect_account_data(data)
    now = int(time.time())
    fields = []
    values = []
    for k in ["name", "uid", "nickname", "phone", "account_type", "access_token",
              "refresh_token", "expires_at", "refresh_expires_at", "domain",
              "enterprise_id", "session_state", "status", "weight", "priority",
              "credit_limit", "credit_baseline", "provider", "extra", "urgency_days"]:
        if k in data:
            if k == "weight":
                data[k] = max(1, int(data[k] or 1))
            elif k == "priority":
                data[k] = int(data[k] or 0)
            elif k == "urgency_days":
                # None/"" clears the override so the automatic value applies.
                raw_urgency = data[k]
                data[k] = None if raw_urgency in (None, "") else max(0.0, float(raw_urgency))
            elif k in {"credit_limit", "credit_baseline"}:
                data[k] = max(0.0, float(data[k] or 0))
            elif k == "provider":
                data[k] = str(data[k] or "workbuddy").strip() or "workbuddy"
            elif k == "extra":
                data[k] = _extra_text(data[k])
            elif k in {"expires_at", "refresh_expires_at"}:
                data[k] = _epoch_int(data[k])
            elif isinstance(data[k], (dict, list)):
                # 上游 auth 文件可能给出 dict/list（例如加密信封），TEXT 列收敛成空串。
                data[k] = ""
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
        conn.execute("DELETE FROM account_resource_cache WHERE account_id=?", (aid,))
        conn.execute("DELETE FROM account_checkin_cache WHERE account_id=?", (aid,))
        conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
        conn.commit()
        conn.close()


def get_account(aid: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    conn.close()
    return _account_dict(row) if row else None


def list_accounts(*, provider: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if provider:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE provider=? ORDER BY id",
            (provider,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [_account_dict(r) for r in rows]


def get_active_accounts(provider: str = "workbuddy") -> list[dict]:
    if not provider:
        raise ValueError("get_active_accounts requires provider")
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM accounts
        WHERE status='active' AND provider=?
        ORDER BY priority DESC,
                 (CAST(total_requests AS REAL) / CASE WHEN weight > 0 THEN weight ELSE 1 END) ASC,
                 total_requests ASC,
                 id ASC
        """,
        (provider,),
    ).fetchall()
    conn.close()
    return [_account_dict(r) for r in rows]


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
# Account cache
# ============================================================

def _payload_remaining(payload: dict) -> Optional[float]:
    """Best-effort remaining quota out of a cached resource payload."""
    if not isinstance(payload, dict):
        return None
    for key in ("remaining", "available_total", "total_dosage"):
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


# Keys `get_account_resource_cache` adds to every read; they are never stored.
_RESOURCE_CACHE_META = ("cached", "cache_updated_at", "age_seconds")


def _write_account_resource_cache(account_id: int, payload: dict, *, merge: bool) -> dict:
    now = int(time.time())
    safe_payload = {
        key: value
        for key, value in dict(payload or {}).items()
        if key not in _RESOURCE_CACHE_META
    }
    safe_payload["cached"] = False
    safe_payload["stale"] = False
    safe_payload["updated_at"] = int(safe_payload.get("updated_at") or now)
    with _lock:
        conn = get_conn()
        # Carry the highest quota ever seen forward. Routing uses it as the
        # denominator of "how empty is this account", because the current
        # remaining value alone cannot tell 3/3 from 3/3000.
        previous_peak = None
        stored = {}
        row = conn.execute(
            "SELECT payload FROM account_resource_cache WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if row:
            try:
                stored = json.loads(row["payload"] or "{}")
                if not isinstance(stored, dict):
                    stored = {}
                previous_peak = _payload_remaining(stored)
                stored_peak = stored.get("peak_remaining")
                if stored_peak not in (None, ""):
                    stored_peak = float(stored_peak)
                    if previous_peak is None or stored_peak > previous_peak:
                        previous_peak = stored_peak
            except (json.JSONDecodeError, TypeError, ValueError):
                stored = {}
                previous_peak = None
        if merge:
            # A partial observation (one quota reading, one deadline) must not
            # erase the keys it did not look at — the expiry a check-in cached
            # and the number a quota check measured have to survive each other.
            merged = dict(stored)
            merged.update(safe_payload)
            safe_payload = merged
        current = _payload_remaining(safe_payload)
        peak = max([v for v in (previous_peak, current) if v is not None], default=None)
        if peak is not None:
            safe_payload["peak_remaining"] = peak
        conn.execute(
            """
            INSERT INTO account_resource_cache (account_id, payload, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (account_id, json.dumps(safe_payload, ensure_ascii=False), safe_payload["updated_at"]),
        )
        conn.commit()
        conn.close()
    return safe_payload


def upsert_account_resource_cache(account_id: int, payload: dict):
    """Replace the cached row with a complete snapshot."""
    return _write_account_resource_cache(account_id, payload, merge=False)


def merge_account_resource_cache(account_id: int, payload: dict) -> dict:
    """Merge observed keys into the cached row, keeping what was already known.

    Use this for a partial observation. ``upsert_account_resource_cache``
    replaces the whole payload, so a writer that only measured a quota number
    would silently drop the deadline another writer cached, and vice versa.
    """
    return _write_account_resource_cache(account_id, payload, merge=True)


def get_account_resource_cache(account_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT payload, updated_at FROM account_resource_cache WHERE account_id=?",
        (account_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    payload["cached"] = True
    payload["cache_updated_at"] = int(row["updated_at"] or payload.get("updated_at") or 0)
    payload["age_seconds"] = max(0, int(time.time()) - int(payload["cache_updated_at"] or 0))
    return payload


def upsert_account_checkin_cache(account_id: int, payload: dict):
    now = int(time.time())
    checkin_date = date.today().isoformat()
    safe_payload = dict(payload or {})
    safe_payload["cached"] = False
    safe_payload["stale"] = False
    safe_payload["updated_at"] = int(safe_payload.get("updated_at") or now)
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO account_checkin_cache (account_id, checkin_date, payload, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                checkin_date=excluded.checkin_date,
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (account_id, checkin_date, json.dumps(safe_payload, ensure_ascii=False), safe_payload["updated_at"]),
        )
        conn.commit()
        conn.close()


def get_account_checkin_cache(account_id: int, today_only: bool = True) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT checkin_date, payload, updated_at FROM account_checkin_cache WHERE account_id=?",
        (account_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    if today_only and row["checkin_date"] != date.today().isoformat():
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    payload["cached"] = True
    payload["cache_date"] = row["checkin_date"]
    payload["cache_updated_at"] = int(row["updated_at"] or payload.get("updated_at") or 0)
    payload["age_seconds"] = max(0, int(time.time()) - int(payload["cache_updated_at"] or 0))
    return payload


# ============================================================
# API Keys
# ============================================================

def add_api_key(key: str, name: str, allowed_models: Optional[list] = None,
                daily_limit: Optional[int] = None, client_type: str = "custom",
                default_channel: str = "workbuddy",
                account_domain_filter: Optional[list] = None) -> int:
    now = int(time.time())
    models_json = json.dumps(allowed_models) if allowed_models else None
    limit = int(daily_limit or 0)
    channel = str(default_channel or "workbuddy").strip() or "workbuddy"
    domain_filter = json.dumps(account_domain_filter) if account_domain_filter else None
    with _lock:
        conn = get_conn()
        cur = conn.execute("""
            INSERT INTO api_keys
                (key_prefix, key_hash, key_secret, name, status, allowed_models,
                 daily_limit, client_type, default_channel, account_domain_filter, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            _key_prefix(key),
            _hash_api_key(key),
            credential_crypto.encrypt_secret(key, DB_PATH),
            name,
            "active",
            models_json,
            limit,
            client_type,
            channel,
            domain_filter,
            now,
        ))
        kid = cur.lastrowid
        conn.commit()
        conn.close()
        return kid


def update_api_key(kid: int, data: dict):
    fields = []
    values = []
    for k in ["name", "status", "allowed_models", "daily_limit", "client_type", "default_channel", "account_domain_filter"]:
        if k in data:
            val = data[k]
            if k == "allowed_models" and isinstance(val, list):
                val = json.dumps(val) if val else None
            if k == "account_domain_filter" and isinstance(val, list):
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
    d.pop("key_secret", None)
    d.pop("key", None)
    d["allowed_models"] = _load_allowed_models(d.get("allowed_models"))
    d["account_domain_filter"] = _load_domain_filter(d.get("account_domain_filter"))
    return d


def list_api_keys(*, include_secret: bool = False) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT k.*, COALESCE(u.request_count, 0) AS today_requests
        FROM api_keys AS k
        LEFT JOIN api_key_daily_usage AS u
          ON u.api_key_id=k.id AND u.usage_date=?
        ORDER BY k.id DESC
        """,
        (date.today().isoformat(),),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d.pop("key_hash", None)
        encrypted_key = d.pop("key_secret", None)
        d.pop("key", None)
        if include_secret:
            d["key"] = (
                credential_crypto.decrypt_secret(encrypted_key, DB_PATH)
                if encrypted_key else None
            )
        d["allowed_models"] = _load_allowed_models(d.get("allowed_models"))
        d["account_domain_filter"] = _load_domain_filter(d.get("account_domain_filter"))
        result.append(d)
    return result


def get_api_key_daily_requests(kid: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT request_count AS c FROM api_key_daily_usage WHERE api_key_id=? AND usage_date=?",
        (kid, date.today().isoformat()),
    ).fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def reserve_api_key_request(kid: int, daily_limit: int) -> bool:
    """Atomically reserve one daily request slot for an API key."""
    today = date.today().isoformat()
    with _lock:
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_count FROM api_key_daily_usage WHERE api_key_id=? AND usage_date=?",
                (kid, today),
            ).fetchone()
            current = int(row["request_count"] if row else 0)
            if daily_limit > 0 and current >= daily_limit:
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO api_key_daily_usage (api_key_id, usage_date, request_count)
                VALUES (?, ?, 1)
                ON CONFLICT(api_key_id, usage_date) DO UPDATE SET
                    request_count=request_count + 1
                """,
                (kid, today),
            )
            conn.commit()
            return True


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

def _resolved_of(data: dict) -> str:
    """The model the provider actually called, in the single ``resolved_model``
    column (upstream spelling). Falls back to the alias so a row is never blank.
    """
    return (
        data.get("resolved_model")
        or data.get("model", "")
    )


def add_log(data: dict):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO logs
                (api_key_id, api_key_name, account_id, account_name, model, stream,
                 prompt_tokens, completion_tokens, total_tokens, credit,
                 finish_reason, duration_ms, status_code, error_msg, provider, resolved_model, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("api_key_id"), data.get("api_key_name"),
            data.get("account_id"), data.get("account_name"),
            data.get("model", ""), data.get("stream", 0),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("total_tokens", 0), data.get("credit", 0),
            data.get("finish_reason", ""), data.get("duration_ms", 0),
            data.get("status_code", 200), data.get("error_msg", ""),
            data.get("provider") or "workbuddy",
            _resolved_of(data), now,
        ))
        conn.commit()
        conn.close()


def record_request(data: dict):
    """Write a request log and update account/key counters in one transaction."""
    now = int(time.time())
    with _lock:
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO logs
                    (api_key_id, api_key_name, account_id, account_name, model, resolved_model, stream,
                     prompt_tokens, completion_tokens, total_tokens, credit,
                     finish_reason, duration_ms, status_code, error_msg, provider, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data.get("api_key_id"), data.get("api_key_name"),
                    data.get("account_id"), data.get("account_name"),
                    data.get("model", ""), _resolved_of(data),
                    data.get("stream", 0),
                    data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
                    data.get("total_tokens", 0), data.get("credit", 0),
                    data.get("finish_reason", ""), data.get("duration_ms", 0),
                    data.get("status_code", 200), data.get("error_msg", ""),
                    data.get("provider") or "workbuddy", now,
                ),
            )
            if data.get("account_id") and data.get("increment_usage", True):
                conn.execute(
                    """
                    UPDATE accounts SET
                        total_requests=total_requests + 1,
                        total_tokens=total_tokens + ?,
                        total_credits=total_credits + ?,
                        last_used_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        data.get("total_tokens", 0), data.get("credit", 0),
                        now, now, data["account_id"],
                    ),
                )
            if data.get("api_key_id") and data.get("increment_usage", True):
                conn.execute(
                    """
                    UPDATE api_keys SET
                        total_requests=total_requests + 1,
                        total_tokens=total_tokens + ?,
                        last_used_at=?
                    WHERE id=?
                    """,
                    (data.get("total_tokens", 0), now, data["api_key_id"]),
                )
            conn.commit()
    from request_logging import note_record

    note_record(data)


def prune_logs(retention_days: int | None = None) -> int:
    """Delete expired request logs and return the number of removed rows."""
    if retention_days is None:
        try:
            retention_days = int(os.environ.get("CB_GATEWAY_LOG_RETENTION_DAYS", "90"))
        except ValueError:
            retention_days = 90
    retention_days = max(1, retention_days)
    cutoff_ts = int(time.time()) - retention_days * 86400
    cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
    with _lock:
        with connection() as conn:
            cursor = conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff_ts,))
            conn.execute("DELETE FROM api_key_daily_usage WHERE usage_date < ?", (cutoff_date,))
            conn.commit()
            return max(0, cursor.rowcount)


def _prune_automation_events(conn: sqlite3.Connection) -> None:
    cutoff = int(time.time()) - 30 * 86400
    conn.execute("DELETE FROM automation_events WHERE created_at < ?", (cutoff,))
    conn.execute(
        """
        DELETE FROM automation_events
        WHERE id NOT IN (
            SELECT id FROM automation_events ORDER BY id DESC LIMIT 2000
        )
        """
    )


def public_automation_text(value, limit: int = 240) -> str:
    """Return a log-safe message. Never keep header values or opaque credentials."""
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"(?i)\b(?:authorization\s*[:=]\s*)?(?:bearer|cloud-ide-jwt)\s+\S+",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?i)\b(?:authorization|cookie)\s*[:=]\s*\S+", "[redacted]", text)
    text = re.sub(
        r"(?i)((?:access_token|refresh_token|password|session_state)[\"']?\s*[:=]\s*[\"']?)\S+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:access_token|refresh_token|token|code)=)[^&\s]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"\beyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]+){1,}", "[redacted]", text)
    text = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "[redacted]", text)
    return text[:limit]


def add_automation_events(events: list[dict]) -> None:
    """Store public automation results. Credential-shaped text is redacted again."""
    if not events:
        return
    now = int(time.time())
    rows = []
    for event in events:
        account_id = event.get("account_id")
        try:
            account_id = int(account_id) if account_id not in (None, "") else None
        except (TypeError, ValueError):
            account_id = None
        status_code = event.get("status_code")
        try:
            status_code = int(status_code) if status_code not in (None, "") else None
        except (TypeError, ValueError):
            status_code = None
        rows.append(
            (
                int(event.get("created_at") or now),
                str(event.get("source") or "manual")[:32],
                str(event.get("job") or "")[:32],
                str(event.get("run_id") or "")[:32],
                account_id,
                public_automation_text(event.get("account_name") or "", 80),
                public_automation_text(event.get("channel") or "", 32),
                str(event.get("outcome") or "failed")[:32],
                1 if event.get("ok") else 0,
                status_code,
                public_automation_text(event.get("message") or ""),
            )
        )
    with _lock:
        conn = get_conn()
        conn.executemany(
            """
            INSERT INTO automation_events
                (created_at, source, job, run_id, account_id, account_name,
                 channel, outcome, ok, status_code, message)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        _prune_automation_events(conn)
        conn.commit()
        conn.close()


def list_automation_events(limit: int = 100, failures_only: bool = False) -> list[dict]:
    limit = max(1, min(200, int(limit or 100)))
    sql = """
        SELECT id, created_at, source, job, run_id, account_id, account_name,
               channel, outcome, ok, status_code, message
        FROM automation_events
    """
    if failures_only:
        sql += " WHERE outcome IN ('failed', 'error')"
    sql += " ORDER BY id DESC LIMIT ?"
    conn = get_conn()
    rows = conn.execute(sql, (limit,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def latest_automation_runs() -> list[dict]:
    conn = get_conn()
    anchors = conn.execute(
        """
        SELECT source, job, run_id, created_at
        FROM automation_events
        WHERE id IN (SELECT MAX(id) FROM automation_events GROUP BY source, job)
        ORDER BY id DESC
        """
    ).fetchall()
    latest = []
    for anchor in anchors:
        stats = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN outcome IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed
            FROM automation_events
            WHERE run_id=?
            """,
            (anchor["run_id"],),
        ).fetchone()
        latest.append(
            {
                "source": anchor["source"],
                "job": anchor["job"],
                "run_id": anchor["run_id"],
                "created_at": int(anchor["created_at"] or 0),
                "total": int(stats["total"] or 0),
                "failed": int(stats["failed"] or 0),
            }
        )
    conn.close()
    return latest


def list_logs(limit: int = 100, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_logs(filters: Optional[dict] = None) -> dict:
    filters = filters or {}
    limit = max(1, min(500, int(filters.get("limit") or 100)))
    offset = max(0, int(filters.get("offset") or 0))
    where = []
    values: list[Any] = []

    q = str(filters.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where.append(
            "(api_key_name LIKE ? OR account_name LIKE ? OR model LIKE ? OR resolved_model LIKE ? OR finish_reason LIKE ? OR error_msg LIKE ?)"
        )
        values.extend([like] * 6)

    status = str(filters.get("status") or "all").strip()
    if status == "success":
        where.append("status_code BETWEEN 200 AND 299 AND finish_reason NOT IN ('error', 'content_filter')")
    elif status == "error":
        where.append("(status_code < 200 OR status_code >= 300 OR finish_reason='error')")
    elif status == "filtered":
        where.append("finish_reason='content_filter'")

    for key, col in (
        ("api_key_id", "api_key_id"),
        ("account_id", "account_id"),
    ):
        value = filters.get(key)
        if value not in (None, "", "all"):
            where.append(f"{col}=?")
            values.append(int(value))

    model = str(filters.get("model") or "").strip()
    if model:
        where.append("model=?")
        values.append(model)

    start = filters.get("start")
    if start not in (None, "", "all"):
        where.append("created_at>=?")
        values.append(int(start))

    end = filters.get("end")
    if end not in (None, "", "all"):
        where.append("created_at<=?")
        values.append(int(end))

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) AS c FROM logs{sql_where}", values).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM logs{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, limit, offset],
    ).fetchall()
    model_rows = conn.execute(
        "SELECT DISTINCT model FROM logs WHERE model IS NOT NULL AND model!='' ORDER BY model LIMIT 200"
    ).fetchall()
    conn.close()
    return {
        "items": [dict(r) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "models": [r["model"] for r in model_rows],
    }


def get_stats(period: str = "today") -> dict:
    window = _stats_period_window(period)
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

    periods = {item: _period_slice(conn, item) for item in STATS_PERIODS}
    selected = periods[window["period"]]
    today_slice = periods["today"]
    daily = periods["30d"]["daily"] if window["period"] == "30d" else periods["7d"]["daily"]

    account_stats = conn.execute("""
        SELECT id, name, nickname, status, total_requests, total_tokens, total_credits, last_used_at
        FROM accounts
        ORDER BY status='active' DESC, total_requests DESC, id ASC
        LIMIT 5
    """).fetchall()

    recent_logs = conn.execute("""
        SELECT id, api_key_name, account_name, model, provider, resolved_model, stream, total_tokens, credit,
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
        "period": window["period"],
        "range": selected,
        "periods": periods,
        "today": {
            "requests": today_slice["requests"],
            "tokens": today_slice["tokens"],
            "credit": today_slice["credit"],
            "success": today_slice["success"],
            "errors": today_slice["errors"],
            "filtered": today_slice["filtered"],
            "success_rate": today_slice["success_rate"],
            "avg_duration_ms": today_slice["avg_duration_ms"],
            "hourly": today_slice["hourly"],
        },
        "active_accounts": active_accounts,
        "total_accounts": total_accounts,
        "active_keys": active_keys,
        "total_keys": total_keys,
        "daily": daily,
        "model_stats": selected["model_stats"],
        "key_stats": selected["key_stats"],
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
