#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

echo ""
echo "  ========================================"
echo "   Buddy 2 API Docker for WSL"
echo "  ========================================"
echo ""

if ! command -v docker >/dev/null 2>&1; then
    echo "  [错误] 未找到 Docker，请先启动 Docker Desktop，并确认 WSL 能访问 docker 命令。"
    exit 1
fi

auth_dir="${CB_HOST_AUTH_DIR:-}"

if [ -z "$auth_dir" ] && command -v cmd.exe >/dev/null 2>&1 && command -v wslpath >/dev/null 2>&1; then
    win_local="$(cmd.exe /c echo %LOCALAPPDATA% 2>/dev/null | tr -d '\r')"
    if [ -n "$win_local" ]; then
        auth_dir="$(wslpath -u "$win_local")/CodeBuddyExtension/Data/Public/auth"
    fi
fi

if [ -z "$auth_dir" ] || [ ! -d "$auth_dir" ]; then
    auth_dir="$(find /mnt/c/Users -path '*/AppData/Local/CodeBuddyExtension/Data/Public/auth' -type d 2>/dev/null | head -n 1 || true)"
fi

if [ -z "$auth_dir" ] || [ ! -d "$auth_dir" ]; then
    echo "  [提示] 未找到 Work Buddy auth 目录。"
    echo "  请先确认 Windows 里的 Work Buddy 已登录，或指定路径后重试："
    echo "  export CB_HOST_AUTH_DIR=/mnt/c/Users/你的用户名/AppData/Local/CodeBuddyExtension/Data/Public/auth"
    echo "  ./start-docker-wsl.sh"
    exit 1
fi

export CB_HOST_AUTH_DIR="$auth_dir"
export CB_GATEWAY_ADMIN_TOKEN="${CB_GATEWAY_ADMIN_TOKEN:-change-this-token}"

echo "  [auth] $auth_dir"
echo "  [挂载] $auth_dir -> /auth:ro"
echo "  [启动] http://127.0.0.1:8787"
echo ""

docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d --build

echo ""
echo "  已启动。打开 http://127.0.0.1:8787 后，账号页点“重新检测”或“一键导入本机登录”。"
