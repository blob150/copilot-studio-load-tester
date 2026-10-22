@echo off
REM Thin wrapper so you can double-click this instead of dealing with
REM PowerShell's execution-policy prompts. See start_app.ps1 for the
REM actual launch logic.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0start_app.ps1"
