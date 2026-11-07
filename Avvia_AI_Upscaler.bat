@echo off
title Creality Real-ESRGAN AI Engine (RTX 4070)
cd /d "%~dp0"
echo ========================================================
echo   Avvio Servizio AI Super-Resolution (Real-ESRGAN)
echo   Dispositivo di calcolo: NVIDIA GeForce RTX 4070
echo ========================================================
echo.
.\.venv\Scripts\python.exe ai_upscaler_service.py
pause

