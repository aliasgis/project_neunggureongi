@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_server.ps1"
set "server_exit_code=%ERRORLEVEL%"
echo.
if not "%server_exit_code%"=="0" (
    echo [ERROR] Server stopped with exit code %server_exit_code%.
    echo         Check the error message above.
) else (
    echo [INFO] The server is already running, or it was stopped normally.
)
echo Press any key to close this window.
pause >nul
exit /b %server_exit_code%
endlocal
