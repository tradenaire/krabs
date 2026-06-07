@echo off
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
:loop
"%PY%" start.py
if errorlevel 1 (
    echo Bot crashed, restarting in 5s...
    timeout /t 5 /nobreak
    goto loop
)
