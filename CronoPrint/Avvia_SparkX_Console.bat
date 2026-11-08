@echo off
chcp 65001 > nul
cd /d "%~dp0"

call "%~dp0Genera_Report_Stampe.bat"

start "" "%~dp0SparkXPrintConsole\bin\Release\net8.0-windows\SparkXPrintConsole.exe"
