@echo off
rem 
rem 
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\windows\install.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%SHARKYCLIPS_NO_PAUSE%"=="1" (
  echo.
  pause
)
exit /b %RC%
