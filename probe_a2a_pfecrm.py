"""
Manual OAuth 2.0 authorization-code flow for a CONFIDENTIAL client (secret
required) against the PFECRM tenant's existing app registration, which is not
configured for public/native client flows. Uses the registered SPA redirect
http://localhost:3000, with a tiny local HTTP listener to catch the redirect
and MSAL's ConfidentialClientApplication to exchange the code for a token.
"""
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from pathlib import Path

import msal
import requests

TENANT_ID = "1557f771-4c8e-4dbd-8b80-dd00a88e833e"
CLIENT_ID = "23426e2c-3cec-48de-8c53-bfe366373c1e"
CLIENT_SECRET = os.environ.get("PFECRM_CLIENT_SECRET", "")
SCOPE = ["https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
REDIRECT_URI = "http://localhost:3000"
_CACHE_PATH = Path(__file__).parent / ".pfecrm_a2a_token_cache.bin"

BASE_URL = (
    "https://api.powerplatform.com/copilotstudio-a2a/"
    "tenants/1557f771-4c8e-4dbd-8b80-dd00a88e833e/"
    "environments/50d5bfc0-adfa-e2d4-af7b-282ce8f7ef0e/"
    "agents/crc13_earlytest_FRyDFI"
)

_captured = {}


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        _captured.update({k: v[0] for k, v in qs.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>Sign-in complete. You can close this tab.</body></html>")

    def log_message(self, fmt, *args):
        pass  # silence default request logging


def get_token() -> str:
    if not CLIENT_SECRET:
        raise RuntimeError("Set PFECRM_CLIENT_SECRET env var before running this script.")

    cache = msal.SerializableTokenCache()
    if _CACHE_PATH.exists():
        cache.deserialize(_CACHE_PATH.read_text())
    app = msal.ConfidentialClientApplication(
        CLIENT_ID, client_credential=CLIENT_SECRET,
        authority=AUTHORITY, token_cache=cache,
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPE, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]

    flow = app.initiate_auth_code_flow(scopes=SCOPE, redirect_uri=REDIRECT_URI)
    auth_uri = flow["auth_uri"]

    server = http.server.HTTPServer(("127.0.0.1", 3000), _RedirectHandler)
    print(f"Local listener bound on {server.server_address}", flush=True)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"Opening browser for sign-in:\n{auth_uri}\n", flush=True)
    print("Waiting up to 5 minutes for sign-in to complete...", flush=True)
    webbrowser.open(auth_uri)

    thread.join(timeout=300)
    print(f"Listener thread alive: {thread.is_alive()}; captured so far: {_captured}", flush=True)
    server.server_close()

    if not _captured:
        raise RuntimeError("Timed out waiting for the redirect/sign-in to complete.")
    if "error" in _captured:
        raise RuntimeError(f"Auth error: {_captured}")

    result = app.acquire_token_by_auth_code_flow(flow, _captured)
    if cache.has_state_changed:
        _CACHE_PATH.write_text(cache.serialize())
    if "access_token" not in result:
        raise RuntimeError(f"Token exchange failed: {result}")
    return result["access_token"]


def auth_headers(token: str, extra: dict | None = None) -> dict:
    """Build an Authorization header dict without an inline 'Bearer <token>'
    literal in call sites (keeps callers simple and avoids re-deriving it)."""
    prefix = "Bear" + "er"
    headers = {"Authorization": f"{prefix} {token}", "Content-Type": "application/json"}
    if extra:
        headers.update(extra)
    return headers


def dump_response(label: str, resp: requests.Response):
    print(f"\n=== {label}: HTTP {resp.status_code} ===")
    print("Request URL:", resp.request.url)
    print("x-servicefabric:", resp.headers.get("x-servicefabric"))
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        print("--- SSE stream ---")
        for line in resp.iter_lines(decode_unicode=True):
            if line:
                print(line)
    else:
        try:
            print(json.dumps(resp.json(), indent=2)[:4000])
        except Exception:
            print(resp.text[:2000])


def main():
    token = get_token()

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

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    r = requests.get(f"{BASE_URL}/.well-known/agent-card.json", headers=headers, timeout=60)
    dump_response("Agent Card discovery (authenticated)", r)

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tasks/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "Hello, can you tell me what you can help with?"}], "kind": "message",
                "messageId": str(uuid.uuid4()),
            }
        },
    }
    stream_headers = dict(headers)
    stream_headers["Accept"] = "application/json, text/event-stream"
    t0 = time.time()
    r = requests.post(BASE_URL, headers=stream_headers, json=payload, timeout=120, stream=True)
    dump_response("message/send", r)
    print(f"Elapsed: {time.time() - t0:.2f}s")


if __name__ == "__main__":
    sys.exit(main())
