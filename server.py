"""
server.py — Buddy 2 API 主服务

FastAPI 应用，包含：
  - /v1/chat/completions  代理端点（OpenAI 兼容）
  - /v1/models            模型列表
  - /health               健康检查
  - /admin/*              管理 API
  - /                     Web UI
"""

import argparse
import contextvars
import os
import secrets
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse

import database as db
import auth_manager
import proxy

app = FastAPI(title="Buddy 2 API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = Path(__file__).parent / "web"


# ============================================================
# 中间件：管理 API 鉴权
# ============================================================

ADMIN_TOKEN: str = ""
ALLOW_NO_ADMIN_AUTH = False
ADMIN_COOKIE_NAME = "cb_gw_admin_token"
_CURRENT_REQUEST: contextvars.ContextVar[Request | None] = contextvars.ContextVar("current_request", default=None)


@app.middleware("http")
async def _request_context(request: Request, call_next):
    token = _CURRENT_REQUEST.set(request)
    try:
        return await call_next(request)
    finally:
        _CURRENT_REQUEST.reset(token)


def _check_admin(authorization: str | None):
    if ALLOW_NO_ADMIN_AUTH:
        return
    candidates = []
    if authorization:
        parts = authorization.split(" ", 1)
        candidates.append(parts[1] if len(parts) == 2 else parts[0])

    request = _CURRENT_REQUEST.get()
    if request:
        candidates.append(request.cookies.get(ADMIN_COOKIE_NAME, ""))

    if not any(t and secrets.compare_digest(t, ADMIN_TOKEN) for t in candidates):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def _check_client_auth(authorization: str | None, x_api_key: str | None):
    """检查客户端 API Key。如果没有配置任何 key 则不校验。"""
    keys = db.list_api_keys()
    if not keys:
        return None  # 没有配置任何 key，放行

    token = ""
    if x_api_key:
        token = x_api_key
    elif authorization:
        parts = authorization.split(" ", 1)
        token = parts[1] if len(parts) == 2 else parts[0]

    if not token:
        raise HTTPException(status_code=401, detail={"error": {"message": "API key required", "type": "invalid_request_error"}})

    key_info = db.get_api_key_by_key(token)
    if not key_info:
        raise HTTPException(status_code=401, detail={"error": {"message": "Invalid API key", "type": "invalid_request_error"}})

    daily_limit = int(key_info.get("daily_limit") or 0)
    if daily_limit > 0:
        used_today = db.get_api_key_daily_requests(key_info["id"])
        if used_today >= daily_limit:
            raise HTTPException(
                status_code=429,
                detail={"error": {"message": "Daily API key request limit exceeded", "type": "rate_limit_error"}},
            )
    return key_info


# ============================================================
# OpenAI 兼容端点
# ============================================================

@app.get("/health")
async def health():
    accounts = db.list_accounts()
    active = [a for a in accounts if a.get("status") == "active"]
    statuses = auth_manager.check_all_accounts()
    return {
        "status": "ok",
        "accounts": len(accounts),
        "active_accounts": len(active),
        "account_statuses": statuses,
    }


@app.get("/v1/models")
async def list_models():
    models = db.get_setting("models", proxy.DEFAULT_MODELS)
    return {
        "object": "list",
        "data": [
            {"id": m["id"], "object": "model", "created": 0, "owned_by": "buddy2api"}
            for m in models
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
):
    api_key_info = _check_client_auth(authorization, x_api_key)

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 检查 API Key 模型权限（同时匹配别名和真实模型 ID）
    if api_key_info and api_key_info.get("allowed_models"):
        raw_model = payload.get("model", "auto")
        resolved_model = proxy.resolve_model_alias(raw_model)
        if raw_model not in api_key_info["allowed_models"] and resolved_model not in api_key_info["allowed_models"]:
            raise HTTPException(status_code=403, detail={"error": {"message": f"Model '{raw_model}' not allowed for this API key", "type": "invalid_request_error"}})

    result = await proxy.proxy_chat_completions(payload, api_key_info)

    if result[0] == "error":
        status, detail = result[1]
        return JSONResponse(status_code=status, content=detail)
    elif result[0] == "json":
        return JSONResponse(content=result[1])
    elif result[0] == "stream":
        return StreamingResponse(
            result[1],
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# ============================================================
# Admin API
# ============================================================

@app.get("/admin/stats")
async def admin_stats(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_stats()


# --- Accounts ---

@app.get("/admin/accounts")
async def admin_list_accounts(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    accounts = db.list_accounts()
    result = []
    for a in accounts:
        s = auth_manager.get_account_status(a)
        s["phone"] = a.get("phone", "")
        s["account_type"] = a.get("account_type", "")
        s["enterprise_id"] = a.get("enterprise_id", "")
        s["domain"] = a.get("domain", "")
        s["weight"] = int(a.get("weight") or 1)
        s["priority"] = int(a.get("priority") or 0)
        s["credit_limit"] = float(a.get("credit_limit") or 0)
        result.append(s)
    return result


@app.get("/admin/accounts/discover")
async def admin_discover_accounts(
    auth_dir: str | None = None,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return auth_manager.discover_auth_files(auth_dir)


@app.post("/admin/accounts/scan")
async def admin_scan_accounts(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    try:
        data = await request.json()
    except Exception:
        data = {}
    auth_dir = data.get("auth_dir") if isinstance(data, dict) else None
    return auth_manager.auto_scan_and_import(auth_dir)


@app.post("/admin/accounts")
async def admin_add_account(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await request.json()
    # 直接粘贴 auth JSON
    auth_data = data.get("auth", {})
    account_data = data.get("account", {})
    parsed = {
        "name": account_data.get("nickname", data.get("name", "")),
        "uid": account_data.get("uid", ""),
        "nickname": account_data.get("nickname", ""),
        "phone": account_data.get("phoneNumber", ""),
        "account_type": account_data.get("type", "personal"),
        "access_token": auth_data.get("accessToken", ""),
        "refresh_token": auth_data.get("refreshToken", ""),
        "expires_at": auth_data.get("expiresAt", 0),
        "refresh_expires_at": auth_data.get("refreshExpiresAt", 0),
        "domain": auth_data.get("domain", "www.codebuddy.cn"),
        "enterprise_id": account_data.get("enterpriseId", ""),
        "session_state": auth_data.get("sessionState", ""),
    }
    if not parsed["access_token"]:
        raise HTTPException(status_code=400, detail="No accessToken found in auth data")
    aid = db.add_account(parsed)
    return {"id": aid, "status": "ok"}


@app.put("/admin/accounts/{aid}")
async def admin_update_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await request.json()
    allowed = {"name", "status", "weight", "priority", "credit_limit"}
    update_data = {k: data[k] for k in allowed if k in data}
    if "status" in update_data and update_data["status"] not in {"active", "inactive", "expired"}:
        raise HTTPException(status_code=400, detail="Invalid account status")
    db.update_account(aid, update_data)
    return {"status": "ok"}


@app.delete("/admin/accounts/{aid}")
async def admin_delete_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_account(aid)
    return {"status": "ok"}


@app.post("/admin/accounts/{aid}/refresh")
async def admin_refresh_account(
    aid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    ok = auth_manager.refresh_token(account)
    return {"status": "ok" if ok else "failed"}


@app.post("/admin/accounts/{aid}/test")
async def admin_test_account(
    aid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    account = db.get_account(aid)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        data = await request.json()
    except Exception:
        data = {}
    model = data.get("model") if isinstance(data, dict) else None
    prompt = data.get("prompt") if isinstance(data, dict) else None
    return await proxy.test_account_chat(account, model or "auto", prompt or "ping")


# --- API Keys ---

@app.get("/admin/api-keys")
async def admin_list_keys(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.list_api_keys()


@app.post("/admin/api-keys")
async def admin_create_key(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await request.json()
    name = data.get("name", "")
    allowed = data.get("allowed_models")
    daily_limit = data.get("daily_limit")
    # 生成 sk- 前缀的 key
    key = f"sk-cb-{secrets.token_urlsafe(32)}"
    kid = db.add_api_key(key, name, allowed, daily_limit)
    return {"id": kid, "key": key, "status": "ok"}


@app.put("/admin/api-keys/{kid}")
async def admin_update_key(
    kid: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await request.json()
    db.update_api_key(kid, data)
    return {"status": "ok"}


@app.delete("/admin/api-keys/{kid}")
async def admin_delete_key(
    kid: int,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    db.delete_api_key(kid)
    return {"status": "ok"}


# --- Logs ---

@app.get("/admin/logs")
async def admin_logs(
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    return db.list_logs(limit, offset)


# --- Settings ---

@app.get("/admin/settings")
async def admin_get_settings(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_all_settings()


@app.put("/admin/settings")
async def admin_update_settings(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await request.json()
    for k, v in data.items():
        db.set_setting(k, v)
    return {"status": "ok"}


# --- Models ---

@app.get("/admin/models")
async def admin_get_models(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return db.get_setting("models", proxy.DEFAULT_MODELS)


@app.put("/admin/models")
async def admin_update_models(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await request.json()
    db.set_setting("models", data)
    return {"status": "ok"}


# --- Model Aliases ---

@app.get("/admin/aliases")
async def admin_get_aliases(authorization: str | None = Header(default=None)):
    _check_admin(authorization)
    return proxy.get_all_aliases()


@app.put("/admin/aliases")
async def admin_update_aliases(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _check_admin(authorization)
    data = await request.json()
    # Only store user-defined aliases (not built-in ones)
    user_aliases = {k: v for k, v in data.items() if k not in proxy._BUILTIN_ALIASES}
    db.set_setting("model_aliases", user_aliases)
    return {"status": "ok"}


# ============================================================
# Web UI
# ============================================================

@app.get("/")
async def index():
    response = FileResponse(str(WEB_DIR / "index.html"))
    if ADMIN_TOKEN and not ALLOW_NO_ADMIN_AUTH:
        response.set_cookie(
            ADMIN_COOKIE_NAME,
            ADMIN_TOKEN,
            httponly=True,
            samesite="lax",
            max_age=30 * 24 * 3600,
        )
    return response


# ============================================================
# 启动
# ============================================================

def main():
    global ADMIN_TOKEN, ALLOW_NO_ADMIN_AUTH

    ap = argparse.ArgumentParser(description="Buddy 2 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--admin-token", default=os.environ.get("CB_GATEWAY_ADMIN_TOKEN", ""),
                    help="Admin API token. Defaults to CB_GATEWAY_ADMIN_TOKEN or a generated startup token.")
    ap.add_argument("--no-admin-auth", action="store_true",
                    help="Disable Admin API authentication. Only use on trusted local machines.")
    ap.add_argument("--log-level", default="warning", choices=["debug","info","warning","error"],
                    help="Log level")
    args = ap.parse_args()

    ALLOW_NO_ADMIN_AUTH = args.no_admin_auth
    ADMIN_TOKEN = "" if ALLOW_NO_ADMIN_AUTH else (args.admin_token or f"cb-admin-{secrets.token_urlsafe(24)}")

    db.init_db()

    # 启动时自动扫描
    result = auth_manager.auto_scan_and_import()
    if result["imported"] or result["updated"]:
        sys.stderr.write(f"[startup] Auto-scan: {result}\n")

    accounts = db.list_accounts()
    sys.stderr.write(f"\n")
    sys.stderr.write(f"  Buddy 2 API v1.0\n")
    sys.stderr.write(f"  ========================\n")
    sys.stderr.write(f"  监听: http://{args.host}:{args.port}\n")
    sys.stderr.write(f"  账号: {len(accounts)} 个 ({sum(1 for a in accounts if a['status']=='active')} active)\n")
    sys.stderr.write(f"  Admin: {'no auth' if ALLOW_NO_ADMIN_AUTH else 'enabled'}\n")
    if ADMIN_TOKEN:
        sys.stderr.write(f"  Admin Token: {ADMIN_TOKEN}\n")
    sys.stderr.write(f"  ========================\n\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
