@echo off
chcp 65001 >nul 2>&1
title CodeBuddy Gateway

cd /d "%~dp0"

echo.
echo  ========================================
echo   CodeBuddy Gateway
echo  ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

REM 检查依赖
python -c "import fastapi, uvicorn, httpx" >nul 2>&1
if errorlevel 1 (
    echo  [安装] 首次运行，安装依赖...
    pip install fastapi "uvicorn[standard]" httpx -q
)

REM 启动
echo  [启动] http://127.0.0.1:8787
echo  [停止] Ctrl+C
echo.
python server.py --port 8787 %*

pause
