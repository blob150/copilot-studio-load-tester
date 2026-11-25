# Copilot Studio Load Tester — Installer

> ⚠️ **Unsupported sample code.** Not an official Microsoft product, not
> covered by any support agreement or SLA. Provided as-is, use at your own
> risk — see the main repo README for the full disclaimer.

## Quick start

1. Double-click **Install.bat**.
2. A console window opens and installs the app for your user account
   (no admin rights needed). It creates Start Menu and Desktop shortcuts
   and launches the app.
3. Your default browser opens to the app automatically. If this is your
   first time, a **Setup Wizard** walks you through:
   - Checking for optional CLI tools (`az`, `pac`) used for enrichment
     features (Application Insights, credit/consumption lookups).
   - Creating an Entra ID app registration in your tenant (copy-pasteable
     PowerShell script + manual admin-center steps), if you don't already
     have one.
   - Signing in and saving your connection as an Auth Profile.

## What gets installed, and where

- **App files**: `%LOCALAPPDATA%\Programs\CopilotStudioLoadTester\`
- **Your data** (saved connections, sign-in token cache, run history,
  presets): `%LOCALAPPDATA%\CopilotStudioLoadTester\`

These are kept separate on purpose — reinstalling or upgrading the app
(re-running Install.bat) never touches your saved data.

## Uninstalling

Run:

```
powershell -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\Programs\CopilotStudioLoadTester\uninstall.ps1"
```

Add `-RemoveData` to also delete your saved connections and run history.

## Advanced install options

Run `install.ps1` directly from PowerShell for extra options:

```
powershell -ExecutionPolicy Bypass -File install.ps1 -NoDesktopShortcut
powershell -ExecutionPolicy Bypass -File install.ps1 -NoLaunch
```

## Troubleshooting

- **Windows SmartScreen warning**: this is an unsigned internal tool, so
  Windows may show "Windows protected your PC" the first time you run
  Install.bat or the app itself. Click **More info** → **Run anyway**.
- **Port 5000 already in use**: if the app is already running, launching
  it again just opens another browser tab pointed at the existing
  instance — this is expected and safe.
