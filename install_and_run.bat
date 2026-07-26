@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul || (echo Python 3.11+ required & pause & exit /b 1)
if not exist venv python -m venv venv
"venv\Scripts\python.exe" -m pip install --upgrade pip
"venv\Scripts\python.exe" -m pip install -r requirements.txt
"venv\Scripts\python.exe" make_samples.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_server.ps1"
endlocal
