"""桌面端 at-rest 信封（``$wbEncrypted``）导入回归测试。

背景：桌面客户端自 2026-09-24 起把 ``accessToken`` / ``refreshToken``（国内版还有
``nickname`` / ``phoneNumber``）从明文改成
``{"$wbEncrypted": 1, "envelope": base64(...)}`` 信封，framing 为 ``"field"``，
与 ``wb_atrest.seal(key, value, "field")`` 完全同构（换号工具写出来的登录文件也是这个格式）。

修复前的两条故障：
  1. ``credential_crypto.is_encrypted(dict)`` 抛 ``AttributeError`` →
     ``POST /admin/accounts/scan`` 恒 500（整批扫描被一个信封带崩）；
  2. 即便不崩，把信封文本当 token 存进库 → 之后每个请求都 401。
"""

import json

import pytest
import database as db

import auth_manager
import credential_crypto
import wb_atrest

KEY = bytes(range(32))


def _sealed(text: str) -> dict:
    return wb_atrest.seal(KEY, text, "field")


def _write_auth_doc(path, access, refresh, nickname, phone, domain="www.workbuddy.cn"):
    doc = {
        "auth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": 1_800_000_000_000,
            "refreshExpiresAt": 1_800_000_000_000,
            "domain": domain,
            "sessionState": "sess-1",
        },
        "account": {"uid": "uid-1", "nickname": nickname, "phoneNumber": phone},
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_is_encrypted_ignores_atrest_envelope():
    assert credential_crypto.is_encrypted({"$wbEncrypted": 1, "envelope": "x"}) is False
    assert credential_crypto.is_encrypted(None) is False
    assert credential_crypto.is_encrypted("") is False
    assert credential_crypto.is_encrypted(credential_crypto.PREFIX + "fernet:x") is True


@pytest.fixture(autouse=True)
def fixed_key(monkeypatch):
    monkeypatch.setattr(wb_atrest, "recover_key", lambda: KEY)


def test_parse_auth_file_decrypts_envelope_fields(tmp_path):
    doc = _write_auth_doc(tmp_path / "enc.info", _sealed("tok-at"),
                          _sealed("tok-rt"), _sealed("昵称"), _sealed("13800000000"))
    parsed = auth_manager.parse_auth_file(doc)
    assert parsed is not None
    assert parsed["access_token"] == "tok-at"
    assert parsed["refresh_token"] == "tok-rt"
    assert parsed["nickname"] == parsed["name"] == "昵称"
    assert parsed["phone"] == "13800000000"


def test_parse_auth_file_keeps_plaintext_docs_working(tmp_path, monkeypatch):
    def unexpected():
        raise AssertionError("plaintext must not recover a key")
    monkeypatch.setattr(wb_atrest, "recover_key", unexpected)
    doc = _write_auth_doc(tmp_path / "plain.info", "plain-at", "plain-rt", "老格式", "13900000000")
    parsed = auth_manager.parse_auth_file(doc)
    assert parsed["access_token"] == "plain-at"
    assert parsed["nickname"] == "老格式"


def test_parse_auth_file_skips_when_key_unavailable(tmp_path, monkeypatch):
    def unavailable():
        raise wb_atrest.AtRestError("client unavailable")
    monkeypatch.setattr(wb_atrest, "recover_key", unavailable)
    doc = _write_auth_doc(tmp_path / "nokey.info", _sealed("a"), _sealed("r"), "n", "p")
    assert auth_manager.parse_auth_file(doc) is None
    meta = auth_manager._safe_auth_file_meta(doc, set())
    assert not meta["valid"]
    assert "client unavailable" in meta["reason"]


def test_resolve_atrest_fields_passes_plain_values_through():
    out, err = auth_manager.resolve_atrest_fields({"access_token": "plain", "nickname": ""})
    assert out == {"access_token": "plain", "nickname": ""}
    assert err == ""


def test_serialized_envelope_import(tmp_path):
    doc = _write_auth_doc(tmp_path / "serialized.info", json.dumps(_sealed("a")),
                          json.dumps(_sealed("r")), "n", "p")
    assert auth_manager.parse_auth_file(doc)["access_token"] == "a"
    assert auth_manager.parse_auth_file(doc)["refresh_token"] == "r"


@pytest.mark.parametrize("bad", [
    {"$wbEncrypted": 1, "envelope": None}, {"$wbEncrypted": 1},
    {"$wbEncrypted": 1, "envelope": "invalid"}, {"unknown": "value"},
    ["token"], 42, True,
])
def test_invalid_credentials_do_not_abort_batch(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "atrest-test-key")
    credential_crypto.reset_cache()
    db.init_db()
    docs = tmp_path / "auth"
    docs.mkdir()
    _write_auth_doc(docs / "a-invalid.info", bad, "r", "n", "p")
    _write_auth_doc(docs / "b-valid.info", _sealed("good-at"), _sealed("good-rt"), "n", "p")
    try:
        result = auth_manager.auto_scan_and_import(str(docs))
        assert result == {"imported": 1, "updated": 0, "skipped": 1, "errors": []}
        accounts = db.list_accounts()
        assert len(accounts) == 1
        assert accounts[0]["access_token"] == "good-at"
        assert accounts[0]["refresh_token"] == "good-rt"
    finally:
        credential_crypto.reset_cache()


def test_failed_refresh_decryption_preserves_existing_account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "atrest-test-key")
    credential_crypto.reset_cache()
    db.init_db()
    try:
        aid = db.add_account({"uid": "uid-1", "access_token": "old-at", "refresh_token": "old-rt"})
        doc = _write_auth_doc(tmp_path / "partial.info", _sealed("new-at"),
                              {"$wbEncrypted": 1, "envelope": "bad"}, "n", "p")
        result = auth_manager.auto_scan_and_import(str(doc))
        assert result["skipped"] == 1
        assert db.get_account(aid)["access_token"] == "old-at"
        assert db.get_account(aid)["refresh_token"] == "old-rt"
    finally:
        credential_crypto.reset_cache()


def test_decryption_error_does_not_leak_to_next_file():
    _, error = auth_manager.resolve_atrest_fields({"access_token": {"$wbEncrypted": 1, "envelope": "bad"}})
    assert error
    out, error = auth_manager.resolve_atrest_fields({"access_token": _sealed("ok")})
    assert out["access_token"] == "ok"
    assert error == ""


def test_missing_module_never_passes_serialized_envelope(monkeypatch):
    monkeypatch.setattr(auth_manager, "_atrest", lambda: None)
    out, error = auth_manager.resolve_atrest_fields({"access_token": json.dumps(_sealed("a"))})
    assert out["access_token"] == ""
    assert error
    out, error = auth_manager.resolve_atrest_fields({"access_token": "plain"})
    assert out["access_token"] == "plain"
    assert error == ""


@pytest.mark.parametrize("document", [[], {"auth": None}, {"auth": []},
                                      {"account": None, "auth": {"accessToken": "a"}}])
def test_malformed_document_is_skipped_by_import_and_discovery(tmp_path, document):
    path = tmp_path / "malformed.info"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert auth_manager.parse_auth_file(path) is None
    assert not auth_manager._safe_auth_file_meta(path, set())["valid"]


def test_real_key_cache_unwraps_master_and_recovers_after_rotation(tmp_path, monkeypatch):
    # Restore the real function replaced by the autouse fixture.
    monkeypatch.undo()
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(tmp_path))
    protector1, master1 = KEY, bytes(range(32, 64))
    protector2, master2 = bytes(reversed(KEY)), bytes(range(64, 96))
    calls = []
    def write_blob(protector, master):
        blob = {"keyId": wb_atrest.derive_key_id(master), "slots": [{
            "type": "static-v1", "protectorKeyId": wb_atrest.derive_key_id(protector),
            "wrapped": wb_atrest.seal(protector, master, "file")}]}
        (tmp_path / "keyblob").write_text(json.dumps(blob), encoding="utf-8")
    def hunt(**kwargs):
        key = protector1 if not calls else protector2
        calls.append(key)
        return key.hex()
    monkeypatch.setattr(wb_atrest, "hunt_key", hunt)
    wb_atrest.forget_key()
    try:
        for master, protector in ((master1, protector1), (master2, protector2)):
            write_blob(protector, master)
            for _ in range(2):
                out, err = auth_manager.resolve_atrest_fields({
                    "access_token": wb_atrest.seal(master, "token", "field")})
                assert out["access_token"] == "token"
                assert err == ""
        assert calls == [protector1, protector2]
    finally:
        wb_atrest.forget_key()


def test_gcm_matches_independent_cryptography_implementation():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    for length in (0, 1, 15, 16, 17, 128):
        plaintext = bytes(range(length))
        sealed = wb_atrest.seal(KEY, plaintext, "field", nonce=bytes(range(12)))
        env = wb_atrest.parse_envelope(sealed)
        aad = wb_atrest.aad(env["keyId"], env["suite"], "field")
        encrypted = AESGCM(KEY).encrypt(env["nonce"], plaintext, aad)
        assert encrypted == env["ciphertext"] + env["authTag"]
        assert wb_atrest.open_envelope(KEY, sealed, "field") == plaintext
    env["authTag"] = bytes([env["authTag"][0] ^ 1]) + env["authTag"][1:]
    with pytest.raises(wb_atrest.AtRestError):
        wb_atrest._gcm_open(KEY, env["nonce"], aad, env["ciphertext"], env["authTag"])


def test_field_sealed_with_wrapper_key_imports(tmp_path, monkeypatch):
    """2026-10 起的客户端用「托管包装密钥」加密 .info 字段（不是 keyblob 主密钥）。

    信封 keyId == static-v1 槽的 protectorKeyId，与 recover_key() 返回的主密钥
    派生出的 keyId 不同 → 只用主密钥解会全部失败（scan 报 skipped、渠道 0 active），
    必须按信封 keyId 在 [包装密钥, 主密钥] 里自动选钥。
    """
    # 还原被 autouse fixture 替换掉的 recover_key，走真实 keyblob 路径。
    monkeypatch.undo()
    monkeypatch.setenv("WORKBUDDY_CONFIG_DIR", str(tmp_path))
    protector, master = KEY, bytes(range(32, 64))
    blob = {"keyId": wb_atrest.derive_key_id(master), "slots": [{
        "type": "static-v1", "protectorKeyId": wb_atrest.derive_key_id(protector),
        "wrapped": wb_atrest.seal(protector, master, "file")}]}
    (tmp_path / "keyblob").write_text(json.dumps(blob), encoding="utf-8")
    monkeypatch.setattr(wb_atrest, "hunt_key", lambda **kwargs: protector.hex())
    wb_atrest.forget_key()
    try:
        doc = _write_auth_doc(
            tmp_path / "wrapper.info",
            wb_atrest.seal(protector, "tok-at", "field"),
            wb_atrest.seal(protector, "tok-rt", "field"),
            "昵称", "13800000000")
        parsed = auth_manager.parse_auth_file(doc)
        assert parsed is not None
        assert parsed["access_token"] == "tok-at"
        assert parsed["refresh_token"] == "tok-rt"
        assert parsed["nickname"] == "昵称"
    finally:
        wb_atrest.forget_key()


def test_key_recovery_failure_keeps_at_rest_reason():
    """密钥回收失败时原因串须仍是 at-rest 口径（各字段不重复刷错）。

    上游 test_control_plane 依赖该原因串含 "at-rest"；Linux CI 上 recover_key 必然抛
    「只支持 Windows」，若不提前统一取钥，会变成逐字段的「xxx 解密失败」而丢失口径。
    """
    sealed = _sealed("tok")
    def unavailable():
        raise wb_atrest.AtRestError("只支持 Windows")
    original = wb_atrest.recover_key
    wb_atrest.recover_key = unavailable
    try:
        out, err = auth_manager.resolve_atrest_fields({"access_token": sealed, "nickname": "n"})
    finally:
        wb_atrest.recover_key = original
    assert out["access_token"] == ""
    assert out["nickname"] == "n"
    assert "at-rest" in err
    assert err.count("解密失败") == 0
