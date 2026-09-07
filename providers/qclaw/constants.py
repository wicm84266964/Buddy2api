"""Official QClaw 0.2.36.629 protocol constants extracted from the desktop client."""

from __future__ import annotations

CHANNEL_ID = "qclaw"
DISPLAY_NAME = "QClaw"

# Hosts from official PRODUCTION_URLS in app.asar (0.2.36.629).
JPRX_GATEWAY = "https://jprx.m.qq.com"
AIZONE_BASE = "https://mmgrcalltoken.3g.qq.com/aizone/v1"
WX_LOGIN_REDIRECT = "https://security.guanjia.qq.com/login"
# Official QClaw WeChat Open Platform appid (public OAuth client id, not a secret).
WX_APP_ID = "wx9d11056dd75b7240"
WX_QRCONNECT = "https://open.weixin.qq.com/connect/qrconnect"

# Renderer HttpClient.buildJPrxCtxHeader in official 0.2.36.629:
# rnd = 32 chars [a-z0-9]; date = unix seconds;
# sg = md5(body + JPRX_SIGNATURE_KEY + rnd + date + gid)
# Official jprx business calls use this MD5 scheme, not HMAC-SHA256.
JPRX_SIGNATURE_KEY = "7fcd3045-3171-482b-9be4-0430bf8553b5"
JPRX_RND_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
JPRX_RND_LEN = 32
WEB_VERSION = "1.4.0"
CLIENT_VERSION = "0.2.36.629"
PRODUCT_ID = "1001"

CMD_WX_LOGIN_STATE = "4050"
CMD_WX_LOGIN = "4026"
CMD_USER_INFO = "4027"
CMD_CREATE_API_KEY = "4055"
CMD_REFRESH_CHANNEL = "4058"
CMD_TODAY_TOKENS = "4075"
CMD_USAGE_DETAILS = "4172"
CMD_MODEL_LIST = "4320"
CMD_TIME_SYNC = "4629"

# 4320 model_status_list ids observed on a logged-in 0.2.36.629 client.
STATIC_MODELS = (
    "default",
    "pool-hy3-preview",
    "pool-deepseek-v4-pro",
    "pool-deepseek-v4-flash",
    "pool-glm-5.2",
    "pool-glm-5.2-night",
    "pool-glm-5.1",
    "pool-kimi-k2.7-code-highspeed",
    "pool-kimi-k2.6",
    "pool-minimax-m3",
    "pool-minimax-m2.7",
)

ALIASES = {
    "auto": "default",
    "modelroute": "default",
}

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
