@echo off
setlocal
pushd "%~dp0"
title Planet Downloader - Installazione
echo.
echo Planet Downloader - installazione iniziale per Windows
echo Richiede Python 3.12 a 64 bit e una connessione Internet.
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
echo Installazione completata. Ora fai doppio clic su avvia.bat.
goto end
:no_python
echo Python 3.12 a 64 bit non trovato.
echo Installa Python 3.12 per Windows da https://www.python.org/downloads/windows/
echo Nell'installer abilita Python Launcher e Add python.exe to PATH.
echo Poi esegui nuovamente installa.bat.
goto end
:failed
echo.
echo Installazione non completata. Leggi il messaggio di errore qui sopra.
echo Controlla Internet e Python 3.12 a 64 bit, poi riprova.
:end
echo.
pause
popd
endlocal
