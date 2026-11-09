"""Shared TLS contexts for outbound httpx clients.

Constructing ``httpx.AsyncClient()`` from scratch runs
``ssl.create_default_context`` synchronously on the calling thread — the event
loop, for every async caller in this codebase. On Windows that walk of the
system certificate store costs 60-900ms per client, and the gateway builds a
fresh client per request (quota checks, token refresh, chat proxying), which
froze the loop for seconds and stalled every admin page with it. Passing
``verify=TLS_CONTEXT`` reuses one context and turns that cost into a one-time
import hit.

The env-var branches mirror httpx's own ``create_ssl_context``. Clients using
``trust_env=False`` must use ``TLS_CONTEXT_NO_ENV`` to retain their CA policy.
"""

from __future__ import annotations

import os
import ssl


def _build_context(*, trust_env: bool = True) -> ssl.SSLContext:
    if trust_env and os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    if trust_env and os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context(capath=os.environ["SSL_CERT_DIR"])
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


TLS_CONTEXT_NO_ENV = _build_context(trust_env=False)
TLS_CONTEXT = (
    _build_context()
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")
    else TLS_CONTEXT_NO_ENV
)
