"""Official QwenWorkCN 0.1.8-26081406 protocol constants.

RSA PEM was extracted from the official desktop asar
(`resources/app.asar`, `RSA_PUBLIC_KEY` used by generateAuthToken).
Cosy-Version was frozen from bundled qoderclicn (`l0A = "1.1.18"`).
"""

from __future__ import annotations

CHANNEL_ID = "qwenwork"
DISPLAY_NAME = "QwenWork / 千问办公"

GATEWAY = "https://gateway.qwenwork.cn"
CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
CHAT_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common"
REFRESH_PATH = "/api/v1/deviceToken/refresh"
ACCOUNT_CONTEXT_PATH = "/api/v1/adapter/user/account-context"
MODELS_PATH = "/api/v2/model/list"

IDE_VERSION = "0.1.8"
RELEASE_VERSION = "0.1.8-26081406"
BUILD = "26081406"
COSY_VERSION = "1.1.18"
COSY_VERSION_FROZEN = True
CLIENT_TYPE = "6"
BUSINESS_PRODUCT = "qoder_work"
BUSINESS_TYPE = "agent"
SCENE = "qwork"
MACHINE_OS = "x86_64_win32"
LOGIN_VERSION = "v2"
USER_AGENT = "qoderwork/0.1.8"

# Official 0.1.8 asar generateAuthToken public key (PKCS#1 v1.5).
RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""

STATIC_MODELS = (
    "pro",
    "flash",
    "qwen3.8-max-preview",
    "qwork-advanced",
    "qwork-auto",
    "qwork-lite",
    "qmodel_latest",
)

ALIASES = {
    "auto": "pro",
    "qwork-advanced": "pro",
}

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
