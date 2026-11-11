"""Check GitHub/CDN for a newer Buddy2api and apply a local git update."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from tls_context import TLS_CONTEXT

from version import VERSION

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = "wicm84266964/Buddy2api"
GHCR_IMAGE = "ghcr.io/wicm84266964/buddy2api"
CACHE_OK_TTL = 600.0
CACHE_FAIL_TTL = 60.0
VERSION_RE = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.M)
VERSION_SOURCES = (
    ("jsdelivr", "https://cdn.jsdelivr.net/gh/{repo}@main/version.py", "file"),
    ("jsdelivr-fastly", "https://fastly.jsdelivr.net/gh/{repo}@main/version.py", "file"),
    ("github-raw", "https://raw.githubusercontent.com/{repo}/main/version.py", "file"),
    ("github-release", "https://api.github.com/repos/{repo}/releases/latest", "release"),
)
ARCHIVE_MAX_BYTES = 32 * 1024 * 1024


async def _get_archive() -> bytes:
    url = f"https://codeload.github.com/{repo()}/zip/refs/heads/main"
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=30.0, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": f"Buddy2api/{VERSION}"}) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > ARCHIVE_MAX_BYTES:
                    raise ValueError("codeload 源码包超过大小限制")
            return bytes(chunks)


def _archive_files(raw: bytes) -> dict[str, bytes]:
    files = {}
    case_names = set()
    roots = set()
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.orig_filename
                    or any(":" in part for part in path.parts)):
                raise ValueError("源码包包含不安全路径")
            if not path.parts:
                continue
            roots.add(path.parts[0])
            if member.is_dir():
                continue
            if len(path.parts) < 2 or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("源码包包含不支持的文件")
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if any(part.lower() == ".git" for part in path.parts[1:]):
                raise ValueError("源码包不能覆盖 Git 元数据")
            if _runtime_path(relative):
                raise ValueError("源码包不能包含本地配置或运行数据")
            if any(part.endswith((".", " ")) for part in path.parts):
                raise ValueError("源码包包含不支持的文件名")
            total += member.file_size
            if total > ARCHIVE_MAX_BYTES * 4 or relative.casefold() in case_names:
                raise ValueError("源码包大小或文件列表异常")
            case_names.add(relative.casefold())
            files[relative] = archive.read(member)
    if len(roots) != 1 or not parse_version_file(files.get("version.py", b"").decode("utf-8")):
        raise ValueError("源码包缺少有效 version.py")
    if not {"server.py", "self_update.py", "requirements.txt"}.issubset(files):
        raise ValueError("源码包缺少必要项目文件")
    return files


def _runtime_path(name: str) -> bool:
    path = PurePosixPath(name.lower())
    return (path.parts[0] in {"data", "logs", "secrets", "credentials", ".venv", "venv", "ant-code-uploads"}
            or path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example")
            or path.suffix in {".db", ".sqlite", ".sqlite3", ".key", ".pem", ".token", ".secret"}
            or path.name.endswith((".db-wal", ".db-shm")))


_cache: dict = {"at": 0.0, "ttl": 0.0, "payload": None}
_apply_lock = threading.Lock()


def repo() -> str:
    value = os.environ.get("CB_GATEWAY_UPDATE_REPO", DEFAULT_REPO).strip()
    return value or DEFAULT_REPO


def fake_latest() -> str | None:
    return _normalize_version(os.environ.get("CB_GATEWAY_FAKE_LATEST", ""))


def parse_version_file(text: str) -> str | None:
    match = VERSION_RE.search(text or "")
    if not match:
        return None
    return _normalize_version(match.group(1))


def parse_release_tag(text: str) -> str | None:
    import json

    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_version(str(data.get("tag_name") or data.get("name") or ""))


def _normalize_version(value: str) -> str | None:
    text = value.strip()
    if text.lower().startswith("v") and len(text) > 1 and text[1].isdigit():
        text = text[1:]
    if not text or not text[0].isdigit():
        return None
    return text


def version_key(value: str) -> tuple[int, ...]:
    parts = []
    normalized = _normalize_version(value) or "0"
    for piece in normalized.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts or (0,))


def is_newer(remote: str, local: str) -> bool:
    return version_key(remote) > version_key(local)


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["ttl"] = 0.0
    _cache["payload"] = None


def in_docker() -> bool:
    flag = os.environ.get("CB_DOCKER", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    return Path("/.dockerenv").exists()


def install_kind() -> str:
    if in_docker():
        return "docker"
    if (ROOT / ".git").is_dir():
        return "git"
    return "files"


def install_hint(kind: str, latest: str | None = None) -> str:
    if kind == "docker":
        tag = f"v{latest}" if latest else "对应版本"
        return f"Docker 部署请在宿主机拉取 {GHCR_IMAGE}:{tag} 后重启"
    if kind == "files":
        return "当前不是 git 克隆，请按 README 手动更新"
    return ""


def restart_allowed() -> bool:
    flag = os.environ.get("CB_GATEWAY_SKIP_RESTART", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


async def _get(url: str) -> tuple[int, str]:
    headers = {"User-Agent": f"Buddy2api/{VERSION}", "Accept": "application/json, text/plain"}
    async with httpx.AsyncClient(verify=TLS_CONTEXT, timeout=4.0, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        return response.status_code, response.text or ""


async def check(*, force: bool = False) -> dict:
    now = time.time()
    payload = _cache.get("payload")
    if not force and payload and now - float(_cache.get("at") or 0) < float(_cache.get("ttl") or 0):
        return payload
    payload = await _check_now()
    _cache["at"] = now
    _cache["ttl"] = CACHE_OK_TTL if payload.get("checked") else CACHE_FAIL_TTL
    _cache["payload"] = payload
    return payload


async def _check_now() -> dict:
    kind = install_kind()
    latest = None
    source = None
    forced = fake_latest()
    if forced:
        latest = forced
        source = "fake"
    else:
        latest, source = await _fetch_latest()
    current = VERSION
    checked = latest is not None
    update_available = bool(checked and is_newer(latest, current))
    return {
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "can_apply": kind == "git" and update_available,
        "install": kind,
        "checked": checked,
        "source": source,
        "message": install_hint(kind, latest) if update_available and kind != "git" else "",
    }


async def _fetch_latest() -> tuple[str | None, str | None]:
    for name, template, mode in VERSION_SOURCES:
        url = template.format(repo=repo())
        try:
            status, text = await _get(url)
        except Exception:
            continue
        if status != 200 or not text:
            continue
        parsed = parse_version_file(text) if mode == "file" else parse_release_tag(text)
        if not parsed:
            continue
        return parsed, name
    try:
        files = _archive_files(await _get_archive())
        return parse_version_file(files["version.py"].decode("utf-8")), "github-codeload"
    except Exception:
        return None, None


_PROXY_ENV_NAMES = ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY")
_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


def _spawn(args: list[str], timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    kwargs = {
        "args": args,
        "cwd": str(ROOT),
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if env is not None:
        kwargs["env"] = env
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(**kwargs)


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    overlay = _proxy_env_overlay(args)
    env = None
    if overlay:
        env = os.environ.copy()
        env.update(overlay)
    return _spawn(args, timeout, env)


def _command_uses_network(args: list[str]) -> bool:
    if not args:
        return False
    if args[0] == "git" and "pull" in args:
        return True
    return "-m" in args and "pip" in args


def _existing_proxy_env() -> str:
    for name in _PROXY_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _git_has_proxy_config() -> bool:
    probes = (
        ["git", "config", "--get", "http.proxy"],
        ["git", "config", "--get-regexp", r"^http\..+\.proxy$"],
    )
    for args in probes:
        try:
            result = _spawn(args, 8)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        if result.returncode == 0:
            return True
    return False


def _git_remote_url() -> str:
    try:
        result = _spawn(["git", "remote", "get-url", "origin"], 8)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _host_from_git_url(url: str) -> str | None:
    text = url.strip()
    if not text:
        return None
    if "://" in text:
        return urlparse(text).hostname
    if text.startswith("git@") and ":" in text:
        host = text.split("@", 1)[1].split(":", 1)[0]
        return host or None
    return None


def _command_host(args: list[str]) -> str:
    if args and args[0] == "git":
        return _host_from_git_url(_git_remote_url()) or "github.com"
    return "pypi.org"


def _normalize_proxy_url(value: object, default_scheme: str = "http") -> str | None:
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    scheme, rest = text.split("://", 1)
    scheme = scheme.lower()
    if scheme == "socks":
        scheme = "socks5"
    if scheme not in _PROXY_SCHEMES or not rest:
        return None
    text = f"{scheme}://{rest}"
    if not urlparse(text).hostname:
        return None
    return text


def _first_proxy_url(proxies: dict) -> str | None:
    for key, scheme in (
        ("https", "http"),
        ("all", "http"),
        ("http", "http"),
        ("socks5", "socks5"),
        ("socks", "socks5"),
    ):
        normalized = _normalize_proxy_url(proxies.get(key), scheme)
        if normalized:
            return normalized
    return None


def _host_bypasses_proxy(host: str, no_proxy: str) -> bool:
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return False
    for raw in re.split(r"[,;\s]+", no_proxy or ""):
        pattern = raw.strip().lower().rstrip(".")
        if not pattern:
            continue
        if pattern == "*":
            return True
        if pattern == "<local>":
            if "." not in name and ":" not in name:
                return True
            continue
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if name == suffix or name.endswith("." + suffix):
                return True
            continue
        if pattern.startswith("."):
            suffix = pattern[1:]
            if name == suffix or name.endswith("." + suffix):
                return True
            continue
        if "*" in pattern:
            if fnmatch.fnmatch(name, pattern):
                return True
            continue
        if name == pattern or name.endswith("." + pattern):
            return True
    return False


def _proxy_url_for_host(host: str, proxies: dict) -> str | None:
    if _host_bypasses_proxy(host, str(proxies.get("no") or "")):
        return None
    return _first_proxy_url(proxies)


def _curl_no_proxy(no_proxy: str) -> str:
    items = []
    for raw in re.split(r"[,;\s]+", no_proxy or ""):
        pattern = raw.strip()
        if not pattern or pattern.lower() == "<local>":
            continue
        if pattern == "*":
            return "*"
        if pattern.startswith("*."):
            items.append(pattern[1:])
            continue
        if "*" in pattern:
            continue
        items.append(pattern)
    return ",".join(items)


def _windows_proxy_override() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "ProxyOverride")
    except (OSError, ImportError, ValueError):
        return ""
    return str(value or "").strip()


def _system_proxies() -> dict:
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    if not isinstance(proxies, dict):
        proxies = {}
    else:
        proxies = dict(proxies)
    if not str(proxies.get("no") or "").strip():
        override = _windows_proxy_override()
        if override:
            proxies["no"] = override
    return proxies


def _proxy_for_command(args: list[str], proxies: dict) -> str | None:
    no = str(proxies.get("no") or "")
    if args and args[0] == "git":
        return _proxy_url_for_host(_command_host(args), proxies)
    hosts = ("pypi.org", "files.pythonhosted.org", "pypi.python.org")
    if all(_host_bypasses_proxy(host, no) for host in hosts):
        return None
    return _first_proxy_url(proxies)


def _proxy_env_overlay(args: list[str]) -> dict[str, str]:
    """Return proxy variables for this command, without writing Git config.

    An existing Git http.proxy or process proxy variable wins. Otherwise use
    the OS proxy address reported by urllib. Ports are never hardcoded.
    """
    if not _command_uses_network(args):
        return {}
    existing = _existing_proxy_env()
    if existing:
        if os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"):
            return {}
        return {"https_proxy": existing, "HTTPS_PROXY": existing}
    if args and args[0] == "git" and _git_has_proxy_config():
        return {}
    proxies = _system_proxies()
    proxy = _proxy_for_command(args, proxies)
    if not proxy:
        return {}
    overlay = {
        "http_proxy": proxy,
        "HTTP_PROXY": proxy,
        "https_proxy": proxy,
        "HTTPS_PROXY": proxy,
    }
    if proxy.split("://", 1)[0].startswith("socks"):
        overlay["all_proxy"] = proxy
        overlay["ALL_PROXY"] = proxy
    no_proxy = _curl_no_proxy(str(proxies.get("no") or ""))
    if no_proxy and not os.environ.get("no_proxy") and not os.environ.get("NO_PROXY"):
        overlay["no_proxy"] = no_proxy
        overlay["NO_PROXY"] = no_proxy
    return overlay


def _looks_like_connect_failure(text: str) -> bool:
    lowered = text.lower()
    if "github.com" not in lowered and "443" not in lowered:
        return False
    markers = (
        "failed to connect",
        "could not connect",
        "couldn't connect",
        "unable to access",
        "connection refused",
        "connection timed out",
        "timed out",
        "could not resolve",
        "connection reset",
        "recv failure",
    )
    return any(marker in lowered for marker in markers)


def _pull_failure_message(detail: str) -> str:
    text = detail.strip() or "git pull 失败"
    if not _looks_like_connect_failure(text):
        return text[:400]
    used_proxy = bool(_proxy_env_overlay(["git", "pull", "--ff-only"]))
    if used_proxy:
        hint = "已沿用当前环境或系统代理，仍连不上 GitHub。"
    else:
        hint = "未发现环境变量或系统代理地址。只配置 PAC 时，请给 Git 设置 http.proxy，或在启动前设置 HTTPS_PROXY。"
    if hint in text:
        return text[:400]
    return f"{text[:220].rstrip()} {hint}"[:400]


def _git_ready() -> str | None:
    if install_kind() != "git":
        return install_hint(install_kind()) or "当前安装不能一键更新"
    try:
        inside = _run(["git", "rev-parse", "--is-inside-work-tree"], 8)
    except FileNotFoundError:
        return "未找到 git，无法一键更新"
    except subprocess.TimeoutExpired:
        return "git 响应超时"
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "当前目录不是 git 仓库"
    try:
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], 8)
        status = _run(["git", "status", "--porcelain"], 8)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "无法读取 git 状态"
    name = branch.stdout.strip()
    if branch.returncode != 0 or name in {"", "HEAD"}:
        return "当前是游离 HEAD，请手动更新"
    if name not in {"main", "master"}:
        return f"当前分支是 {name}，一键更新只支持 main"
    dirty = [
        line
        for line in status.stdout.splitlines()
        if line.strip() and not line.startswith("??") and not line.startswith("!!")
    ]
    if _archive_state_path().exists():
        try:
            _validate_archive_state()
        except Exception as exc:
            return str(exc)[:400]
    elif dirty:
        return "工作区有未提交修改，请先手动处理"
    return None


def _archive_state_path() -> Path:
    return ROOT / ".git" / "buddy2api-codeload.json"


def _git_output(args: list[str]) -> str:
    result = _run(["git", *args], 15)
    if result.returncode:
        raise ValueError("无法验证 Git 状态：" + (result.stderr or result.stdout)[:200])
    return result.stdout.strip()


def _file_hash(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError(f"不能更新符号链接：{path.name}")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _validate_archive_state() -> dict:
    state = json.loads(_archive_state_path().read_text(encoding="utf-8"))
    if state["head"] != _git_output(["rev-parse", "HEAD"]):
        raise ValueError("源码包更新后 Git HEAD 已变化，请先手动同步仓库")
    for name, expected in state["files"].items():
        target = _update_target(name)
        if _file_hash(target) != expected:
            raise ValueError(f"工作区有本地修改，请先手动处理：{name}")
    changed = _git_output(["diff", "--name-only", "-z", "HEAD"]).split("\0")
    if any(name and name not in state["files"] for name in changed):
        raise ValueError("工作区有未提交修改，请先手动处理")
    if _git_output(["diff", "--cached", "--name-only"]):
        raise ValueError("暂存区有本地修改，请先手动处理")
    return state


def _update_target(name: str) -> Path:
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or "\\" in name or not path.parts
            or any(":" in part or part.lower() == ".git" for part in path.parts)):
        raise ValueError("更新路径无效")
    target = ROOT.joinpath(*path.parts)
    if not target.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("更新路径超出项目目录")
    parent = target
    while parent != ROOT:
        if parent.is_symlink():
            raise ValueError(f"更新路径包含符号链接：{name}")
        parent = parent.parent
    return target


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".buddy-update-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _apply_codeload(latest: str) -> None:
    # Archive updates leave Git history intact. A manifest distinguishes these
    # known changes from user edits, so the next update can use the same route.
    remote = _git_remote_url().rstrip("/")
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+)", remote)
    remote_repo = match.group(1).removesuffix(".git") if match else ""
    if remote_repo.lower() != repo().lower():
        raise ValueError("Git remote 与更新仓库不一致，不能使用 codeload 回退")
    files = _archive_files(asyncio.run(_get_archive()))
    version = parse_version_file(files["version.py"].decode("utf-8"))
    if version != latest and not is_newer(version, latest):
        raise ValueError("codeload 源码包版本落后于已检测版本")
    blocked = _git_ready()
    if blocked:
        raise ValueError(blocked)
    state_path = _archive_state_path()
    previous = _validate_archive_state() if state_path.exists() else None
    head = _git_output(["rev-parse", "HEAD"])
    if not previous and _git_output(["rev-list", "--count", "@{upstream}..HEAD"]) != "0":
        raise ValueError("当前分支有本地提交，不能使用源码包覆盖")
    tracked = set(_git_output(["ls-files", "-z"]).split("\0")) - {""}
    managed = {name for name in tracked | set(previous["files"] if previous else ()) if not _runtime_path(name)}
    names = managed | set(files)
    originals = {}
    for name in names:
        path = _update_target(name)
        # Never replace a user's untracked/ignored file with a new upstream file.
        if name not in managed and path.exists():
            raise ValueError(f"源码包与本地文件冲突：{name}")
        if path.exists() and not path.is_file():
            raise ValueError(f"源码包与本地目录冲突：{name}")
        originals[name] = path.read_bytes() if path.exists() else None
    old_state = state_path.read_bytes() if previous else None
    # Durable backup survives a process crash; runtime databases and secrets
    # outside the source file list are never included or changed.
    backup_dir = Path(tempfile.mkdtemp(prefix="buddy2api-update-", dir=ROOT / ".git"))
    with zipfile.ZipFile(backup_dir / "before.zip", "w", zipfile.ZIP_DEFLATED) as backup:
        for name, data in originals.items():
            if data is not None:
                backup.writestr(name, data)
    (backup_dir / "manifest.json").write_text(json.dumps({
        "head": head, "absent": [name for name, data in originals.items() if data is None],
        "previous_state": previous,
    }), encoding="utf-8")
    touched = []
    try:
        for name in sorted(names):
            path = _update_target(name)
            touched.append(name)
            if name in files:
                _atomic_write(path, files[name])
            elif path.exists():
                path.unlink()
        state = {"head": head, "version": version, "backup": str(backup_dir),
                 "files": {name: hashlib.sha256(files[name]).hexdigest() if name in files else None
                           for name in names}}
        _atomic_write(state_path, json.dumps(state, ensure_ascii=False).encode("utf-8"))
    except Exception:
        for name in reversed(touched):
            path = _update_target(name)
            if originals[name] is not None:
                _atomic_write(path, originals[name])
            elif path.exists():
                path.unlink()
        if old_state is not None:
            _atomic_write(state_path, old_state)
        elif state_path.exists():
            state_path.unlink()
        raise


def _local_version() -> str:
    try:
        text = (ROOT / "version.py").read_text(encoding="utf-8")
    except OSError:
        return VERSION
    return parse_version_file(text) or VERSION


def apply() -> dict:
    if not _apply_lock.acquire(blocking=False):
        return {"ok": False, "restart": False, "message": "更新正在进行中，请稍后再试"}
    try:
        return _apply()
    finally:
        _apply_lock.release()


def _apply() -> dict:
    snapshot = asyncio.run(check(force=True))
    if not snapshot.get("update_available"):
        if not snapshot.get("checked"):
            return {
                "ok": False,
                "restart": False,
                "current": snapshot.get("current") or VERSION,
                "latest": snapshot.get("latest"),
                "message": "暂时查不到远端版本",
            }
        return {
            "ok": False,
            "restart": False,
            "current": snapshot.get("current") or VERSION,
            "latest": snapshot.get("latest"),
            "message": f"已是最新版本 v{snapshot.get('current') or VERSION}",
        }
    if snapshot.get("source") == "fake":
        return {
            "ok": True,
            "restart": False,
            "current": snapshot.get("current") or VERSION,
            "latest": snapshot.get("latest"),
            "message": "测试模式：不会拉取代码，也不会重启",
        }
    blocked = _git_ready()
    if blocked:
        return {
            "ok": False,
            "restart": False,
            "current": snapshot.get("current") or VERSION,
            "latest": snapshot.get("latest"),
            "message": blocked,
        }
    archive_update = _archive_state_path().exists()
    detail = ""
    if not archive_update:
        try:
            pull = _run(["git", "pull", "--ff-only"], 90)
        except FileNotFoundError:
            return {"ok": False, "restart": False, "message": "未找到 git，无法一键更新"}
        except subprocess.TimeoutExpired:
            detail = "git pull 超时"
            archive_update = True
        else:
            if pull.returncode != 0:
                detail = (pull.stderr or pull.stdout or "git pull 失败").strip()
                if not _looks_like_connect_failure(detail):
                    return {"ok": False, "restart": False, "message": detail[:400]}
                archive_update = True
    if archive_update:
        try:
            _apply_codeload(snapshot["latest"])
        except Exception as exc:
            prefix = _pull_failure_message(detail) + " " if detail else ""
            return {"ok": False, "restart": False,
                    "message": f"{prefix}codeload 回退失败：{exc}"[:700]}
    new_version = _local_version()
    if not is_newer(new_version, snapshot["current"]) and new_version == snapshot["current"]:
        return {
            "ok": False,
            "restart": False,
            "current": new_version,
            "latest": snapshot.get("latest"),
            "message": "已拉取，但本地版本没有变化。请确认 git remote 指向本仓库 main",
        }
    requirements = ROOT / "requirements.txt"
    if requirements.is_file():
        try:
            pip = _run([sys.executable, "-m", "pip", "install", "-r", str(requirements)], 180)
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "restart": False,
                "current": new_version,
                "latest": snapshot.get("latest"),
                "message": "代码已更新，但安装依赖超时。请手动执行 pip install -r requirements.txt 后重启",
            }
        if pip.returncode != 0:
            detail = (pip.stderr or pip.stdout or "pip 失败").strip()
            return {
                "ok": False,
                "restart": False,
                "current": new_version,
                "latest": snapshot.get("latest"),
                "message": f"代码已更新，但安装依赖失败：{detail[:300]}",
            }
    reset_cache()
    return {
        "ok": True,
        "restart": True,
        "current": snapshot["current"],
        "latest": new_version,
        "source": "github-codeload" if archive_update else "git",
        "message": f"已{'通过 codeload ' if archive_update else ''}更新到 v{new_version}，正在重启",
    }


def restart_now() -> None:
    if not restart_allowed():
        return
    python = sys.executable
    argv = [python, *sys.argv]
    if os.name == "nt":
        _restart_windows(python, argv)
        return
    os.execv(python, argv)


def schedule_restart(delay: float = 0.4) -> None:
    if not restart_allowed():
        return
    threading.Thread(target=_restart_after, args=(delay,), daemon=True).start()


def _restart_after(delay: float) -> None:
    time.sleep(max(0.0, delay))
    restart_now()


def _restart_windows(python: str, argv: list[str]) -> None:
    parent = os.getpid()
    cwd = os.getcwd()
    waiter = (
        "import os, time, subprocess\n"
        f"p={parent}\n"
        f"a={argv!r}\n"
        f"c={cwd!r}\n"
        "for _ in range(75):\n"
        "    try:\n"
        "        os.kill(p, 0)\n"
        "    except OSError:\n"
        "        break\n"
        "    time.sleep(0.2)\n"
        "time.sleep(0.4)\n"
        "flags=getattr(subprocess,'DETACHED_PROCESS',8)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)\n"
        "subprocess.Popen(a, cwd=c, close_fds=True, creationflags=flags)\n"
    )
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    subprocess.Popen([python, "-c", waiter], close_fds=True, creationflags=flags)
    os._exit(0)
