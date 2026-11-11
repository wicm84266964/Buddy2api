import asyncio
import json
from pathlib import Path

import pytest

import self_update
import server
from version import VERSION


@pytest.fixture(autouse=True)
def _reset_update_cache(monkeypatch):
    monkeypatch.delenv("CB_GATEWAY_FAKE_LATEST", raising=False)
    self_update.reset_cache()
    yield
    self_update.reset_cache()


def test_parse_and_compare_versions():
    assert self_update.parse_version_file('VERSION = "2.1.14"\n') == "2.1.14"
    assert self_update.parse_release_tag(json.dumps({"tag_name": "v2.1.14"})) == "2.1.14"
    assert self_update.is_newer("2.1.14", "2.1.13")
    assert self_update.is_newer("2.1.13", "2.1.9")
    assert not self_update.is_newer("2.1.13", "2.1.13")
    assert not self_update.is_newer("2.1.12", "2.1.13")


def test_check_uses_first_reachable_source(monkeypatch):
    async def fake_get(url):
        if "cdn.jsdelivr.net" in url:
            return 200, 'VERSION = "9.9.9"\n'
        raise AssertionError(url)

    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    payload = asyncio.run(self_update.check(force=True))
    assert payload["checked"] is True
    assert payload["latest"] == "9.9.9"
    assert payload["update_available"] is True
    assert payload["can_apply"] is True
    assert payload["source"] == "jsdelivr"
    assert payload["current"] == VERSION


def test_check_failure_is_silent(monkeypatch):
    async def fake_get(url):
        raise OSError("blocked")

    monkeypatch.setattr(self_update, "_get", fake_get)
    async def failed_archive():
        raise OSError("blocked")
    monkeypatch.setattr(self_update, "_get_archive", failed_archive)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    payload = asyncio.run(self_update.check(force=True))
    assert payload["checked"] is False
    assert payload["latest"] is None
    assert payload["update_available"] is False
    assert payload["can_apply"] is False
    assert payload["message"] == ""


def test_fake_latest_skips_network(monkeypatch):
    async def fake_get(url):
        raise AssertionError(url)

    monkeypatch.setenv("CB_GATEWAY_FAKE_LATEST", "9.9.9")
    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    payload = asyncio.run(self_update.check(force=True))
    assert payload["source"] == "fake"
    assert payload["latest"] == "9.9.9"
    assert payload["update_available"] is True
    assert payload["can_apply"] is True


def test_fake_latest_apply_does_not_pull(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_FAKE_LATEST", "9.9.9")
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")

    def fake_run(args, timeout):
        raise AssertionError(args)

    monkeypatch.setattr(self_update, "_run", fake_run)
    result = self_update.apply()
    assert result["ok"] is True
    assert result["restart"] is False
    assert "测试模式" in result["message"]


def test_docker_install_cannot_apply(monkeypatch):
    async def fake_get(url):
        return 200, 'VERSION = "9.9.9"\n'

    monkeypatch.setattr(self_update, "_get", fake_get)
    monkeypatch.setattr(self_update, "install_kind", lambda: "docker")
    payload = asyncio.run(self_update.check(force=True))
    assert payload["update_available"] is True
    assert payload["can_apply"] is False
    assert payload["message"] == "Docker 部署请在宿主机拉取 ghcr.io/wicm84266964/buddy2api:v9.9.9 后重启"


def test_apply_pulls_installs_and_asks_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "requirements.txt").write_text("fastapi==0.141.1\n", encoding="utf-8")
    (tmp_path / "version.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")

    async def fake_check(*, force=False):
        return {
            "current": "2.1.13",
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    calls = []

    def fake_run(args, timeout):
        calls.append(list(args))
        class Result:
            returncode = 0
            stdout = "true\n" if "rev-parse" in args and "--is-inside-work-tree" in args else "main\n"
            stderr = ""
            if "status" in args:
                stdout = ""
            if "pull" in args:
                stdout = "Updating abc..def\n"
        return Result()

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    result = self_update.apply()
    assert result["ok"] is True
    assert result["restart"] is True
    assert result["latest"] == "9.9.9"
    assert ["git", "pull", "--ff-only"] in calls
    assert any(args[:3] == [__import__("sys").executable, "-m", "pip"] for args in calls)


def test_apply_refuses_dirty_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    async def fake_check(*, force=False):
        return {
            "current": "2.1.13",
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    def fake_run(args, timeout):
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""
        if args[:2] == ["git", "rev-parse"] and "--abbrev-ref" in args:
            Result.stdout = "main\n"
        if args[:2] == ["git", "status"]:
            Result.stdout = " M server.py\n"
        return Result()

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    result = self_update.apply()
    assert result["ok"] is False
    assert result["restart"] is False
    assert "未提交" in result["message"]


def test_apply_skips_untracked_files(monkeypatch, tmp_path):
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "requirements.txt").write_text("fastapi==0.141.1\n", encoding="utf-8")
    (tmp_path / "version.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")

    async def fake_check(*, force=False):
        return {
            "current": "2.1.13",
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    def fake_run(args, timeout):
        class Result:
            returncode = 0
            stdout = "true\n"
            stderr = ""
        if "--abbrev-ref" in args:
            Result.stdout = "main\n"
        elif "status" in args:
            Result.stdout = "?? docker-compose.override.yml\n"
        elif "pull" in args:
            Result.stdout = "Updating\n"
        return Result()

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    result = self_update.apply()
    assert result["ok"] is True


def test_restart_disabled_under_pytest():
    assert self_update.restart_allowed() is False


def test_admin_version_endpoint(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.setattr(server, "LOCAL_MODE", False)

    async def fake_check(*, force=False):
        return {
            "current": VERSION,
            "latest": "9.9.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    monkeypatch.setattr(self_update, "check", fake_check)
    payload = asyncio.run(server.admin_get_version())
    assert payload["latest"] == "9.9.9"
    assert payload["update_available"] is True


def test_admin_update_endpoint_skips_restart_under_pytest(monkeypatch):
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    monkeypatch.setattr(server, "LOCAL_MODE", False)
    monkeypatch.setattr(
        self_update,
        "apply",
        lambda: {"ok": True, "restart": True, "current": "2.1.13", "latest": "9.9.9", "message": "done"},
    )
    payload = asyncio.run(server.admin_apply_update())
    assert payload["ok"] is True
    assert payload["restart"] is True
    assert self_update.restart_allowed() is False


def test_web_has_update_controls():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "/admin/version" in html
    assert "/admin/update" in html
    assert "更新到 v" in html
    assert "check-cta" in html
    assert "检查更新" in html
    assert "暂时查不到远端版本" in html
    assert "测试模式" in html
