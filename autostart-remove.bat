@echo off
chcp 65001 >nul
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AI-Team-start.bat"
if exist "%TARGET%" (del "%TARGET%" && echo Removed: %TARGET%) else (echo Nothing to remove.)
pause
