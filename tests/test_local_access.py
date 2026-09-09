import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import server


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setattr(server, "LOCAL_MODE", True)
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", False)
    monkeypatch.setattr(server, "ADMIN_TOKEN", "unused")
    monkeypatch.setattr(server.db, "get_all_settings", lambda: {"timeout": 300})


def request(path="/admin/settings", *, peer="127.0.0.1", base="http://127.0.0.1:8787", headers=None):
    async def run():
        transport = httpx.ASGITransport(app=server.app, client=(peer, 12345))
        async with httpx.AsyncClient(transport=transport, base_url=base) as client:
            return await client.get(path, headers=headers)
    return asyncio.run(run())


def test_local_management_works_without_credentials_and_with_stale_credentials(local_mode):
    assert request().status_code == 200
    assert request(headers={"Cookie": "cb_gw_admin_token=stale", "Authorization": "Bearer stale"}).status_code == 200
    assert request(headers={"Origin": "http://127.0.0.1:8787", "Sec-Fetch-Site": "same-origin"}).status_code == 200


@pytest.mark.parametrize("kwargs", [
    {"peer": "192.168.1.10"},
    {"peer": "192.168.1.10", "headers": {"X-Forwarded-For": "127.0.0.1"}},
    {"base": "http://attacker.example:8787"},
    {"headers": {"Origin": "https://attacker.example"}},
    {"headers": {"Origin": "http://127.0.0.1:9999"}},
    {"headers": {"Origin": "null"}},
    {"headers": {"Sec-Fetch-Site": "cross-site"}},
    {"headers": {"Sec-Fetch-Site": "same-site"}},
])
@pytest.mark.parametrize("path", ["/", "/admin/settings"])
def test_local_mode_rejects_untrusted_requests(local_mode, kwargs, path):
    assert request(path, **kwargs).status_code == 403


@pytest.mark.parametrize("host,peer", [("localhost", "127.0.0.1"), ("[::1]", "::1")])
def test_local_aliases(local_mode, host, peer):
    assert request(base=f"http://{host}:8787", peer=peer).status_code == 200


def test_home_does_not_cache_or_disclose_tokens(local_mode, monkeypatch):
    response = request("/")
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "const localMode=true;" in response.text
    monkeypatch.setattr(server, "LOCAL_MODE", False)
    monkeypatch.setattr(server, "ADMIN_TOKEN", "private-management-secret")
    response = request("/")
    assert "set-cookie" not in response.headers
    assert "private-management-secret" not in response.text
    assert "const localMode=false;" in response.text
    assert request().status_code == 401
    assert request(headers={"Cookie": "cb_gw_admin_token=private-management-secret"}).status_code == 401
    assert request(headers={"Authorization": "Bearer private-management-secret"}).status_code == 200


def test_client_api_key_is_still_required(local_mode, monkeypatch):
    monkeypatch.setattr(server, "ALLOW_UNAUTHENTICATED_API", False)
    monkeypatch.setattr(server.db, "list_api_keys", lambda: [])
    assert request("/v1/models").status_code == 503
    monkeypatch.setattr(server.db, "list_api_keys", lambda: [{"id": 1}])
    assert request("/v1/models").status_code == 401
    # Browser-based API clients keep using API keys and the configured CORS policy.
    assert request("/v1/models", headers={"Origin": "http://localhost:3000", "Sec-Fetch-Site": "cross-site"}).status_code == 401


def test_database_lock_releases_after_close(tmp_path, monkeypatch):
    monkeypatch.setattr(server.db, "DB_PATH", tmp_path / "test.db")
    first = server._lock_database()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            server._lock_database()
    finally:
        first.close()
    server._lock_database().close()


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_repeated_start_and_restart(tmp_path):
    root = Path(server.__file__).parent
    port = unused_port()
    env = os.environ.copy()
    env.pop("CB_GATEWAY_ADMIN_TOKEN", None)
    env.update(CB_GATEWAY_DB_PATH=str(tmp_path / "gateway.db"), CB_GATEWAY_AUTO_IMPORT="0", CB_GATEWAY_PROVIDERS="workbuddy", CB_AUTH_DIR=str(tmp_path / "no-auth"))
    command = [sys.executable, "server.py", "--no-browser", "--port", str(port)]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1) as client:
        # Keep the browser's old credentials across process restarts.
        client.cookies.set("cb_gw_admin_token", "stale")
        for cycle in range(2):
            with open(tmp_path / f"server-{cycle}.log", "w") as log:
                process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        assert process.poll() is None, (tmp_path / f"server-{cycle}.log").read_text()
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    else:
                        pytest.fail("Server did not start")
                    assert client.get("/admin/settings").status_code == 200
                    duplicate = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=20)
                    assert duplicate.returncode == 0, duplicate.stderr
                    assert "already running" in duplicate.stderr
                    assert process.poll() is None
                    assert client.get("/admin/settings").status_code == 200
                    conflict = subprocess.run(command[:-1] + [str(unused_port())], cwd=root, env=env, capture_output=True, text=True, timeout=20)
                    assert conflict.returncode != 0
                    assert "already in use" in conflict.stderr
                finally:
                    process.terminate()
                    process.wait(timeout=10)


def test_remote_listener_requires_explicit_token(tmp_path):
    env = os.environ.copy()
    env.pop("CB_GATEWAY_ADMIN_TOKEN", None)
    env["CB_GATEWAY_DB_PATH"] = str(tmp_path / "never-created.db")
    result = subprocess.run([sys.executable, server.__file__, "--host", "0.0.0.0", "--no-browser"], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "Remote access requires" in result.stderr
    assert not (tmp_path / "never-created.db").exists()
