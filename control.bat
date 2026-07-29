@echo off
REM ════════════════════════════════════════════════════════════
REM  Perennia backend control script (Windows)
REM  Usage:  control.bat start | stop | restart | status
REM ════════════════════════════════════════════════════════════
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PIDFILE=%~dp0.perennia.pid"
set "LOGFILE=%~dp0perennia.log"
set "VENV=%~dp0.venv"
set "HOST=127.0.0.1"
set "PORT=8000"

if "%~1"=="" goto usage
if /i "%~1"=="start"   goto start
if /i "%~1"=="stop"    goto stop
if /i "%~1"=="restart" goto restart
if /i "%~1"=="status"  goto status
goto usage

:start
if exist "%PIDFILE%" (
    for /f %%p in ('type "%PIDFILE%"') do set "OLDPID=%%p"
    tasklist /fi "PID eq !OLDPID!" | findstr /i "!OLDPID!" >nul
    if not errorlevel 1 (
        echo Perennia is already running ^(PID !OLDPID!^).
        goto :eof
    ) else (
        del "%PIDFILE%" >nul 2>&1
    )
)

if not exist "%VENV%\Scripts\python.exe" (
    echo No virtual environment found at "%VENV%".
    echo Run:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)
if not exist "%~dp0.env" (
    echo No .env file found. Copy .env.example to .env and fill in real values first.
    exit /b 1
)

echo Starting Perennia on %HOST%:%PORT% ...
start "" /b "%VENV%\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT% > "%LOGFILE%" 2>&1

REM Give uvicorn a moment to spawn, then find its PID by listening port.
timeout /t 2 /nobreak >nul
set "FOUNDPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do set "FOUNDPID=%%a"
if defined FOUNDPID (
    echo !FOUNDPID! > "%PIDFILE%"
    echo Perennia started ^(PID !FOUNDPID!^). Logs: %LOGFILE%
) else (
    echo Could not confirm startup — check %LOGFILE% for errors.
)
goto :eof

:stop
if not exist "%PIDFILE%" (
    echo No PID file found — Perennia does not appear to be running via this script.
    goto :eof
)
for /f %%p in ('type "%PIDFILE%"') do set "OLDPID=%%p"
tasklist /fi "PID eq !OLDPID!" | findstr /i "!OLDPID!" >nul
if errorlevel 1 (
    echo Process !OLDPID! is not running. Cleaning up stale PID file.
    del "%PIDFILE%" >nul 2>&1
    goto :eof
)
echo Stopping Perennia ^(PID !OLDPID!^) ...
taskkill /pid !OLDPID! /t /f >nul 2>&1
del "%PIDFILE%" >nul 2>&1
echo Stopped.
goto :eof

:restart
call "%~f0" stop
timeout /t 1 /nobreak >nul
call "%~f0" start
goto :eof

:status
if not exist "%PIDFILE%" (
    echo Perennia is not running.
    goto :eof
)
for /f %%p in ('type "%PIDFILE%"') do set "OLDPID=%%p"
tasklist /fi "PID eq !OLDPID!" | findstr /i "!OLDPID!" >nul
if errorlevel 1 (
    echo Perennia is not running ^(stale PID file^).
) else (
    echo Perennia is running ^(PID !OLDPID!^) on %HOST%:%PORT%.
)
goto :eof

:usage
echo Usage: control.bat start ^| stop ^| restart ^| status
exit /b 1
