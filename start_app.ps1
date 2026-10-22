# Launches the Copilot Studio Agent Load Tester silently in the background
# (no console window) and opens your default browser to the dashboard.
# If the server is already running, it just opens the browser.
# Server stdout/stderr go to server.log / server.err in this folder.

$ErrorActionPreference = 'SilentlyContinue'
Set-Location -Path $PSScriptRoot

$existing = Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue
if (-not $existing) {
    Start-Process -FilePath "pythonw.exe" `
        -ArgumentList "app.py" `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput "$PSScriptRoot\server.log" `
        -RedirectStandardError "$PSScriptRoot\server.err"
    Start-Sleep -Seconds 3
}

Start-Process "http://127.0.0.1:5000"
