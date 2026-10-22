"""
A2A probe variant: attempts to invoke a Copilot Studio A2A channel endpoint using an
EXISTING Microsoft first-party multi-tenant public client (Azure CLI's well-known
client ID) instead of a custom app registration of our own.

Purpose: answer "do we strictly need our own app registration, or can we test an A2A
endpoint in a tenant where we have no app-registration rights at all?" Azure CLI's
client already exists as an enterprise app in every tenant (no admin action needed to
"create" it there) -- whether this actually yields a token depends entirely on that
tenant's user-consent policy for the MCS.InvokeAsA2A scope, which we can only find out
empirically.

Usage:
    python probe_a2a_cli.py --tenant <tenantId> --url <full A2A base url>
"""
import argparse
import base64
import json
import sys
import time
import uuid
from pathlib import Path

import msal
import requests

# Azure CLI's well-known, multi-tenant public client ID. Exists in every tenant
# already -- no app registration needs to be created by us or by an admin there.
AZ_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"

SCOPE = ["https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A"]


def get_token(tenant_id: str, client_id: str, cache_path: Path) -> str:
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPE, account=accounts[0])
    if not result or "access_token" not in result:
        flow = app.initiate_device_flow(scopes=SCOPE)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed to start: {flow}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        cache_path.write_text(cache.serialize())
    return result


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
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True, help="Tenant ID to authenticate against")
    ap.add_argument("--url", required=True, help="Full A2A base URL (…/agents/{schema})")
    ap.add_argument("--client", default=AZ_CLI_CLIENT_ID,
                     help="Client ID to use (default: Azure CLI's well-known public client)")
    args = ap.parse_args()

    cache_path = Path(__file__).parent / f".a2a_cli_token_cache_{args.tenant}.bin"

    print(f"Acquiring token: tenant={args.tenant} client={args.client} scope={SCOPE}")
    result = get_token(args.tenant, args.client, cache_path)

    if "access_token" not in result:
        print("\n*** TOKEN ACQUISITION FAILED ***")
        print(f"error: {result.get('error')}")
        print(f"error_description: {result.get('error_description')}")
        print(f"error_codes: {result.get('error_codes')}")
        print(
            "\nThis means the tenant's consent/CA policy blocked this client from "
            "getting a token for this scope at all -- we never even reach the agent "
            "endpoint. This is the expected failure mode if user consent to new "
            "scopes is locked down tenant-wide (very likely for Microsoft's own "
            "corporate tenant)."
        )
        return 1

    token = result["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        print("Token claims (unverified):")
        for k in ("aud", "scp", "roles", "tid", "appid", "upn", "unique_name"):
            if k in claims:
                print(f"  {k}: {claims[k]}")
    except Exception as e:
        print("Could not decode token for inspection:", e)

    base_url = args.url.rstrip("/")

    r = requests.get(f"{base_url}/.well-known/agent-card.json", headers=headers, timeout=60)
    dump_response("Agent Card discovery", r)

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "What's the weather like today?"}],
                "messageId": str(uuid.uuid4()),
            }
        },
    }
    t0 = time.time()
    r = requests.post(base_url, headers=headers, json=payload, timeout=120, stream=True)
    dump_response("message/send", r)
    print(f"Elapsed: {time.time() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
