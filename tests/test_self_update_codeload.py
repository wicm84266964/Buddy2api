import asyncio
import io
import json
import subprocess
import zipfile

import pytest

import self_update


def archive(version="9.9.9", **extra):
    files = {"version.py": f'VERSION = "{version}"\n', "server.py": "new server\n",
             "self_update.py": "new updater\n", "requirements.txt": "", **extra}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for name, text in files.items():
            z.writestr(zipfile.ZipInfo("Buddy2api-main/" + name), text)
    return output.getvalue()


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "ROOT", tmp_path)
    monkeypatch.setattr(self_update, "_proxy_env_overlay", lambda _args: {})
    monkeypatch.setattr(self_update, "in_docker", lambda: False)
    monkeypatch.delenv("CB_GATEWAY_UPDATE_REPO", raising=False)
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    for name, text in {"version.py": 'VERSION = "1.0.0"\n', "server.py": "old server\n",
                       "self_update.py": "old updater\n", "requirements.txt": "",
                       "obsolete.py": "old\n", ".gitignore": ".env\n*.db\n"}.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "initial")
    git("remote", "add", "origin", "https://github.com/wicm84266964/Buddy2api.git")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", head)
    git("config", "branch.main.remote", "origin")
    git("config", "branch.main.merge", "refs/heads/main")
    async def download():
        return archive(**{".gitignore": ".env\n*.db\n"})
    monkeypatch.setattr(self_update, "_get_archive", download)
    return tmp_path, git, head


def test_version_check_uses_codeload_after_other_sources_fail(monkeypatch):
    urls = []
    async def get(url):
        urls.append(url)
        raise OSError("blocked")
    async def download():
        return archive()
    monkeypatch.setattr(self_update, "_get", get)
    monkeypatch.setattr(self_update, "_get_archive", download)
    assert asyncio.run(self_update._fetch_latest()) == ("9.9.9", "github-codeload")
    assert len(urls) == len(self_update.VERSION_SOURCES)


def test_archive_updates_deletes_and_preserves_runtime_files(repository):
    root, git, head = repository
    (root / ".env").write_text("private-setting")
    (root / "gateway.db").write_bytes(b"database")
    (root / "personal.txt").write_text("notes")
    self_update._apply_codeload("9.9.9")
    assert (root / "server.py").read_text() == "new server\n"
    assert not (root / "obsolete.py").exists()
    assert (root / ".env").read_text() == "private-setting"
    assert (root / "gateway.db").read_bytes() == b"database"
    assert (root / "personal.txt").read_text() == "notes"
    assert git("rev-parse", "HEAD") == head
    assert self_update._git_ready() is None
    state = self_update._validate_archive_state()
    with zipfile.ZipFile(state["backup"] + "/before.zip") as backup:
        assert backup.read("server.py").decode().splitlines() == ["old server"]


def test_repeated_archive_update_detects_user_edits(repository, monkeypatch):
    root, _, _ = repository
    self_update._apply_codeload("9.9.9")
    async def download():
        return archive("10.0.0", **{"new.py": "new", ".gitignore": ".env\n*.db\n"})
    monkeypatch.setattr(self_update, "_get_archive", download)
    self_update._apply_codeload("10.0.0")
    assert self_update._local_version() == "10.0.0"
    (root / "new.py").write_text("user edit")
    assert "本地修改" in self_update._git_ready()
    with pytest.raises(ValueError, match="本地修改"):
        self_update._apply_codeload("10.0.0")
    assert (root / "new.py").read_text() == "user edit"


@pytest.mark.parametrize("case", ["dirty", "untracked", "ignored", "local-commit", "remote", "stale"])
def test_archive_refuses_unsafe_updates(repository, monkeypatch, case):
    root, git, _ = repository
    if case == "dirty":
        (root / "server.py").write_text("user edit")
    elif case in {"untracked", "ignored"}:
        name = "new.py" if case == "untracked" else ".env"
        (root / name).write_text("user data")
        async def download():
            return archive(**{name: "upstream"})
        monkeypatch.setattr(self_update, "_get_archive", download)
    elif case == "local-commit":
        (root / "server.py").write_text("user commit")
        git("add", ".")
        git("commit", "-m", "local")
    elif case == "remote":
        git("remote", "set-url", "origin", "https://github.com/example/other.git")
    elif case == "stale":
        async def download():
            return archive("1.0.0")
        monkeypatch.setattr(self_update, "_get_archive", download)
    before = (root / "version.py").read_bytes()
    with pytest.raises(ValueError):
        self_update._apply_codeload("9.9.9")
    assert (root / "version.py").read_bytes() == before
    assert not self_update._archive_state_path().exists()


def test_failed_write_restores_original_files(repository, monkeypatch):
    root, _, _ = repository
    original = self_update._atomic_write
    failed = False
    def write(path, data):
        nonlocal failed
        if path.name == "version.py" and not failed:
            failed = True
            raise OSError("disk failure")
        original(path, data)
    monkeypatch.setattr(self_update, "_atomic_write", write)
    with pytest.raises(OSError, match="disk failure"):
        self_update._apply_codeload("9.9.9")
    assert (root / "server.py").read_text() == "old server\n"
    assert (root / "obsolete.py").read_text() == "old\n"
    assert self_update._local_version() == "1.0.0"
    assert not self_update._archive_state_path().exists()


@pytest.mark.parametrize("name", ["../escape.py", "folder/../../escape.py", "C:/escape.py", ".git/config", "folder\\escape.py"])
def test_archive_rejects_unsafe_paths(name):
    if "\\" in name:
        # Windows' ZIP writer normalizes separators; patch both name headers
        # to simulate an archive created on another platform.
        raw = archive(**{name.replace("\\", "/"): "bad"})
        raw = raw.replace(name.replace("\\", "/").encode(), name.encode())
    else:
        raw = archive(**{name: "bad"})
    with pytest.raises(ValueError):
        self_update._archive_files(raw)


@pytest.mark.parametrize("name", [".env", ".env.local", "data/gateway.db", "private.key"])
def test_archive_rejects_runtime_files(name):
    with pytest.raises(ValueError, match="本地配置"):
        self_update._archive_files(archive(**{name: "private"}))


def test_archive_rejects_symlink():
    raw = io.BytesIO(archive())
    with zipfile.ZipFile(raw, "a") as z:
        info = zipfile.ZipInfo("Buddy2api-main/link")
        info.external_attr = 0o120777 << 16
        z.writestr(info, "../outside")
    with pytest.raises(ValueError, match="不支持"):
        self_update._archive_files(raw.getvalue())


def test_apply_rejects_concurrent_update():
    with self_update._apply_lock:
        result = self_update.apply()
    assert result["ok"] is False
    assert "正在进行" in result["message"]


@pytest.mark.parametrize("failure", ["connect", "timeout", "conflict"])
def test_apply_uses_archive_only_for_network_errors(repository, monkeypatch, failure):
    root, _, _ = repository
    async def check(**_kwargs):
        return {"current": "1.0.0", "latest": "9.9.9", "update_available": True, "source": "jsdelivr"}
    monkeypatch.setattr(self_update, "check", check)
    original = self_update._run
    def run(args, timeout):
        if "pull" in args:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, timeout)
            message = "Failed to connect to github.com port 443" if failure == "connect" else "Not possible to fast-forward, aborting."
            return subprocess.CompletedProcess(args, 1, "", message)
        if "pip" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return original(args, timeout)
    monkeypatch.setattr(self_update, "_run", run)
    result = self_update.apply()
    assert result["ok"] == (failure != "conflict")
    if failure != "conflict":
        assert result["source"] == "github-codeload"
        assert result["restart"] is True
        # Subsequent updates skip git pull, which would see archive changes.
        self_update._validate_archive_state()
    else:
        assert not self_update._archive_state_path().exists()


def test_download_failure_leaves_tree_untouched(repository, monkeypatch):
    root, _, _ = repository
    async def download():
        raise OSError("codeload unreachable")
    monkeypatch.setattr(self_update, "_get_archive", download)
    with pytest.raises(OSError):
        self_update._apply_codeload("9.9.9")
    assert (root / "server.py").read_text() == "old server\n"
