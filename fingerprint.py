"""
fingerprint.py — Work Buddy / CodeBuddy 官方客户端请求头指纹模拟

综合 fingerprint-refs 中四份逆向参考实现的共识字段构建，让出站请求与
官方 CLI 客户端（CLI/x.y CodeBuddy/x.y）在请求头层面保持一致：

  - workbuddy2api_sliv（Go）：common / chat / billing / refresh 头规范、
    Origin/Referer 地域规则、X-No-* 缺省字段约定
  - workbuddy2api_wug（TS）：X-IDE-Type/Name/Version、x-codebuddy-request、X-Product
  - workbuddy2api_orange（TS）：X-B3-* / b3 分布式追踪头、X-Agent-Intent、
    X-Conversation-ID / X-Conversation-Request-ID / X-Conversation-Message-ID
  - workbuddy2api_xue（Python）：x-stainless-* SDK 指纹头

可用环境变量覆盖（默认值均为逆向捕获到的官方值）：

  CB_GATEWAY_USER_AGENT                 完整 User-Agent，默认 CLI/<v> CodeBuddy/<v>
  CB_GATEWAY_IDE_VERSION                CLI 版本号，默认 2.109.2
  CB_GATEWAY_STAINLESS_OS               上报的操作系统，默认按当前平台推断
  CB_GATEWAY_STAINLESS_PACKAGE_VERSION  x-stainless-package-version，默认 5.10.1
  CB_GATEWAY_NODE_VERSION               x-stainless-runtime-version，默认 v22.13.1
"""

import os
import platform
import secrets
import uuid
from typing import Optional

DEFAULT_IDE_VERSION = "2.109.2"

_CN_ORIGIN = "https://www.codebuddy.cn"
_GLOBAL_ORIGIN = "https://www.workbuddy.ai"

STAINLESS_PACKAGE_VERSION = os.environ.get(
    "CB_GATEWAY_STAINLESS_PACKAGE_VERSION", "5.10.1"
)
NODE_RUNTIME_VERSION = os.environ.get("CB_GATEWAY_NODE_VERSION", "v22.13.1")


def ide_version() -> str:
    """CLI 版本号，驱动 User-Agent 与 X-IDE-Version。"""
    return (os.environ.get("CB_GATEWAY_IDE_VERSION") or "").strip() or DEFAULT_IDE_VERSION


def user_agent() -> str:
    """官方 CLI User-Agent。可用 CB_GATEWAY_USER_AGENT 整体覆盖。"""
    override = (os.environ.get("CB_GATEWAY_USER_AGENT") or "").strip()
    if override:
        return override
    v = ide_version()
    return f"CLI/{v} CodeBuddy/{v}"


def stainless_os() -> str:
    """官方 CLI 上报的操作系统。默认按当前平台推断。"""
    override = (os.environ.get("CB_GATEWAY_STAINLESS_OS") or "").strip()
    if override:
        return override
    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "macOS"
    return "Linux"


def origin_for(domain: str) -> str:
    """按账号域选择 Origin/Referer：workbuddy.ai 国际版，否则中国版。"""
    if "workbuddy" in (domain or "").lower():
        return _GLOBAL_ORIGIN
    return _CN_ORIGIN


def _trace_id() -> str:
    return uuid.uuid4().hex


def _span_id() -> str:
    return secrets.token_hex(8)


def _domain_headers(account: dict) -> dict:
    domain = (account.get("domain") or "").strip()
    if domain:
        return {"X-Domain": domain}
    # 与官方 CLI 一致：字段缺失时用 X-No-* 标记，而不是发空值
    return {"X-No-Department-Info": "1"}


def _account_header(account: dict, field: str, header: str, no_header: str) -> dict:
    value = str(account.get(field) or "").strip()
    if value:
        return {header: value}
    return {no_header: "1"}


def common_headers(account: dict) -> dict:
    """所有 API 共享的通用指纹头（含 B3 追踪头）。"""
    domain = (account.get("domain") or "").strip()
    origin = origin_for(domain)
    request_id = _trace_id()
    span_id = _span_id()
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "X-Product": "SaaS",
        "User-Agent": user_agent(),
        "X-Request-ID": request_id,
        "X-B3-TraceId": request_id,
        "X-B3-SpanId": span_id,
        "X-B3-Sampled": "1",
        "b3": f"{request_id}-{span_id}-1-",
        **_domain_headers(account),
    }


def chat_headers(account: dict) -> dict:
    """Chat 请求头：通用指纹 + 账号头 + IDE/CLI 指纹 + SDK 指纹。

    安全红线：chat 请求绝不携带 X-Refresh-Token。
    """
    headers = common_headers(account)
    headers.update(_account_header(account, "access_token", "Authorization", "X-No-Authorization"))
    headers.update(_account_header(account, "uid", "X-User-Id", "X-No-User-Id"))
    headers.update(
        _account_header(account, "enterprise_id", "X-Enterprise-Id", "X-No-Enterprise-Id")
    )
    if headers.get("Authorization"):
        headers["Authorization"] = f"Bearer {headers['Authorization']}"
    enterprise_id = str(account.get("enterprise_id") or "").strip()
    if enterprise_id:
        headers["X-Tenant-Id"] = enterprise_id

    v = ide_version()
    headers.update(
        {
            "X-IDE-Type": "CLI",
            "X-IDE-Name": "CLI",
            "X-IDE-Version": v,
            "x-codebuddy-request": "1",
            "X-Agent-Intent": "craft",
            # 无状态网关按官方 CLI 规则为每次请求生成全新会话标识
            "X-Conversation-ID": str(uuid.uuid4()),
            "X-Conversation-Request-ID": uuid.uuid4().hex,
            "X-Conversation-Message-ID": uuid.uuid4().hex,
            "x-stainless-arch": "x64",
            "x-stainless-lang": "js",
            "x-stainless-os": stainless_os(),
            "x-stainless-package-version": STAINLESS_PACKAGE_VERSION,
            "x-stainless-retry-count": "0",
            "x-stainless-runtime": "node",
            "x-stainless-runtime-version": NODE_RUNTIME_VERSION,
        }
    )
    return headers


def billing_headers(account: dict) -> dict:
    """Billing 接口（余额/积分）请求头指纹。"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Product": "SaaS",
        "User-Agent": user_agent(),
        **_domain_headers(account),
    }
    headers.update(_account_header(account, "access_token", "Authorization", "X-No-Authorization"))
    if headers.get("Authorization"):
        headers["Authorization"] = f"Bearer {headers['Authorization']}"
    headers.update(_account_header(account, "uid", "X-User-Id", "X-No-User-Id"))
    headers.update(
        _account_header(account, "enterprise_id", "X-Enterprise-Id", "X-No-Enterprise-Id")
    )
    enterprise_id = str(account.get("enterprise_id") or "").strip()
    if enterprise_id:
        headers["X-Tenant-Id"] = enterprise_id
    return headers


def refresh_headers(account: dict) -> dict:
    """Token 刷新接口请求头。X-Refresh-Token 只允许出现在这里。"""
    headers = common_headers(account)
    headers["Accept"] = "application/json"
    headers["Cache-Control"] = "no-cache"
    headers["Pragma"] = "no-cache"
    access_token = str(account.get("access_token") or "").strip()
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    headers["X-Refresh-Token"] = str(account.get("refresh_token") or "")
    headers["X-Auth-Refresh-Source"] = "plugin"
    headers.update(_account_header(account, "uid", "X-User-Id", "X-No-User-Id"))
    headers.update(
        _account_header(account, "enterprise_id", "X-Enterprise-Id", "X-No-Enterprise-Id")
    )
    return headers
