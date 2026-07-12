@echo off
REM Avvio automatico del trading bot + dashboard, con riavvio su crash.
REM Lanciato al login dalla cartella Esecuzione automatica di Windows.
REM  - runner.py esce 0  -> arresto volontario (KILL_SWITCH hard) -> stop, niente riavvio
REM  - runner.py esce !=0 -> crash -> riavvio dopo 30 secondi
title Trading Bot Supervisor
cd /d "%~dp0"

:loop
".venv\Scripts\python.exe" runner.py
if "%errorlevel%"=="0" goto end
echo.
echo [autostart] runner terminato con errore (codice %errorlevel%). Riavvio tra 30s...  [Ctrl+C per annullare]
ping -n 31 127.0.0.1 >nul
goto loop

:end
echo.
echo [autostart] Arresto pulito (KILL_SWITCH hard). Puoi chiudere questa finestra.
echo Per riavviare: elimina il file KILL_SWITCH e rilancia autostart.bat (o rifai login).
