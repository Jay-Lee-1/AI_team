@echo off
chcp 65001 >nul
set "TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AI-Team-start.bat"
echo This will create ONE small file so the team starts when you sign in to Windows:
echo   %TARGET%
echo Its content will be:
echo   @call "%~dp0start-team.bat"
echo Nothing else is changed. Sleep/power settings are NOT changed (see README.md).
echo To undo, run autostart-remove.bat.
choice /M "Create it now"
if errorlevel 2 exit /b 0
> "%TARGET%" echo @call "%~dp0start-team.bat"
echo Created.
pause
