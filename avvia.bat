@echo off
setlocal
pushd "%~dp0"
title Planet Downloader
if not exist ".venv\Scripts\python.exe" (
  echo Esegui prima installa.bat, poi riapri avvia.bat.
  pause
  popd
  exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 (
  echo.
  echo Avvio non riuscito. Esegui verifica_installazione.bat per la diagnostica.
  pause
)
popd
endlocal
