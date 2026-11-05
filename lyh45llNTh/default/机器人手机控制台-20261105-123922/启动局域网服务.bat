@echo off
rem ============================================================
rem  Robot Console - LAN server launcher
rem  Double-click to start. A QR code will be printed for phone access.
rem  Close this window to stop the service.
rem
rem  NOTE: This file is intentionally ASCII-only. cmd.exe parses batch
rem  files using the OEM codepage (GBK on Chinese Windows), so UTF-8
rem  Chinese text inside a .bat corrupts line parsing. All user-facing
rem  Chinese output is produced by start_server.py instead.
rem ============================================================

setlocal EnableExtensions
cd /d "%~dp0"

rem Switch console to UTF-8 so Python can print Chinese correctly
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

title Robot Console - LAN Server

where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0start_server.py" %*
    goto :end
)

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0start_server.py" %*
    goto :end
)

echo.
echo [ERROR] Python not found. Please install Python 3.7 or newer,
echo         and check "Add Python to PATH" during installation.
echo         https://www.python.org/downloads/
echo.

:end
echo.
echo Service stopped. Press any key to close this window.
pause >nul
endlocal
