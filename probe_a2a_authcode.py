"""
A2A probe variant using the REAL OAuth 2.0 Authorization Code flow (with PKCE),
via MSAL's acquire_token_interactive() -- opens a system browser, redirects to a
local loopback listener, and exchanges the auth code for a token. This is
architecturally different from device-code flow (what probe_a2a.py uses) and is
the specific flow the Copilot Studio A2A/MCP doc's prerequisites call for:

  "An MCP client or A2A client that supports connecting to a remote MCP server or
   A2A agent, and that supports the OAuth 2.0 authorization code flow."

Also requests `offline_access` alongside the MCS.InvokeAsA2A scope, matching the
doc's exact guidance for the scope string a client should request.

Our app registration (75bd452e-3349-47e2-9e19-068e0829180a) already has
"http://localhost" registered as a public-client redirect URI, which Entra ID
treats specially for public/native apps -- it matches any dynamically chosen
loopback port, so no further Entra changes should be needed for this flow.
"""
import json
import sys
import time
import uuid
from pathlib import Path

import msal
import requests

TENANT_ID = "a6f114ba-0ed3-4dec-89c9-8689136e6926"
CLIENT_ID = "75bd452e-3349-47e2-9e19-068e0829180a"
SCOPE = ["https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A"]
# Note: MSAL automatically appends offline_access/openid/profile to every request;
# passing them explicitly raises "reserved scope" -- they're already included.
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
_CACHE_PATH = Path(__file__).parent / ".a2a_authcode_token_cache.bin"

BASE_URL = (
    "https://api.powerplatform.com/copilotstudio-a2a/"
    "tenants/a6f114ba-0ed3-4dec-89c9-8689136e6926/"
    "environments/ea32dda3-82d5-e9c4-a499-d7d7f2d31bf3/"
    "agents/cr8f9_sopghcp_mCP7gy"
)

PROMPTS = [
    "What is the standard operating procedure for onboarding?",
    "Summarize the safety guidelines for lab access.",
]


def get_token(login_hint: str | None = None) -> str:
    cache = msal.SerializableTokenCache()
    if _CACHE_PATH.exists():
        cache.deserialize(_CACHE_PATH.read_text())
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    accounts = app.get_accounts(username=login_hint) if login_hint else app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPE, account=accounts[0])

    if not result or "access_token" not in result:
        print("No cached token -- launching interactive (browser) auth code flow...",
              flush=True)
        print("A browser window should open. Sign in and consent, then return here.",
              flush=True)
        # MSAL's acquire_token_interactive drives the full OAuth 2.0 authorization
        # code flow with PKCE: opens the system browser, spins up a local loopback
        # HTTP listener to catch the redirect, exchanges the code for tokens.
        # prompt="select_account" forces an account picker instead of silently
        # reusing whatever session the default browser already has cached.
        kwargs = {"prompt": "select_account"}
        if login_hint:
            kwargs["login_hint"] = login_hint
        result = app.acquire_token_interactive(scopes=SCOPE, **kwargs)

    if cache.has_state_changed:
        _CACHE_PATH.write_text(cache.serialize())

    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result}")
    return result["access_token"]


def dump_response(label: str, resp: requests.Response):
    print(f"\n=== {label}: HTTP {resp.status_code} ===")
    print("Request URL:", resp.request.url)
    print("Request method:", resp.request.method)
    print("All response headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        print("--- SSE stream ---")
        for line in resp.iter_lines(decode_unicode=True):
            if line:
                print(line)
    else:
        try:
            body = resp.json()
            print(json.dumps(body, indent=2)[:4000])
        except Exception:
            print(resp.text[:4000])


def main():
    print("Acquiring token (authorization code flow) for scope:", SCOPE)
    token = get_token(login_hint="admin@diax71231748.onmicrosoft.com")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        import base64
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        print("Token claims (unverified):")
        for k in ("aud", "scp", "roles", "tid", "appid", "upn", "unique_name", "amr"):
            if k in claims:
                print(f"  {k}: {claims[k]}")
    except Exception as e:
        print("Could not decode token for inspection:", e)

    r = requests.get(f"{BASE_URL}/.well-known/agent-card.json", headers=headers, timeout=60)
    dump_response("Agent Card discovery", r)

    for i, prompt in enumerate(PROMPTS, start=1):
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": prompt}],
                    "messageId": str(uuid.uuid4()),
                }
            },
        }
        t0 = time.time()
        r = requests.post(BASE_URL, headers=headers, json=payload, timeout=120, stream=True)
        dump_response(f"message/send #{i} ({prompt[:40]!r})", r)
        print(f"Elapsed: {time.time() - t0:.2f}s")


if __name__ == "__main__":
    sys.exit(main())
