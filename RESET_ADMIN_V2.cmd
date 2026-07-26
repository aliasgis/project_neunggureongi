@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0venv\Scripts\pythonw.exe" (
  echo [ERROR] Python virtual environment was not found.
  echo Run install_and_run.bat first.
  pause
  exit /b 1
)
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0reset_admin_gui.py"
endlocal
