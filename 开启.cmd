@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\windows\launch.ps1" launch
set "ARB_EXIT=%ERRORLEVEL%"
if not defined ARB_NO_PAUSE pause
exit /b %ARB_EXIT%
