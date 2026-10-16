@echo off
setlocal
pushd "%~dp0"
title Planet Downloader - Installation
echo.
echo Planet Downloader - first-time installation for Windows
echo Requires 64-bit Python 3.12 and an Internet connection.
echo.
set "PD_PY=py -3.12"
%PD_PY% -c "import sys,struct;sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 else 1)" >nul 2>nul
if errorlevel 1 (
  set "PD_PY=python"
)
%PD_PY% -c "import sys,struct;sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 else 1)" >nul 2>nul
if errorlevel 1 goto no_python
if not exist ".venv\Scripts\python.exe" (
  %PD_PY% -m venv ".venv"
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" verifica_installazione.py
if errorlevel 1 goto failed
echo.
echo Installation complete. Now double-click start.bat.
goto end
:no_python
echo 64-bit Python 3.12 not found.
echo Install Python 3.12 for Windows from https://www.python.org/downloads/windows/
echo In the installer, enable Python Launcher and Add python.exe to PATH.
echo Then run install.bat again.
goto end
:failed
echo.
echo Installation incomplete. Read the error message above.
echo Check your Internet connection and 64-bit Python 3.12, then try again.
:end
echo.
pause
popd
endlocal
