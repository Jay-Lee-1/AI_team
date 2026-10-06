@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_find_python.bat" || exit /b 1
if not exist "%~dp0logs" mkdir "%~dp0logs"
echo [AI-Team] Starting the controller in a separate window titled "AI-Team".
echo           Closing that window stops the team. Use stop-team.bat to stop safely.
start "AI-Team" /min cmd /k "cd /d "%~dp0controller" && %PY% -m aiteam run"
timeout /t 4 /nobreak >nul
cd /d "%~dp0controller"
%PY% -m aiteam open
