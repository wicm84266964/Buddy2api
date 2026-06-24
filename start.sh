#!/bin/bash
cd "$(dirname "$0")"

echo ""
echo "  ========================================"
echo "   Buddy 2 API"
echo "  ========================================"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "  [错误] 未找到 python3"
    exit 1
fi

# 检查依赖
python3 -c "import fastapi, uvicorn, httpx" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "  [安装] 首次运行，安装依赖..."
    pip3 install fastapi "uvicorn[standard]" httpx -q
fi

echo "  [启动] http://127.0.0.1:8787"
echo "  [停止] Ctrl+C"
echo ""

python3 server.py --port 8787 "$@"
