@echo off
title GC Used Inventory Tracker
color 0A

echo.
echo  ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ
echo   GC Used Inventory Tracker
echo  ÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄÄ
echo.

:: Move to the folder this script lives in
cd /d "%~dp0"

:: ── Check Python ──────────────────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    py --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo  Python is not installed.
        echo.
        echo  Please install it from: https://www.python.org/downloads/
        echo  Make sure to check "Add Python to PATH" during installation.
        echo  Then double-click this file again.
        echo.
        pause
        exit /b 1
    )
    set PYTHON=py
) else (
    set PYTHON=python
)

for /f "tokens=2" %%i in ('%PYTHON% --version 2^>^&1') do set PYVER=%%i
echo  [OK] Python %PYVER% found

:: ── Install / upgrade dependencies ────────────────────────────────────────────
echo.
echo  Installing dependencies (this only takes a moment)...
%PYTHON% -m pip install --upgrade --quiet flask requests openpyxl
echo  [OK] Dependencies ready

:: ── Data folder — save in Documents\GCTracker so data persists between runs ──
set DATA_DIR=%USERPROFILE%\Documents\GCTracker
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
echo  [OK] Data folder: %DATA_DIR%

:: ── Launch ────────────────────────────────────────────────────────────────────
echo.
echo  Starting GC Tracker...
echo  Your browser will open automatically.
echo  To stop the tracker, close this window.
echo.

set DATA_DIR=%DATA_DIR%
set PORT=5050
%PYTHON% gc_tracker_app.py

pause
