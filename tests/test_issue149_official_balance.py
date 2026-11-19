"""Regression checks for GitHub issue #149 (exhausted CodeBuddy packages)."""

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import auth_manager
import control_plane
import database as db


HTML_PATH = Path(__file__).resolve().parents[1] / "web" / "index.html"


def test_exhausted_unexpired_package_has_zero_available_total(tmp_path, monkeypatch):
    """Upstream TotalDosage is a package size, not spendable balance."""
    import asyncio
    from unittest.mock import patch

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    db.init_db()
    monkeypatch.setattr(auth_manager, "backend_url", lambda account=None: "https://invalid.test")
    monkeypatch.setattr(auth_manager, "request_timeout", lambda default: default)
    now = int(time.time())
    future = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 7 * 86400))
    payload = {
        "Response": {
            "Data": {
                "TotalDosage": 500,
                "TotalCount": 1,
                "Accounts": [{
                    "PackageName": "exhausted",
                    "CycleStartTime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 86400)),
                    "CycleEndTime": future,
                    "ExpiredTime": future,
                    "CapacitySizePrecise": 500,
                    "CapacityRemainPrecise": 0,
                    "CapacityUsedPrecise": 500,
                    "CycleCapacitySizePrecise": 500,
                    "CycleCapacityRemainPrecise": 0,
                    "CycleCapacityUsedPrecise": 500,
                    "Status": 1,
                }],
            }
        }
    }

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return FakeResponse()

    async def fake_headers(account):
        return {"Authorization": "test-only"}

    async def run():
        with patch.object(auth_manager, "get_billing_headers", fake_headers), \
             patch.object(auth_manager.httpx, "AsyncClient", FakeClient), \
             patch.object(auth_manager.db, "upsert_account_resource_cache", lambda *a, **kw: None):
            return await auth_manager.fetch_account_resources(
                {"id": 149, "name": "issue149"}, force=True,
            )

    result = asyncio.run(run())
    package = result["packages"][0]
    assert result["total_dosage"] == 500
    assert result["available_total"] == 0
    assert package["remaining_precise"] == 0
    assert package["cycle_used"] == 500
    assert package["expired"] is False


def test_spendable_balance_keeps_explicit_zero():
    assert control_plane._spendable_balance({
        "remaining": "", "available_total": 0, "total_dosage": 500,
    }) == 0
    assert control_plane._spendable_balance({
        "remaining": 0, "available_total": 11, "total_dosage": 500,
    }) == 0
    assert control_plane._spendable_balance({
        "remaining": None, "available_total": 7, "total_dosage": 500,
    }) == 7
    assert control_plane._spendable_balance({"total_dosage": 500}) == 500
    assert control_plane._spendable_balance({"available_total": "bad", "total_dosage": 9}) == 9
    assert control_plane._spendable_balance({}) == 0
    assert control_plane._spendable_balance({
        "remaining": float("nan"), "available_total": float("inf"), "total_dosage": 6,
    }) == 6
    assert control_plane._spendable_balance({
        "remaining": "NaN", "available_total": "Infinity", "total_dosage": "-Infinity",
    }) == 0.0
    assert control_plane._spendable_balance({
        "remaining": "bad", "available_total": 0, "total_dosage": 500,
    }) == 0.0


def test_accounts_page_uses_spendable_balance_and_package_status():
    html = HTML_PATH.read_text(encoding="utf-8")
    assert "function firstFiniteNumber(" in html
    assert "if(!r||!r.ok||r.unsupported)return null" in html
    assert "r.remaining,r.available_total,r.total_dosage" in html
    assert "r.remaining??r.available_total??r.total_dosage" not in html
    assert "r.remaining??r.total_dosage??r.available_total" not in html
    assert "function officialSpendable(r){if(!r||r.ok===false" not in html
    assert "function officialSpendable(r)" in html
    assert "return officialSpendable(a.official_resource)" in html
    assert "{{officialSpendable(pkg.resource)===null?'—':credit(officialSpendable(pkg.resource))}}" in html
    assert "{{credit(officialSpendable(pkg.resource))}}" not in html
    assert "pkg.resource.total_dosage??pkg.resource.remaining" not in html
    assert "x.expired?'已过期':'可用'" not in html
    assert ":class=\"pkgStatusClass(x)\">{{pkgStatusText(x)}}" in html
    assert "function pkgStatusText(x)" in html
    assert "function pkgAmount(v)" in html
    assert "{{pkgAmount(x.remaining_precise)}}" in html
    assert "{{pkgAmount(x.cycle_used??x.used)}}" in html
    assert "{{pkgAmount(x.cycle_size??x.size)}}" in html
    assert "credit(x.remaining_precise)" not in html
    assert "credit(x.cycle_used||x.used)" not in html
    assert "credit(x.cycle_size||x.size)" not in html
    assert "claimOf,officialSpendable,pkgAmount,officialBalance" in html
    assert "pkgBadge,pkgStatusText,pkgStatusClass" in html


def test_browser_helpers_match_spendable_balance_truth_table(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = HTML_PATH.read_text(encoding="utf-8")
    names = ("firstFiniteNumber", "officialUnit", "officialSpendable", "officialBalance",
             "officialHasValue", "pkgStatusText", "pkgStatusClass", "pkgAmount")
    defs = []
    for name in names:
        match = re.search(rf"function {name}\([^\n]*\}}\n", html)
        assert match, f"missing helper: {name}"
        defs.append(match.group(0))
    script = tmp_path / "issue149_helpers.js"
    script.write_text(
        "\n".join(defs) + r"""
const assert = require('assert');
const credit = v => Number(v||0).toLocaleString('zh-CN',{maximumFractionDigits:4});
const eq = (actual, expected, label) => assert.strictEqual(actual, expected, label);
const base = {ok:true, unit:'credit', remaining:null, available_total:null, total_dosage:null};
const r = patch => ({...base, ...patch});
const a = resource => ({official_resource:resource, provider:'workbuddy'});
eq(officialBalance(a(r({available_total:0,total_dosage:500}))), 0, 'exhausted available_total wins');
eq(officialBalance(a(r({remaining:'',available_total:0,total_dosage:500}))), 0, 'empty remaining skipped');
eq(officialBalance(a(r({remaining:'bad',available_total:0,total_dosage:500}))), 0, 'bad remaining skipped');
eq(officialBalance(a(r({total_dosage:500}))), 500, 'legacy total_dosage fallback');
eq(officialBalance(a(r({remaining:7,available_total:0,total_dosage:500}))), 7, 'remaining wins');
eq(officialBalance(a(r({remaining:'NaN',available_total:'Infinity',total_dosage:8}))), 8, 'non-finite values skipped');
eq(officialBalance(a(r({unit:'token',remaining:42,total_dosage:500}))), 42, 'token remaining');
eq(officialBalance(a(r({unit:'token',total_dosage:500}))), null, 'token without remaining');
eq(officialBalance(a(r({ok:false,total_dosage:500}))), null, 'failed resource');
eq(officialBalance(a({unit:'credit',total_dosage:500})), null, 'unknown ok');
eq(officialBalance(a(r({unsupported:true,total_dosage:500}))), null, 'unsupported resource');
eq(officialBalance(a(null)), null, 'missing resource');
eq(officialHasValue(a(r({unit:'token',used:2,limit:3}))), true, 'token has value');
eq(officialHasValue(a(r({available_total:0,total_dosage:500}))), true, 'credit zero has value');
eq(officialHasValue(a(r({unit:'token',ok:false,used:2}))), false, 'failed token has no value');
const EXPIRED='\u5df2\u8fc7\u671f', USED='\u5df2\u7528\u5c3d', SOON='\u5373\u5c06\u5230\u671f', OK='\u53ef\u7528';
const status = (x, text, cls) => {eq(pkgStatusText(x), text, 'status text '+JSON.stringify(x));eq(pkgStatusClass(x), cls, 'status class '+JSON.stringify(x))};
status({expired:true,days_to_expire:3}, EXPIRED, 'err');
status({expired:false,days_to_expire:-1}, EXPIRED, 'err');
status({remaining_precise:0}, USED, 'err');
status({remaining_precise:'0',days_to_expire:3}, USED, 'err');
status({remaining_precise:null,used:0,size:300,cycle_size:300}, OK, 'ok');
status({remaining_precise:null,cycle_used:300,cycle_size:300}, USED, 'err');
status({remaining_precise:null,used:12,size:10}, USED, 'err');
status({remaining_precise:'bad',used:11,size:10}, USED, 'err');
status({remaining_precise:1,days_to_expire:3,cycle_used:300,cycle_size:300}, SOON, 'inactive');
status({remaining_precise:1,days_to_expire:30,cycle_used:300,cycle_size:300}, OK, 'ok');
status({remaining_precise:1,days_to_expire:null,cycle_used:300,cycle_size:300}, OK, 'ok');
eq(pkgAmount(null), '\u2014', 'null amount is unknown');
eq(pkgAmount(''), '\u2014', 'blank amount is unknown');
eq(pkgAmount(0), credit(0), 'zero amount is visible');
eq(pkgAmount('bad'), '\u2014', 'bad amount is unknown');
console.log('issue149 browser helpers OK');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, (result.stdout, result.stderr)
    print(result.stdout.strip())
