@echo off
echo stopped manually > "%~dp0\..\KILL_SWITCH"
echo KILL_SWITCH (soft) written. Trading e chiamate Claude in pausa;
echo il risk engine resta attivo a proteggere le posizioni aperte.
echo Per riprendere: cancella il file KILL_SWITCH.
echo Per lo spegnimento completo: scripts\stop-hard.bat
