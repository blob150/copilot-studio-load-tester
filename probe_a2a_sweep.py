"""
Alternative-approach sweep for the A2A endpoint in OUR OWN tenant/agent (full control,
auth already proven correct: aud=6f46e6ed-..., scp=MCS.InvokeAsA2A, tid=a6f114ba-...).

Goal: rule in/out "we're calling the protocol wrong" vs "not provisioned yet" by trying
every legitimate variation of the call we haven't yet tried:
  - OPTIONS on both URLs (server-reported Allow/CORS methods -- free information)
  - Alternate JSON-RPC method names across A2A spec versions: message/send (current),
    tasks/send (older/pre-1.0 naming some early implementations used)
  - message/stream (SSE path) with Accept: text/event-stream
  - api-version query parameter (several guesses) -- D2E requires one, A2A's docs don't
    mention one but Microsoft APIs almost always version somehow
  - Bare tenant/environment root and bare agents/ listing (service discovery guesses)
"""
import json
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
ENV_ROOT = (
    "https://api.powerplatform.com/copilotstudio-a2a/"
    "tenants/a6f114ba-0ed3-4dec-89c9-8689136e6926/"
    "environments/ea32dda3-82d5-e9c4-a499-d7d7f2d31bf3"
)


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
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
    if cache.has_state_changed:
        _CACHE_PATH.write_text(cache.serialize())
    if "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result}")
    return result["access_token"]


def brief(label, resp):
    allow = resp.headers.get("Allow", "")
    sf = resp.headers.get("x-servicefabric", "")
    ct = resp.headers.get("content-type", "")
    body_preview = ""
    try:
        if "text/event-stream" in ct:
            body_preview = "<SSE stream — see full dump>"
        else:
            txt = resp.text[:300]
            body_preview = txt
    except Exception:
        pass
    print(f"[{label}] HTTP {resp.status_code}  Allow={allow!r}  x-servicefabric={sf!r}  "
          f"body={body_preview!r}")


def main():
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print("\n--- OPTIONS probes ---")
    for label, url in [("OPTIONS base", BASE_URL),
                        ("OPTIONS agent-card", f"{BASE_URL}/.well-known/agent-card.json"),
                        ("OPTIONS env root", ENV_ROOT)]:
        try:
            r = requests.options(url, headers=headers, timeout=30)
            brief(label, r)
        except Exception as e:
            print(f"[{label}] EXCEPTION: {e}")

    print("\n--- Bare discovery probes (GET, no suffix) ---")
    for label, url in [("GET env root", ENV_ROOT),
                        ("GET agents collection", f"{ENV_ROOT}/agents")]:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            brief(label, r)
        except Exception as e:
            print(f"[{label}] EXCEPTION: {e}")

    print("\n--- api-version query param guesses ---")
    for ver in ["2025-05-01", "2026-05-01-preview", "2025-10-01", "1.0"]:
        for suffix in ["", "/.well-known/agent-card.json"]:
            url = f"{BASE_URL}{suffix}?api-version={ver}"
            try:
                r = requests.get(url, headers=headers, timeout=30)
                brief(f"GET api-version={ver} suffix={suffix or '(base)'}", r)
            except Exception as e:
                print(f"[api-version={ver}] EXCEPTION: {e}")

    print("\n--- Alternate JSON-RPC method names (POST) ---")
    methods_to_try = ["message/send", "tasks/send", "message/stream", "tasks/sendSubscribe"]
    for method in methods_to_try:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "ping"}],
                    "messageId": str(uuid.uuid4()),
                }
            },
        }
        hdrs = dict(headers)
        if "stream" in method or "Subscribe" in method:
            hdrs["Accept"] = "text/event-stream"
        try:
            r = requests.post(BASE_URL, headers=hdrs, json=payload, timeout=30, stream=True)
            brief(f"POST method={method}", r)
        except Exception as e:
            print(f"[method={method}] EXCEPTION: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
