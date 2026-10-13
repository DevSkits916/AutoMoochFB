@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip --version >nul 2>&1
if errorlevel 1 (
    .venv\Scripts\python.exe -m ensurepip --upgrade
    if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m playwright install chromium
if errorlevel 1 goto failed
.venv\Scripts\python.exe facebook_group_scanner.py
if errorlevel 1 goto failed
exit /b
:failed
echo Setup or startup failed. See the error above.
pause
