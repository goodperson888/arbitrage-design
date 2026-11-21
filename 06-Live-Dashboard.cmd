@echo off
call "%~dp0Arbitrage.cmd" start live
if errorlevel 1 goto done
start "" "http://127.0.0.1:8766/"
:done
pause
