@echo off
setlocal
chcp 65001 >nul
title IT Admin Service Desk
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment is missing. See README.md for setup instructions.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m app.launcher
if errorlevel 1 (
    echo Service startup failed. Review the message above.
    pause
    exit /b 1
)
