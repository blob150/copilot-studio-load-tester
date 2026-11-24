"""
First-time setup wizard support: prerequisite checks (az/pac CLI) and the
static Entra app-registration guide shown to a brand-new user before they've
created/saved any Auth Profile.

Deliberately thin: the actual "does this app registration work" validation
reuses the EXISTING device-code sign-in flow (/api/authenticate,
/api/auth_status) and EXISTING /api/auth_profiles save route -- this module
does not duplicate auth logic, it only adds the guidance/prereq layer on top
of what already works, so the wizard can't drift out of sync with the real
sign-in path.
"""
from __future__ import annotations

import json
import shutil
import subprocess


def _run(exe: str, *args, timeout: float = 15) -> tuple[int, str]:
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, str(e)


def _check_az() -> dict:
    exe = shutil.which("az")
    if not exe:
        return {"installed": False, "signed_in": False, "account": None,
                "note": "Azure CLI not found on PATH -- only needed for the optional "
                        "Application Insights enrichment source."}
    rc, text = _run(exe, "account", "show", "-o", "json", timeout=20)
    if rc != 0:
        return {"installed": True, "signed_in": False, "account": None,
                "note": "Installed, but not signed in -- run `az login` before using "
                        "the App Insights enrichment source."}
    try:
        data = json.loads(text)
        return {"installed": True, "signed_in": True,
                "account": data.get("user", {}).get("name"), "note": ""}
    except json.JSONDecodeError:
        return {"installed": True, "signed_in": False, "account": None,
                "note": "Installed, but couldn't parse `az account show` output."}


def _check_pac() -> dict:
    exe = shutil.which("pac")
    if not exe:
        return {"installed": False, "signed_in": False, "account": None,
                "note": "Power Platform CLI not found on PATH -- only needed for the "
                        "optional credit/consumption lookup feature."}
    rc, text = _run(exe, "auth", "list", timeout=20)
    if rc != 0 or "No profiles" in text:
        return {"installed": True, "signed_in": False, "account": None,
                "note": "Installed, but no authenticated profile found -- run "
                        "`pac auth create --tenant <tenantId>` before using credit lookups."}
    return {"installed": True, "signed_in": True, "account": None, "note": ""}


def check_prereqs() -> dict:
    """Both az and pac are OPTIONAL (only needed for App Insights enrichment
    and credit lookups respectively) -- the core load-testing feature works
    fully without either, so this is informational, not a hard gate."""
    return {"az": _check_az(), "pac": _check_pac()}


# Static copy for the guided "create an app registration" step -- mirrors
# README.md's "Creating the app registration" section. Kept here (not just in
# the README) so the in-app wizard can show it without the user needing to
# go find/open a separate file, and so the exact PowerShell snippet is
# copy-pasteable straight out of the UI.
ENTRA_SETUP_GUIDE = {
    "summary": "One Entra app registration, created once per tenant, works for every "
               "protocol this tool supports (D2E, A2A, MCP). It needs to be a PUBLIC "
               "client (no secret to store/rotate) with two delegated API permissions, "
               "admin-consented.",
    "permissions": [
        {"api": "Power Platform API", "scope": "CopilotStudio.Copilots.Invoke",
         "used_for": "Direct-to-Engine (D2E) protocol"},
        {"api": "Microsoft Copilot Studio Agent MCP and A2A channels",
         "scope": "MCS.InvokeAsA2A", "used_for": "A2A protocol"},
        {"api": "Microsoft Copilot Studio Agent MCP and A2A channels",
         "scope": "MCS.InvokeAsMCP", "used_for": "MCP protocol"},
    ],
    "powershell_script": r"""az login --tenant <yourTenantId> --use-device-code

# Power Platform API (D2E) + Copilot Studio A2A/MCP channels (both first-party,
# multi-tenant resource apps -- same app IDs in every tenant)
$ppapi  = "8578e004-a5c6-46e7-913e-12f58912df43"
$a2amcp = "6f46e6ed-07bf-4616-80f0-47546850f654"

$d2eScope = (az ad sp show --id $ppapi  --query "oauth2PermissionScopes[?value=='CopilotStudio.Copilots.Invoke'].id" -o tsv)
$a2aScope = (az ad sp show --id $a2amcp --query "oauth2PermissionScopes[?value=='MCS.InvokeAsA2A'].id" -o tsv)
$mcpScope = (az ad sp show --id $a2amcp --query "oauth2PermissionScopes[?value=='MCS.InvokeAsMCP'].id" -o tsv)

@"
[
  { "resourceAppId": "$ppapi",  "resourceAccess": [ { "id": "$d2eScope", "type": "Scope" } ] },
  { "resourceAppId": "$a2amcp", "resourceAccess": [
      { "id": "$a2aScope", "type": "Scope" },
      { "id": "$mcpScope", "type": "Scope" }
  ] }
]
"@ | Out-File rra.json -Encoding utf8

$app = az ad app create --display-name "Copilot Studio Load Tester" `
  --sign-in-audience AzureADMyOrg --is-fallback-public-client true `
  --public-client-redirect-uris "http://localhost" "https://login.microsoftonline.com/common/oauth2/nativeclient" `
  --required-resource-accesses "@rra.json" -o json | ConvertFrom-Json

az ad sp create --id $app.appId
az ad app permission admin-consent --id $app.appId   # requires admin rights; grants tenant-wide, one time

"Client ID: $($app.appId)"
""",
    "manual_steps": [
        "Entra admin center -> App registrations -> New registration",
        "Authentication -> add platform 'Mobile and desktop applications' -> "
        "redirect URI http://localhost (and the nativeclient one above)",
        "Authentication -> 'Allow public client flows' -> Yes",
        "API permissions -> Add a permission -> APIs my organization uses -> search "
        "'Power Platform API' -> Delegated -> CopilotStudio.Copilots.Invoke",
        "API permissions -> Add a permission -> APIs my organization uses -> search "
        "'Microsoft Copilot Studio Agent MCP and A2A channels' -> Delegated -> "
        "MCS.InvokeAsA2A and MCS.InvokeAsMCP",
        "API permissions -> Grant admin consent for <tenant> (requires admin rights)",
    ],
}
