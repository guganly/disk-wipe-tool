@echo off
title Install Python Dependencies

echo.
echo Installing dependencies: psutil + pywin32
echo.

set "PYTHON_EXE=C:\Users\yanwh\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "VENV_DIR=C:\Users\yanwh\.workbuddy\binaries\python\envs\disk_wipe"

if not exist "%VENV_DIR%" (
    echo Creating virtual environment...
    "%PYTHON_EXE%" -m venv "%VENV_DIR%"
)

echo Installing packages...
"%VENV_DIR%\Scripts\pip.exe" install psutil pywin32

echo.
echo Done! Now run: Launch_Wipe_Tool.bat
echo.
pause
