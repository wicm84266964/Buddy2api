import asyncio

import pytest

import control_plane
import credential_crypto
import database as db
import auth_manager


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_create_key_requires_channel(isolated_db):
    from fastapi import HTTPException
    import server

    with pytest.raises(HTTPException) as err:
        server._validate_key_channel("")
    assert err.value.status_code == 400


def test_preview_import_roundtrip(isolated_db, tmp_path, monkeypatch):
    info = tmp_path / "a.info"
    info.write_text(
        '{"account":{"uid":"u-1","nickname":"n"},"auth":{"accessToken":"tok","refreshToken":"ref"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(auth_manager, "candidate_auth_dirs", lambda auth_dir=None: [tmp_path])
    monkeypatch.setattr(auth_manager, "find_auth_files", lambda auth_dir=None: [info])
    monkeypatch.setattr(
        auth_manager,
        "discover_auth_files",
        lambda auth_dir=None: {
            "dirs": [{"path": str(tmp_path), "exists": True, "file_count": 1}],
            "files": [
                {
                    "path": str(info),
                    "valid": True,
                    "already_imported": False,
                    "account_name": "n",
                    "uid_masked": "u-1",
                }
            ],
            "file_count": 1,
            "valid_count": 1,
        },
    )
    preview = control_plane.discover("workbuddy")
    assert preview["preview_token"]
    result = control_plane.import_channel("workbuddy", preview["preview_token"], [str(info)])
    assert result["imported"] == 1
    rows = db.list_accounts(provider="workbuddy")
    assert len(rows) == 1
    assert rows[0]["access_token"] == "tok"

    info.write_text(
        '{"account":{"uid":"u-1","nickname":"n"},"auth":{"accessToken":"tok2","refreshToken":"ref2"}}',
        encoding="utf-8",
    )
    db.update_account(rows[0]["id"], {"weight": 7, "priority": 3})
    preview = control_plane.discover("workbuddy")
    result = control_plane.import_channel("workbuddy", preview["preview_token"], [str(info)])
    assert result["updated"] == 1
    row = db.get_account(rows[0]["id"])
    assert row["access_token"] == "tok2"
    assert row["weight"] == 7
    assert row["priority"] == 3


def test_discover_marks_docker_host_auth_limited(isolated_db, monkeypatch):
    monkeypatch.setenv("CB_DOCKER", "1")
    qclaw = control_plane.discover("qclaw")
    assert qclaw["runtime"]["container"] is True
    assert qclaw["runtime"]["host_auth_limited"] is True
    workbuddy = control_plane.discover("workbuddy")
    assert workbuddy["runtime"]["container"] is True
    assert workbuddy["runtime"]["host_auth_limited"] is False


def test_credit_summary_has_null_total(isolated_db):
    payload = asyncio.run(control_plane.credit_summary())
    assert payload["total_balance"] is None
    assert "channels" in payload
    assert any(item["id"] == "workbuddy" for item in payload["channels"])


def test_credit_summary_qclaw_uses_token_unit(isolated_db, monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw")
    db.add_account(
        {
            "name": "qc",
            "uid": "q1",
            "provider": "qclaw",
            "access_token": "sk",
            "status": "active",
        }
    )
    from providers.protocol import QuotaSnapshot
    from providers.qclaw import PROVIDER

    async def fake_quota(account):
        return QuotaSnapshot(
            ok=True,
            channel="qclaw",
            account_id=int(account.get("id") or 0),
            unit="token",
            remaining=40,
            extra={"used": 10, "limit": 50},
            unsupported=False,
        )

    monkeypatch.setattr(PROVIDER, "fetch_quota", fake_quota)
    payload = asyncio.run(control_plane.credit_summary())
    assert payload["total_balance"] is None
    qclaw = next(item for item in payload["channels"] if item["id"] == "qclaw")
    assert qclaw["unit"] == "token"
    assert qclaw["remaining"] == 40
    assert qclaw["used"] == 10
    assert qclaw["limit"] == 50
    assert qclaw["unsupported"] is False
    workbuddy = next(item for item in payload["channels"] if item["id"] == "workbuddy")
    assert workbuddy["unit"] == "credit"


def test_startup_does_not_import_by_default(isolated_db, monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_AUTO_IMPORT", raising=False)
    called = []

    def fake_discover(channel=None, auth_dir=None):
        called.append(channel or "workbuddy")
        return {"files": [], "dirs": [], "preview_token": "x"}

    monkeypatch.setattr(control_plane, "discover", fake_discover)
    monkeypatch.setattr(
        control_plane,
        "import_channel",
        lambda *args, **kwargs: called.append("imported") or {"imported": 1},
    )
    summary = control_plane.startup_scan()
    assert summary["auto_import"] is False
    assert "imported" not in called
