@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if defined POSTER_PYTHON (
  "%POSTER_PYTHON%" scripts\windows_bootstrap.py %*
  goto result
)
where py >nul 2>&1
if not errorlevel 1 (
  for %%V in (3.12 3.13 3.11) do (
    py -%%V -c "import sys; assert (3,11) <= sys.version_info[:2] <= (3,13)" >nul 2>&1
    if not errorlevel 1 (
      py -%%V scripts\windows_bootstrap.py %*
      goto result
    )
  )
)
python -c "import sys; assert (3,11) <= sys.version_info[:2] <= (3,13)" >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.11, 3.12 or 3.13 from https://www.python.org/downloads/windows/ with pip and Tcl/Tk.
  goto error
)
python scripts\windows_bootstrap.py %*
:result
if errorlevel 1 goto error
exit /b 0
:error
echo Recorder did not start. See WINDOWS_README.txt and docs\windows_quickstart.md.
if "%~1"=="" pause
exit /b 1
