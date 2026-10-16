@echo off
setlocal
pushd "%~dp0"
title Planet Downloader
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first, then reopen start.bat.
  pause
  popd
  exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 (
  echo.
  echo Startup failed. Run check_installation.bat for diagnostics.
  pause
)
popd
endlocal
