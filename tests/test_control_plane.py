import asyncio
import json

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


def test_claim_one_uses_traework_claim_not_workbuddy(monkeypatch):
    account = {"id": 3, "provider": "traework", "name": "tw", "nickname": "tw"}
    called = {}

    class Provider:
        async def claim_checkin(self, row):
            called["account_id"] = row["id"]
            return {"ok": True, "claimed": True, "account_id": row["id"], "message": "success"}

    monkeypatch.setattr(control_plane.providers, "get_provider", lambda channel: Provider() if channel == "traework" else None)

    async def workbuddy_claim(row):
        raise AssertionError("workbuddy claim")

    monkeypatch.setattr(auth_manager, "claim_daily_checkin", workbuddy_claim)
    result = asyncio.run(control_plane.claim_one(account))
    assert called["account_id"] == 3
    assert result["channel"] == "traework"
    assert result["claimed"] is True


def test_any_channel_key_is_allowed(isolated_db):
    import server

    assert server._validate_key_channel("*") == "*"


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
    qwenwork = control_plane.discover("qwenwork")
    assert qwenwork["runtime"]["container"] is True
    assert qwenwork["runtime"]["host_auth_limited"] is True
    workbuddy = control_plane.discover("workbuddy")
    assert workbuddy["runtime"]["container"] is True
    assert workbuddy["runtime"]["host_auth_limited"] is False


def test_credit_summary_has_null_total(isolated_db):
    payload = asyncio.run(control_plane.credit_summary())
    assert payload["total_balance"] is None
    assert "channels" in payload
    assert any(item["id"] == "workbuddy" for item in payload["channels"])


def test_credit_summary_skips_retired_qclaw(isolated_db, monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw")
    payload = asyncio.run(control_plane.credit_summary())
    assert payload["total_balance"] is None
    assert all(item["id"] != "qclaw" for item in payload["channels"])
    workbuddy = next(item for item in payload["channels"] if item["id"] == "workbuddy")
    assert workbuddy["unit"] == "credit"


def test_credit_summary_prefers_available_total_over_total_dosage(isolated_db, monkeypatch):
    account_id = db.add_account({
        "name": "exhausted", "uid": "exhausted-1", "provider": "workbuddy",
        "status": "active", "extra": {},
    })
    resource = {
        "ok": True,
        "status_code": 200,
        "account_id": account_id,
        "account_name": "exhausted",
        "total_dosage": 500.0,
        "available_total": 0.0,
        "expiring_7d_total": 0.0,
        "expiring_30d_total": 0.0,
        "package_count": 1,
        "packages": [],
        "updated_at": 0,
    }

    async def fake_fetch(account, *, force=False, max_age_seconds=60):
        return dict(resource)

    monkeypatch.setattr(auth_manager, "fetch_account_resources", fake_fetch)
    monkeypatch.setattr(control_plane.providers, "enabled_provider_ids", lambda: ["workbuddy"])
    payload = asyncio.run(control_plane.credit_summary(force=True))

    workbuddy = next(item for item in payload["channels"] if item["id"] == "workbuddy")
    assert workbuddy["remaining"] == 0
    assert payload["low_accounts"][0]["account_id"] == account_id
    assert payload["low_accounts"][0]["balance"] == 0
    assert payload["accounts"][0]["total_dosage"] == 500.0


def test_credit_summary_failed_resource_has_no_fake_zero_balance(isolated_db, monkeypatch):
    account_id = db.add_account({
        "name": "failed", "uid": "failed-1", "provider": "workbuddy",
        "status": "active", "extra": {},
    })
    resource = {
        "ok": False,
        "status_code": 503,
        "account_id": account_id,
        "account_name": "failed",
        "total_dosage": 500.0,
        "available_total": 0.0,
        "expiring_7d_total": 0.0,
        "expiring_30d_total": 0.0,
        "package_count": 0,
        "packages": [],
        "updated_at": 0,
    }

    async def fake_fetch(account, *, force=False, max_age_seconds=60):
        return dict(resource)

    monkeypatch.setattr(auth_manager, "fetch_account_resources", fake_fetch)
    monkeypatch.setattr(control_plane.providers, "enabled_provider_ids", lambda: ["workbuddy"])
    payload = asyncio.run(control_plane.credit_summary(force=True))

    workbuddy = next(item for item in payload["channels"] if item["id"] == "workbuddy")
    assert workbuddy["remaining"] == 0
    assert payload["low_accounts"] == []
    assert payload["accounts"][0]["ok"] is False

    resource.update(ok=True, unsupported=True, expiring_30d_total=10.0)
    payload = asyncio.run(control_plane.credit_summary(force=True))
    assert payload["expiring_accounts"][0]["balance"] is None
    assert payload["low_accounts"] == []


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


def test_encrypted_envelope_auth_file_is_rejected_not_imported(isolated_db, tmp_path):
    """accessToken 是 {"$wbEncrypted":1,"envelope":...} 时不能进库，也不能把 import 打成 500。"""
    info = tmp_path / "desktop.info"
    info.write_text(
        json.dumps({
            "account": {"uid": "u-enc", "nickname": {"$wbEncrypted": 1, "envelope": "abc"}},
            "auth": {
                "accessToken": {"$wbEncrypted": 1, "envelope": "abc"},
                "refreshToken": {"$wbEncrypted": 1, "envelope": "def"},
                "sessionState": "ss",
                "expiresAt": 1,
                "domain": "www.workbuddy.cn",
            },
        }),
        encoding="utf-8",
    )
    assert auth_manager.parse_auth_file(info) is None
    meta = auth_manager._safe_auth_file_meta(info, set())
    assert meta["valid"] is False
    # The encrypted-envelope file must not be importable -- whether the reason
    # mentions the envelope or the at-rest key recovery failure, it stays invalid.
    assert "envelope" in repr(meta["reason"]) or "信封" in meta["reason"] or "at-rest" in meta["reason"]
    assert db.list_accounts(provider="workbuddy") == []


def test_import_update_path_survives_encrypted_envelope_fields(isolated_db, tmp_path):
    """同 uid 走 update 分支时，dict 形 token 字段曾让 encrypt_secret 抛 AttributeError。"""
    plain = tmp_path / "plain.info"
    plain.write_text(
        '{"account":{"uid":"u-1","nickname":"n"},"auth":{"accessToken":"tok","refreshToken":"ref"}}',
        encoding="utf-8",
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(auth_manager, "candidate_auth_dirs", lambda auth_dir=None: [tmp_path])
    monkeypatch.setattr(auth_manager, "find_auth_files", lambda auth_dir=None: [plain])
    monkeypatch.setattr(
        auth_manager,
        "discover_auth_files",
        lambda auth_dir=None: {
            "dirs": [],
            "files": [{"path": str(plain), "valid": True, "already_imported": False, "account_name": "n", "uid_masked": "u-1"}],
            "file_count": 1,
            "valid_count": 1,
        },
    )
    preview = control_plane.discover("workbuddy")
    assert control_plane.import_channel("workbuddy", preview["preview_token"], [str(plain)])["imported"] == 1
    monkeypatch.undo()

    # 第二次导入同一个 uid：token 变成加密信封，走 update 分支，必须能落地而不是 500。
    plain.write_text(
        json.dumps({
            "account": {"uid": "u-1", "nickname": "n"},
            "auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "abc"}, "refreshToken": {"$wbEncrypted": 1, "envelope": "def"}},
        }),
        encoding="utf-8",
    )
    # parse_auth_file 现在拒绝加密信封，所以直接用 dict 形 patch 打 update_account，
    # 验证 dict 不再让 _protect_account_data / sqlite 绑定崩溃。
    db.update_account(1, {"access_token": {"$wbEncrypted": 1}, "refresh_token": {"$wbEncrypted": 1}, "uid": "u-1"})
    row = db.get_account(1)
    assert row["access_token"] == ""
    assert row["refresh_token"] == ""
    assert row["uid"] == "u-1"


def test_encrypt_secret_ignores_non_string_values(isolated_db):
    assert credential_crypto.encrypt_secret({"$wbEncrypted": 1}, isolated_db) == ""
    assert credential_crypto.encrypt_secret(None, isolated_db) == ""
    assert credential_crypto.is_encrypted({"$wbEncrypted": 1}) is False
    encrypted = credential_crypto.encrypt_secret("plain", isolated_db)
    assert credential_crypto.is_encrypted(encrypted) is True
    assert credential_crypto.decrypt_secret(encrypted, isolated_db) == "plain"
