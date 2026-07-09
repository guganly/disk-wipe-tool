@echo off
chcp 65001 >nul 2>&1
title Disk Secure Wipe Tool v1.15

echo.
echo  ==========================================
echo   Disk Secure Wipe Tool  v1.15
echo   Starting, please wait...
echo  ==========================================
echo.

set "PYTHON_EXE=C:\Users\yanwh\.workbuddy\binaries\python\envs\disk_wipe\Scripts\python.exe"
set "SCRIPT=%~dp0disk_wipe.py"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python env not found.
    echo Please run install_env.bat first.
    pause
    exit /b 1
)

if not exist "%SCRIPT%" (
    echo [ERROR] disk_wipe.py not found in: %~dp0
    pause
    exit /b 1
)

echo Launching with Python: %PYTHON_EXE%
echo Script: %SCRIPT%
echo.

"%PYTHON_EXE%" "%SCRIPT%"

if errorlevel 1 (
    echo.
    echo [HINT] If UAC is needed, right-click this file and choose "Run as administrator".
    pause
)
