@echo off
setlocal
cd /d "%~dp0"
title Perennia - Server

if not exist venv\Scripts\python.exe (
    echo.
    echo No virtual environment found. Run install.bat first.
    echo.
    pause
    exit /b 1
)

REM Safety net: generate .env if install.bat was skipped or interrupted.
venv\Scripts\python.exe scripts\setup_env.py
if errorlevel 1 (
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Starting Perennia — press Ctrl+C in this window to stop.
echo ============================================================
echo.

venv\Scripts\python.exe scripts\run_server.py

pause
