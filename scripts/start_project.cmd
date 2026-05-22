@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_project.ps1" %*
exit /b %ERRORLEVEL%
