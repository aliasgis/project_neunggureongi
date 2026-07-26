@echo off
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
  echo [ERROR] Python virtual environment was not found.
  echo Run install_and_run.bat first.
  pause
  exit /b 1
)
start "" "venv\Scripts\pythonw.exe" "reset_admin_gui.py"
endlocal
