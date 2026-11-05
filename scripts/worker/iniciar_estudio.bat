@echo off
REM Estudio de render de la Oficina de Canales (arma los videos con la potencia de esta laptop).
REM Para que arranque solo al prender Windows: Win+R -> shell:startup -> pega ahí un acceso directo a este archivo.
cd /d "%~dp0"
title Estudio de render - Oficina de Canales
:otra
python oficina_worker.py
echo El estudio se detuvo; reintento en 30 segundos...
timeout /t 30 >nul
goto otra
