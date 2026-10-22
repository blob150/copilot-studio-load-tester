"""
Standalone smoke-test probe for the new Copilot Studio A2A channel.

Scope:  https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A
Auth:   delegated, device-code flow (MSAL public client) -- same app registration
        as our D2E tool (75bd452e-3349-47e2-9e19-068e0829180a), now also granted
        the MCS.InvokeAsA2A permission.

Goal: fire a couple of independent A2A invocations against the GHCP-harness test
agent and dump the raw request/response so we can compare shape against D2E.
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
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
_CACHE_PATH = Path(__file__).parent / ".a2a_token_cache.bin"

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


def get_token() -> str:
    cache = msal.SerializableTokenCache()
    if _CACHE_PATH.exists():
        cache.deserialize(_CACHE_PATH.read_text())
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
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
    print("Acquiring token for scope:", SCOPE)
    token = get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Decode (without verifying) just to confirm aud/scp/tid landed as expected.
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

    # 1) Agent Card discovery
    r = requests.get(f"{BASE_URL}/.well-known/agent-card.json", headers=headers, timeout=60)
    dump_response("Agent Card discovery", r)

    # 2) A couple of independent message/send calls (smoke test -- a handful of sessions)
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
