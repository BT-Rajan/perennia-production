@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Perennia - Install

echo ============================================================
echo  PERENNIA - INSTALL
echo ============================================================
echo.
echo This will:
echo   1. Check for Python
echo   2. Create a virtual environment (.\venv)
echo   3. Install dependencies
echo   4. Generate the secrets Perennia needs to start
echo.
pause

echo.
echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python was not found on PATH.
    echo   Install Python 3.10+ from https://www.python.org/downloads/
    echo   During setup, check "Add Python to PATH", then run this installer again.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do echo   %%i found

echo.
echo [2/4] Creating virtual environment...
if exist venv (
    echo   venv already exists, skipping.
) else (
    python -m venv venv
    if errorlevel 1 (
        echo   Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo   Done.
)

echo.
echo [3/4] Installing dependencies (this can take a few minutes)...
venv\Scripts\python.exe -m pip install --upgrade pip >nul
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo   Failed to install dependencies. Check your internet connection and try again.
    echo   You can also try running as Administrator.
    echo.
    pause
    exit /b 1
)
echo   Done.

echo.
echo [4/4] Generating configuration and secrets...
venv\Scripts\python.exe scripts\setup_env.py
if errorlevel 1 (
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  INSTALL COMPLETE
echo ============================================================
echo.
echo Your admin login was saved to ADMIN_CREDENTIALS.txt in this folder.
echo Keep it safe, then feel free to delete the file.
echo.
echo Next: double-click start.bat to launch Perennia.
echo Once it's running, open http://localhost:8001/admin to log in
echo and add your Anthropic API key (it's encrypted automatically —
echo you never need to edit a file to set it).
echo.
pause
