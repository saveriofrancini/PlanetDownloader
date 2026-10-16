@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Environment missing: run install.bat first.
) else (
  ".venv\Scripts\python.exe" verifica_installazione.py
)
pause
popd
endlocal
