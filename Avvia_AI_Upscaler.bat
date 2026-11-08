@echo off
title Creality Vision AI Engine & Controller
cd /d "%~dp0"

REM Avvia silenziosamente il controller locale (:8089) se disponibile
set "PY_CTRL=python"
if exist ".\.venv\Scripts\python.exe" set "PY_CTRL=.\.venv\Scripts\python.exe"
"%PY_CTRL%" server_manager.py --ensure-running >nul 2>nul

REM Verifica se il server AI (:8088) e gia attivo (es. avviato da Creality Print)
"%PY_CTRL%" -c "import urllib.request, sys; urllib.request.urlopen('http://127.0.0.1:8088/status', timeout=0.6); sys.exit(0)" >nul 2>nul
if %errorlevel%==0 (
    echo ========================================================
    echo   Server AI ^(:8088^) gia attivo in background!
    echo   Puoi controllarlo/fermarlo direttamente da Creality Print.
    echo ========================================================
    echo.
    pause
    exit /b 0
)

if exist ".\.venv\Scripts\python.exe" (
    echo ========================================================
    echo   Avvio Servizio AI Super-Resolution ^(Real-ESRGAN^)
    echo   Dispositivo di calcolo: NVIDIA GeForce RTX 4070
    echo ========================================================
    echo.
    .\.venv\Scripts\python.exe ai_upscaler_service.py
) else (
    echo ========================================================
    echo   Ambiente .venv non trovato: avvio Telemetria + Stringing AI ^(:8088^)
    echo   Controller Start/Stop da Creality Print attivo su :8089
    echo ========================================================
    echo.
    python printer_telemetry.py
)
pause
