@echo off
rem ============================================================
rem  LiteGate one-click launcher:
rem  bootstrap deps -> frontend build check -> start gateway.
rem  Close this window (or Ctrl+C) to stop the service.
rem  NOTE: keep this file ASCII-only to avoid cmd codepage bugs;
rem        human-readable messages are printed by scripts/start.py
rem ============================================================
setlocal
set "PYTHONIOENCODING=utf-8"
title LiteGate - LLM proxy gateway
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :no_python

%PY% scripts\start.py %*
if errorlevel 1 goto :fail
endlocal
exit /b 0

:no_python
echo [ERROR] Python not found. Please install Python 3.9+ and enable "Add to PATH".
goto :fail

:fail
pause
exit /b 1
