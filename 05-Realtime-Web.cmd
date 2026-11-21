@echo off
call "%~dp0Arbitrage.cmd" start realtime
if errorlevel 1 goto done
start "" "http://127.0.0.1:8787/"
:done
pause
