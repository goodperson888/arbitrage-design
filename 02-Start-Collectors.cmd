@echo off
call "%~dp0Arbitrage.cmd" start all
if errorlevel 1 goto done
start "" "http://127.0.0.1:8765/"
:done
pause
