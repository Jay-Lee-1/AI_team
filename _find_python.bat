@rem Shared helper: finds Python 3 and sets PY. Called by the other .bat files.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [AI-Team] Python 3 was not found. Install it from https://www.python.org/downloads/windows/
  echo           and check "Add python.exe to PATH" during setup. See README.md.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
exit /b 0
