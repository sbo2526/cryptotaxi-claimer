@echo off
setlocal
cd /d "%~dp0"
echo Start only ONE supervisor. Configure secrets in the environment, never in this file.
echo External monitoring is OFF by default. Press Ctrl+C to stop.
python supervisor.py
endlocal
