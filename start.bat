@echo off
:loop
python start.py
if errorlevel 1 (
    echo Bot crashed, restarting in 5s...
    timeout /t 5 /nobreak
    goto loop
)
