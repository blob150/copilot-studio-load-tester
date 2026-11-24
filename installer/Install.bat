@echo off
REM Double-click entry point for the Copilot Studio Load Tester installer.
REM Runs install.ps1 with a scoped execution-policy bypass (does not change
REM your system-wide PowerShell execution policy).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
