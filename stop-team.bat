@echo off
chcp 65001 >nul
call "%~dp0_find_python.bat" || exit /b 1
cd /d "%~dp0controller"
%PY% -m aiteam stop
timeout /t 3 /nobreak >nul
