@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -c "import poster.recorder.app; import playwright" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -e ".[recorder]"
  if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -c "import os; from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); assert os.getenv('POSTER_CHROMIUM') or Path(p.chromium.executable_path).is_file(); p.stop()" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m playwright install chromium
  if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -m poster recorder
if errorlevel 1 goto error
exit /b 0
:error
echo Recorder did not start. See docs/windows_quickstart.md.
pause
exit /b 1
