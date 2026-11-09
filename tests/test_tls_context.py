import importlib
import ssl
from pathlib import Path

import certifi
import httpx
import pytest
from httpx._config import create_ssl_context

import tls_context


@pytest.mark.parametrize("cert_source", ["file", "dir", "both", "default"])
def test_shared_contexts_preserve_httpx_ca_policy(monkeypatch, tmp_path, cert_source):
    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    first_cert = bundle[bundle.index("-----BEGIN CERTIFICATE-----"):].split("-----END CERTIFICATE-----")[0]
    custom = tmp_path / "custom-ca.pem"
    custom.write_text(first_cert + "-----END CERTIFICATE-----\n", encoding="utf-8")
    ca_dir = tmp_path / "ca-dir"
    ca_dir.mkdir()

    # Reload with the environment already set, as it is at process startup.
    # Restore the module afterwards so other tests retain the normal policy.
    try:
        with monkeypatch.context() as env:
            env.delenv("SSL_CERT_FILE", raising=False)
            env.delenv("SSL_CERT_DIR", raising=False)
            if cert_source in {"file", "both"}:
                env.setenv("SSL_CERT_FILE", str(custom))
            if cert_source in {"dir", "both"}:
                env.setenv("SSL_CERT_DIR", str(ca_dir))
            importlib.reload(tls_context)
            for trust_env, context in (
                (True, tls_context.TLS_CONTEXT),
                (False, tls_context.TLS_CONTEXT_NO_ENV),
            ):
                expected = create_ssl_context(trust_env=trust_env)
                with httpx.Client(verify=context, trust_env=trust_env) as client:
                    actual = client._transport._pool._ssl_context
                    assert actual is context
                    assert set(actual.get_ca_certs(binary_form=True)) == set(expected.get_ca_certs(binary_form=True))
                    assert actual.verify_mode == ssl.CERT_REQUIRED
                    assert actual.check_hostname is True
    finally:
        importlib.reload(tls_context)
