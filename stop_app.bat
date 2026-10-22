@echo off
REM Stops the Agent Load Tester server if it's running on port 5000.
powershell -NoProfile -Command ^
    "$c = Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue; " ^
    "if ($c) { Stop-Process -Id $c.OwningProcess -Force; Write-Host 'Stopped.' } else { Write-Host 'Not running.' }"
pause
