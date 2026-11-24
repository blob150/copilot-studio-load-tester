<#
.SYNOPSIS
    Installs the Copilot Studio Load Tester for the current user.

.DESCRIPTION
    No admin rights required, no external installer tooling (Inno Setup/WiX)
    required. This script:
      1. Copies the packaged app (this script's sibling "app" folder) to a
         stable per-user location: %LOCALAPPDATA%\Programs\CopilotStudioLoadTester
      2. Creates a Start Menu shortcut and (optionally) a Desktop shortcut.
      3. Launches the app, which opens your browser and walks you through the
         one-time Setup Wizard (Entra app registration + sign-in).

    Safe to re-run: it simply overwrites the previous install (your saved
    data, sign-in, and run history live separately in %LOCALAPPDATA%\
    CopilotStudioLoadTester and are untouched by reinstalling the app itself).
#>
[CmdletBinding()]
param(
    [switch]$NoDesktopShortcut,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"

$AppName    = "Copilot Studio Load Tester"
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceDir  = Join-Path $ScriptDir "app"
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\CopilotStudioLoadTester"
$ExeName    = "CopilotStudioLoadTester.exe"

Write-Host "== $AppName installer ==" -ForegroundColor Cyan

if (-not (Test-Path $SourceDir)) {
    Write-Error "Could not find the packaged app folder next to this script (expected: $SourceDir). Re-download the installer package and try again."
    exit 1
}
if (-not (Test-Path (Join-Path $SourceDir $ExeName))) {
    Write-Error "Packaged app folder is missing $ExeName. The installer package may be corrupt or incomplete."
    exit 1
}

# --- 1. Stop any running instance so we can overwrite it cleanly ------------
$running = Get-Process -Name "CopilotStudioLoadTester" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Closing the currently running app..." -ForegroundColor Yellow
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

# --- 2. Copy app files to the stable install location -----------------------
Write-Host "Installing to $InstallDir ..."
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-Item -Path (Join-Path $SourceDir "*") -Destination $InstallDir -Recurse -Force

$ExePath = Join-Path $InstallDir $ExeName

# --- 3. Create shortcuts ------------------------------------------------------
function New-Shortcut {
    param([string]$ShortcutPath, [string]$TargetPath)
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = Split-Path -Parent $TargetPath
    $shortcut.IconLocation = $TargetPath
    $shortcut.Description = $AppName
    $shortcut.Save()
}

$StartMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$StartMenuShortcut = Join-Path $StartMenuDir "$AppName.lnk"
New-Shortcut -ShortcutPath $StartMenuShortcut -TargetPath $ExePath
Write-Host "Created Start Menu shortcut." -ForegroundColor Green

if (-not $NoDesktopShortcut) {
    $DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk"
    New-Shortcut -ShortcutPath $DesktopShortcut -TargetPath $ExePath
    Write-Host "Created Desktop shortcut." -ForegroundColor Green
}

# --- 4. Record an uninstall helper -------------------------------------------
$DesktopShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.lnk"
$UninstallScript = @"
# Uninstalls $AppName (app files + shortcuts only; your sign-in/profiles/run
# history in %LOCALAPPDATA%\CopilotStudioLoadTester are left in place unless
# you pass -RemoveData).
param([switch]`$RemoveData)
Get-Process -Name "CopilotStudioLoadTester" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$InstallDir" -ErrorAction SilentlyContinue
Remove-Item -Force "$StartMenuShortcut" -ErrorAction SilentlyContinue
Remove-Item -Force "$DesktopShortcutPath" -ErrorAction SilentlyContinue
if (`$RemoveData) {
    Remove-Item -Recurse -Force "$(Join-Path $env:LOCALAPPDATA 'CopilotStudioLoadTester')" -ErrorAction SilentlyContinue
}
Write-Host "$AppName uninstalled." -ForegroundColor Green
"@
Set-Content -Path (Join-Path $InstallDir "uninstall.ps1") -Value $UninstallScript -Encoding UTF8

Write-Host ""
Write-Host "Install complete!" -ForegroundColor Cyan
Write-Host "  App folder: $InstallDir"
Write-Host "  To uninstall later: powershell -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`""
Write-Host ""

# --- 5. Launch -----------------------------------------------------------------
if (-not $NoLaunch) {
    Write-Host "Launching $AppName ..." -ForegroundColor Cyan
    Start-Process -FilePath $ExePath
    Write-Host "Your browser should open automatically. If this is your first time, a Setup Wizard will guide you through connecting to your Copilot Studio agent."
}
