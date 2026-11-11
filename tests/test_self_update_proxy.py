import os

import self_update


PROXY_ENV_NAMES = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def _clear_proxy_env(monkeypatch):
    for name in PROXY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_proxy_url_uses_detected_address_and_bypass_rules():
    proxies = {"https": "http://10.1.2.3:4242", "no": "localhost;127.*;<local>"}
    assert self_update._proxy_url_for_host("github.com", proxies) == "http://10.1.2.3:4242"
    assert self_update._proxy_url_for_host("github.com", {"https": "http://10.0.0.1:1", "no": "github.com"}) is None
    assert self_update._proxy_url_for_host("github.com", {"http": "10.8.8.8:8080"}) == "http://10.8.8.8:8080"
    assert self_update._proxy_url_for_host("github.com", {"socks": "127.0.0.1:1080"}) == "socks5://127.0.0.1:1080"
    assert self_update._proxy_url_for_host("github.com", {"all": "socks5h://127.0.0.1:1080"}) == "socks5h://127.0.0.1:1080"
    assert self_update._curl_no_proxy("localhost;127.*;<local>;*.example.com") == "localhost,.example.com"
    assert self_update._host_from_git_url("git@github.com:wicm84266964/Buddy2api.git") == "github.com"
    assert self_update._host_from_git_url("https://github.com/wicm84266964/Buddy2api.git") == "github.com"
    assert self_update._host_from_git_url("ssh://git@github.com/wicm84266964/Buddy2api.git") == "github.com"


def test_overlay_uses_system_proxy_without_hardcoded_port(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setattr(self_update, "_git_has_proxy_config", lambda: False)
    monkeypatch.setattr(self_update, "_command_host", lambda args: "github.com")
    monkeypatch.setattr(
        self_update,
        "_system_proxies",
        lambda: {"https": "http://10.1.2.3:4242", "no": "localhost;127.*;<local>"},
    )

    overlay = self_update._proxy_env_overlay(["git", "pull", "--ff-only"])

    assert overlay["https_proxy"] == "http://10.1.2.3:4242"
    assert overlay["HTTPS_PROXY"] == "http://10.1.2.3:4242"
    assert "7897" not in overlay["https_proxy"]
    assert "<local>" not in overlay["NO_PROXY"]
    assert self_update._proxy_env_overlay(["git", "status", "--porcelain"]) == {}


def test_overlay_does_not_override_git_config_or_existing_env(monkeypatch):
    _clear_proxy_env(monkeypatch)
    monkeypatch.setattr(self_update, "_git_has_proxy_config", lambda: True)
    monkeypatch.setattr(self_update, "_system_proxies", lambda: {"https": "http://10.1.2.3:4242"})
    assert self_update._proxy_env_overlay(["git", "pull", "--ff-only"]) == {}

    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:1")
    monkeypatch.setattr(self_update, "_git_has_proxy_config", lambda: False)
    assert self_update._proxy_env_overlay(["git", "pull", "--ff-only"]) == {}

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("http_proxy", "http://10.8.8.8:8080")
    overlay = self_update._proxy_env_overlay(["git", "pull", "--ff-only"])
    assert overlay == {"https_proxy": "http://10.8.8.8:8080", "HTTPS_PROXY": "http://10.8.8.8:8080"}


def test_run_injects_proxy_without_dropping_environment(monkeypatch):
    seen = {}

    def fake_spawn(args, timeout, env=None):
        seen["args"] = args
        seen["env"] = env

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr(self_update, "_spawn", fake_spawn)
    monkeypatch.setattr(self_update, "_proxy_env_overlay", lambda args: {"https_proxy": "http://10.1.2.3:4242"})

    self_update._run(["git", "pull", "--ff-only"], 5)

    assert seen["args"] == ["git", "pull", "--ff-only"]
    assert seen["env"]["https_proxy"] == "http://10.1.2.3:4242"
    assert seen["env"]["PATH"] == os.environ["PATH"]


def _ready_git(args):
    class Result:
        returncode = 0
        stdout = "true\n"
        stderr = ""

    if "--abbrev-ref" in args:
        Result.stdout = "main\n"
    elif "status" in args:
        Result.stdout = ""
    return Result()


def test_pull_connect_failure_explains_missing_proxy(monkeypatch, tmp_path):
    def failed_archive(_latest):
        raise OSError("blocked")
    monkeypatch.setattr(self_update, "_apply_codeload", failed_archive)
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    async def fake_check(*, force=False):
        return {
            "current": "2.2.8",
            "latest": "2.2.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    def fake_run(args, timeout):
        result = _ready_git(args)
        if "pull" in args:
            result.returncode = 1
            result.stderr = "fatal: unable to access 'https://github.com/example/repo.git': Failed to connect to github.com port 443"
        return result

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    monkeypatch.setattr(self_update, "_proxy_env_overlay", lambda args: {})

    result = self_update.apply()

    assert result["ok"] is False
    assert "HTTPS_PROXY" in result["message"]
    assert "PAC" in result["message"]


def test_pull_connect_failure_says_when_proxy_was_used(monkeypatch, tmp_path):
    def failed_archive(_latest):
        raise OSError("blocked")
    monkeypatch.setattr(self_update, "_apply_codeload", failed_archive)
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    async def fake_check(*, force=False):
        return {
            "current": "2.2.8",
            "latest": "2.2.9",
            "update_available": True,
            "can_apply": True,
            "install": "git",
            "checked": True,
            "source": "jsdelivr",
            "message": "",
        }

    def fake_run(args, timeout):
        result = _ready_git(args)
        if "pull" in args:
            result.returncode = 1
            result.stderr = "fatal: unable to access 'https://github.com/example/repo.git': Failed to connect to github.com port 443"
        return result

    monkeypatch.setattr(self_update, "check", fake_check)
    monkeypatch.setattr(self_update, "_run", fake_run)
    monkeypatch.setattr(self_update, "install_kind", lambda: "git")
    monkeypatch.setattr(self_update, "_proxy_env_overlay", lambda args: {"https_proxy": "http://10.1.2.3:4242"})

    result = self_update.apply()

    assert result["ok"] is False
    assert "已沿用当前环境或系统代理" in result["message"]
    assert "10.1.2.3" not in result["message"]

def test_system_proxies_include_windows_bypass_list(monkeypatch):
    monkeypatch.setattr(self_update.urllib.request, "getproxies", lambda: {"https": "http://10.1.2.3:4242"})
    monkeypatch.setattr(self_update, "_windows_proxy_override", lambda: "localhost;127.*;<local>")

    proxies = self_update._system_proxies()

    assert proxies["https"] == "http://10.1.2.3:4242"
    assert proxies["no"] == "localhost;127.*;<local>"
    assert self_update._proxy_url_for_host("github.com", proxies) == "http://10.1.2.3:4242"
