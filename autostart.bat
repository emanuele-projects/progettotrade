@echo off
REM Avvio del trading bot + dashboard al login (cartella Esecuzione automatica).
REM Nessun loop qui: runner.py e' un supervisor persistente che riavvia da solo
REM bot/dashboard su crash, si ferma su KILL_SWITCH hard, e ha un lock che impedisce
REM istanze doppie. Questo .bat lo lancia una volta sola.
title Trading Bot Supervisor
cd /d "%~dp0"
".venv\Scripts\python.exe" runner.py
