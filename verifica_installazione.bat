@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente mancante: esegui prima installa.bat.
) else (
  ".venv\Scripts\python.exe" verifica_installazione.py
)
pause
popd
endlocal
