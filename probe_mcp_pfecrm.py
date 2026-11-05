"""Check whether the PFECRM agent's MCP endpoint is reachable with the same
app registration/token audience pattern as A2A, and if so, exercise it the
same way (discovery, a real tool call, and error scenarios) for comparison."""
import json
import time
import uuid

import requests

from probe_a2a_pfecrm import TENANT_ID, CLIENT_ID, CLIENT_SECRET, AUTHORITY, REDIRECT_URI
import msal
from pathlib import Path

MCP_SCOPE = ["https://api.powerplatform.com/copilotstudio-mcp/MCS.InvokeAsMCP"]
MCP_BASE_URL = (
    "https://api.powerplatform.com/copilotstudio-mcp/"
    "tenants/1557f771-4c8e-4dbd-8b80-dd00a88e833e/"
    "environments/50d5bfc0-adfa-e2d4-af7b-282ce8f7ef0e/"
    "agents/crc13_earlytest_FRyDFI"
)
_CACHE_PATH = Path(__file__).parent / ".pfecrm_mcp_token_cache.bin"


def get_mcp_token() -> str:
    import http.server
    import threading
    import webbrowser

    cache = msal.SerializableTokenCache()
    if _CACHE_PATH.exists():
        cache.deserialize(_CACHE_PATH.read_text())
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, client_credential=CLIENT_SECRET, authority=AUTHORITY, token_cache=cache,
    )
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(MCP_SCOPE, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    flow = app.initiate_auth_code_flow(scopes=MCP_SCOPE, redirect_uri=REDIRECT_URI)
    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html><body>Sign-in complete, close this tab.</body></html>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 3000), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print(f"Opening browser for MCP scope consent:\n{flow['auth_uri']}\n")
    print("Waiting up to 5 minutes for sign-in/consent to complete...")
    webbrowser.open(flow["auth_uri"])
    thread.join(timeout=300)
    print(f"Listener alive: {thread.is_alive()}; captured: {captured}")
    server.server_close()
    if not captured:
        raise RuntimeError("Timed out waiting for MCP sign-in redirect.")
    result = app.acquire_token_by_auth_code_flow(flow, captured)
    if cache.has_state_changed:
        _CACHE_PATH.write_text(cache.serialize())
    if "access_token" not in result:
        raise RuntimeError(f"MCP token exchange failed: {result}")
    return result["access_token"]


def parse_sse_json(resp: requests.Response) -> dict:
    """Parse a single JSON-RPC response out of an SSE stream body."""
    if "text/event-stream" not in (resp.headers.get("content-type") or ""):
        return resp.json()
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise ValueError("No data: line found in SSE body")


def main():
    token = get_mcp_token()
    prefix = "Bear" + "er"
    headers = {"Authorization": f"{prefix} {token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    # MCP initialize handshake (JSON-RPC, per MCP spec)
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe-mcp-pfecrm", "version": "0.1"},
        },
    }
    t0 = time.time()
    r = requests.post(MCP_BASE_URL, headers=headers, json=init_payload, timeout=30)
    print(f"=== MCP initialize: HTTP {r.status_code} ({time.time()-t0:.2f}s) ===")
    print("content-type:", r.headers.get("content-type"))
    print("x-servicefabric:", r.headers.get("x-servicefabric"))
    session_id = r.headers.get("mcp-session-id")
    print("mcp-session-id:", session_id)
    print(json.dumps(parse_sse_json(r), indent=2)[:1500])

    if session_id:
        headers["Mcp-Session-Id"] = session_id

    # Send the "initialized" notification (required by MCP handshake before other calls)
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    r = requests.post(MCP_BASE_URL, headers=headers, json=notif, timeout=30)
    print(f"\n=== notifications/initialized: HTTP {r.status_code} ===")

    # tools/list
    list_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    t0 = time.time()
    r = requests.post(MCP_BASE_URL, headers=headers, json=list_payload, timeout=30)
    print(f"\n=== tools/list: HTTP {r.status_code} ({time.time()-t0:.2f}s) ===")
    try:
        tools_result = parse_sse_json(r)
        print(json.dumps(tools_result, indent=2)[:2000])
    except Exception as e:
        print("Parse error:", e, r.text[:500])

    # tools/call send-message (real conversation)
    call_payload = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "send-message", "arguments": {"message": "Hello, what can you help with?"}},
    }
    t0 = time.time()
    r = requests.post(MCP_BASE_URL, headers=headers, json=call_payload, timeout=60)
    print(f"\n=== tools/call send-message: HTTP {r.status_code} ({time.time()-t0:.2f}s) ===")
    try:
        print(json.dumps(parse_sse_json(r), indent=2)[:3000])
    except Exception as e:
        print("Parse error:", e, r.text[:1000])

    # Error scenario: bad token
    bad_headers = dict(headers)
    bad_headers["Authorization"] = "Bearer not-a-real-token"
    r = requests.post(MCP_BASE_URL, headers=bad_headers, json=list_payload, timeout=30)
    print(f"\n=== tools/list with invalid token: HTTP {r.status_code} ===")
    print("www-authenticate:", r.headers.get("www-authenticate"))
    print(r.text[:500])

    # Error scenario: unknown tool name
    bad_call = {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "not-a-real-tool", "arguments": {}},
    }
    r = requests.post(MCP_BASE_URL, headers=headers, json=bad_call, timeout=30)
    print(f"\n=== tools/call with unknown tool name: HTTP {r.status_code} ===")
    try:
        print(json.dumps(parse_sse_json(r), indent=2)[:1000])
    except Exception as e:
        print("Parse error:", e, r.text[:500])


if __name__ == "__main__":
    main()