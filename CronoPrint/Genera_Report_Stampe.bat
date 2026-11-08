@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Generatore Report Stampe 3D - SparkX i7
echo ============================================================
echo   Generazione Report Storico Stampe & Consumi SparkX i7
echo   Stampante IP: 192.168.78.50:7125 (Moonraker)
echo ============================================================
echo.
python "%~dp0generate_printer_report.py" --open
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERRORE: Impossibile generare il report. Verifica che la stampante sia accesa e connessa al Wi-Fi.
    pause
)
