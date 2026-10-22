"""
Standalone smoke-test probe for the new Copilot Studio MCP client channel.

Scope:  https://api.powerplatform.com/copilotstudio-mcp/MCS.InvokeAsMCP
Auth:   real OAuth 2.0 authorization code flow (PKCE, via MSAL's
        acquire_token_interactive) -- same app registration as our A2A probes
        (75bd452e-3349-47e2-9e19-068e0829180a), now also granted MCS.InvokeAsMCP.

Protocol: Model Context Protocol (MCP) -- JSON-RPC 2.0 over HTTP, with its own
method set distinct from A2A's: `initialize` (handshake/capability negotiation),
`tools/list` (discover what the agent exposes as a callable tool), `tools/call`
(actually invoke it). MCP typically requires an `Mcp-Session-Id` style flow or at
minimum correct `Accept` headers (JSON + text/event-stream) per the 2026 MCP
streamable-HTTP transport spec.
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
SCOPE = ["https://api.powerplatform.com/copilotstudio-mcp/MCS.InvokeAsMCP"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
_CACHE_PATH = Path(__file__).parent / ".mcp_authcode_token_cache.bin"

BASE_URL = (
    "https://api.powerplatform.com/copilotstudio-mcp/"
    "tenants/a6f114ba-0ed3-4dec-89c9-8689136e6926/"
    "environments/ea32dda3-82d5-e9c4-a499-d7d7f2d31bf3/"
    "agents/cr8f9_sopghcp_mCP7gy"
)


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


def jsonrpc(headers, method, params=None, session_id=None):
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
    if params is not None:
        payload["params"] = params
    hdrs = dict(headers)
    if session_id:
        hdrs["Mcp-Session-Id"] = session_id
    r = requests.post(BASE_URL, headers=hdrs, json=payload, timeout=60, stream=True)
    return r


def main():
    print("Acquiring token (authorization code flow) for scope:", SCOPE)
    token = get_token(login_hint="admin@diax71231748.onmicrosoft.com")

    try:
        import base64
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        print("Token claims (unverified):")
        for k in ("aud", "scp", "roles", "tid", "appid", "upn", "unique_name"):
            if k in claims:
                print(f"  {k}: {claims[k]}")
    except Exception as e:
        print("Could not decode token for inspection:", e)

    # MCP streamable-HTTP transport (2026 spec) expects both JSON and SSE accepted.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    # 1) OPTIONS -- free info on what the gateway thinks this route supports.
    try:
        r = requests.options(BASE_URL, headers=headers, timeout=30)
        dump_response("OPTIONS base", r)
    except Exception as e:
        print("OPTIONS EXCEPTION:", e)

    # 2) MCP handshake: initialize
    r = jsonrpc(headers, "initialize", {
        "protocolVersion": "2026-06-18",
        "capabilities": {},
        "clientInfo": {"name": "copilot-studio-load-tester", "version": "0.1"},
    })
    dump_response("initialize", r)
    session_id = r.headers.get("Mcp-Session-Id")
    if session_id:
        print(f"Got Mcp-Session-Id: {session_id}")

    # 3) tools/list -- discover what's exposed
    r = jsonrpc(headers, "tools/list", {}, session_id=session_id)
    dump_response("tools/list", r)

    # 4) Try tools/call with a generic guess at a tool name (likely to fail with a
    #    clean JSON-RPC error if the name is wrong -- that itself is useful signal,
    #    since a JSON-RPC-level error means the request REACHED the MCP handler).
    r = jsonrpc(headers, "tools/call", {
        "name": "chat",
        "arguments": {"message": "What is the standard operating procedure for onboarding?"},
    }, session_id=session_id)
    dump_response("tools/call (guessed name='chat')", r)


if __name__ == "__main__":
    sys.exit(main())
