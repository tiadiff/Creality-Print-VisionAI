@echo off
title Creality Vision AI - Server Manager Supervisor
cd /d "%~dp0"

set "PY_EXE=python"
if exist ".\.venv\Scripts\python.exe" set "PY_EXE=.\.venv\Scripts\python.exe"

echo ========================================================
echo   Avvio Server Manager Supervisor (Creality Vision AI)
echo   Porta Supervisore: :8089 ^| Server AI: :8088
echo ========================================================
echo.

"%PY_EXE%" server_manager.py
if %errorlevel% neq 0 (
    echo.
    echo Supervisore terminato con codice %errorlevel%.
    pause
)
