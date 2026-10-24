@echo off
rem Start the maimai_pre_touch PC server.
rem   run_server.bat                 normal start (auto-detects the game window)
rem   run_server.bat --touch log     dry run, only log touches, inject nothing
rem   run_server.bat --port 8770     override the HTTP port
setlocal
chcp 65001 >nul
cd /d "%~dp0"
python "%~dp0pc\server.py" %*
if errorlevel 1 pause
endlocal
