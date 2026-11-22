@echo off
setlocal
title CryptoTaxi Launcher
cd /d "%~dp0"

REM Force UTF-8 mode so Persian characters in logs/output don't crash on Windows
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo =============================================
echo   Crypto Taxi - claimer + dashboard
echo =============================================

REM 1) check python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python found is not installed.
    echo         Install from python.org and check "Add to PATH".
    pause
    exit /b 1
)

REM 2) check requests module - install if missing (FIXED: actual command, not echo)
python -c "import requests" >nul 2>nul
if errorlevel 1 (
    echo [INFO] requests module not found. Installing...
    python -m pip install requests
    if errorlevel 1 (
        echo [ERROR] Could not install requests automatically.
        echo         Run this command manually in a terminal:
        echo             python -m pip install requests
        pause
        exit /b 1
    )
)

REM 3) check accounts.json exists
if not exist "accounts.json" (
    echo [ERROR] accounts.json not found in this folder.
    echo         Make sure all files are in the same directory.
    pause
    exit /b 1
)

REM 4) kill any previous claimer / dashboard instances on port 8080
echo [INFO] Checking for old processes on port 8080...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8080 " ^| findstr "LISTENING"') do (
    echo [INFO] Stopping old dashboard ^(PID %%P^)...
    taskkill /PID %%P /F >nul 2>nul
)

REM 5) start claimer (minimized - the worker, runs by itself)
echo [INFO] Starting claimer...
start "CryptoTaxi-Claimer" /min python claimer.py --poll 60

REM 6) wait a moment, then check claimer is still running
timeout /t 3 >nul
tasklist /FI "WINDOWTITLE eq CryptoTaxi-Claimer*" | find /i "claimer.py" >nul
if errorlevel 1 (
    echo [WARN] Claimer window not found — check the minimized "CryptoTaxi-Claimer" window.
    echo        It may have crashed. Check claimer.log in this folder for details.
)

REM 7) start dashboard (the local website)
echo [INFO] Starting dashboard...
start "CryptoTaxi-Dashboard" python dashboard.py

REM 8) wait and open the website in the browser
timeout /t 3 >nul
start http://localhost:8080

echo.
echo =============================================
echo   Ready! Dashboard: http://localhost:8080
echo   Claimer and Dashboard are running in minimized windows.
echo   Leave the PC on — that is enough.
echo   Check claimer.log in this folder for any errors.
echo =============================================
echo.
pause
endlocal
