@echo off
title Buddy 2 API

cd /d "%~dp0"

echo.
echo  ========================================
echo   Buddy 2 API
echo  ========================================
echo.

REM Prefer the fixed Conda environment. Activation is not required.
set "CONDA_EXE="
for /f "delims=" %%I in ('where conda 2^>nul') do if not defined CONDA_EXE set "CONDA_EXE=%%I"
for %%I in (
    "%USERPROFILE%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\anaconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\anaconda3\Scripts\conda.exe"
    "%ProgramData%\miniconda3\Scripts\conda.exe"
    "%ProgramData%\anaconda3\Scripts\conda.exe"
) do if not defined CONDA_EXE if exist "%%~I" set "CONDA_EXE=%%~I"

if defined CONDA_EXE goto use_conda
goto use_venv

:use_conda
echo  [Environment] Conda: buddy2api
call "%CONDA_EXE%" run -n buddy2api python --version >nul 2>&1
if errorlevel 1 (
    echo  [Setup] Creating Conda environment buddy2api ^(Python 3.12^)...
    call "%CONDA_EXE%" create -n buddy2api python=3.12 -y
    if errorlevel 1 goto conda_error
)
call "%CONDA_EXE%" run -n buddy2api python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo  [Update] Upgrading buddy2api to Python 3.12...
    call "%CONDA_EXE%" install -n buddy2api python=3.12 -y
    if errorlevel 1 goto conda_error
)
call "%CONDA_EXE%" run -n buddy2api python -c "import fastapi, uvicorn, httpx, cryptography" >nul 2>&1
if errorlevel 1 (
    echo  [Setup] Installing dependencies...
    call "%CONDA_EXE%" run -n buddy2api python -m pip install -r requirements.txt
    if errorlevel 1 goto dependency_error
)
echo  [Start] http://127.0.0.1:8787
echo  [Stop] Ctrl+C
echo.
call "%CONDA_EXE%" run --no-capture-output -n buddy2api python server.py --port 8787 %*
goto end

:use_venv
echo  [Environment] Conda not found; using project .venv
set "BASE_PYTHON=python"
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if errorlevel 1 goto python_error
    set "BASE_PYTHON=py -3"
)
if not exist ".venv\Scripts\python.exe" (
    echo  [Setup] Creating project virtual environment...
    %BASE_PYTHON% -m venv .venv
    if errorlevel 1 goto venv_error
)
.venv\Scripts\python.exe -c "import fastapi, uvicorn, httpx, cryptography" >nul 2>&1
if errorlevel 1 (
    echo  [Setup] Installing dependencies...
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto dependency_error
)
echo  [Start] http://127.0.0.1:8787
echo  [Stop] Ctrl+C
echo.
.venv\Scripts\python.exe server.py --port 8787 %*
goto end

:conda_error
echo  [Error] Could not create or update Conda environment buddy2api
goto failed
:python_error
echo  [Error] Python 3.10+ was not found. Install Miniconda or Python.
goto failed
:venv_error
echo  [Error] Could not create the project virtual environment
goto failed
:dependency_error
echo  [Error] Dependency installation failed
:failed
pause
exit /b 1

:end
if errorlevel 1 goto failed
exit /b 0
