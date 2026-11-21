@echo off
setlocal
chcp 65001 >nul
set "ARB_PYTHON=%~dp0runtime\python.exe"
if not exist "%ARB_PYTHON%" (
  echo Portable Python not found. Download and extract the full Windows ZIP from GitHub Actions.
  pause
  exit /b 1
)
"%ARB_PYTHON%" -X utf8 -u "%~dp0tools\windows\manager.py" %*
set "ARB_EXIT=%ERRORLEVEL%"
exit /b %ARB_EXIT%
