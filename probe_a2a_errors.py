"""Error-scenario probes against the PFECRM A2A agent (raw JSON-RPC, no SDK,
so we can see the exact wire-level error shape for each failure mode)."""
import json
import time
import uuid

import requests

from probe_a2a_pfecrm import BASE_URL, get_token


def post(headers, payload, label):
    t0 = time.time()
    try:
        r = requests.post(BASE_URL, headers=headers, json=payload, timeout=30)
        dt = time.time() - t0
        print(f"\n=== {label}: HTTP {r.status_code} ({dt:.2f}s) ===")
        print("x-servicefabric:", r.headers.get("x-servicefabric"))
        print("www-authenticate:", r.headers.get("www-authenticate"))
        try:
            print(json.dumps(r.json(), indent=2)[:1500])
        except Exception:
            print(r.text[:1000])
    except requests.RequestException as e:
        print(f"\n=== {label}: TRANSPORT ERROR ({time.time()-t0:.2f}s) ===")
        print(repr(e))


def main():
    token = get_token()
    good_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1) Invalid/garbage bearer token
    bad_headers = {"Authorization": "Bearer not-a-real-token", "Content-Type": "application/json"}
    post(bad_headers, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}],
                                "messageId": str(uuid.uuid4()), "kind": "message"}},
    }, "Invalid bearer token")

    # 2) No Authorization header at all
    post({"Content-Type": "application/json"}, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}],
                                "messageId": str(uuid.uuid4()), "kind": "message"}},
    }, "Missing Authorization header")

    # 3) Malformed JSON-RPC: unknown method (valid token)
    post(good_headers, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "not/a/real/method",
        "params": {},
    }, "Unknown JSON-RPC method (valid token)")

    # 4) Malformed params: missing required 'message' field entirely
    post(good_headers, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {},
    }, "message/send with empty params")

    # 5) Empty text part
    post(good_headers, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": ""}],
                                "messageId": str(uuid.uuid4()), "kind": "message"}},
    }, "message/send with empty text")

    # 6) Bogus task id for tasks/get (task doesn't exist)
    post(good_headers, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tasks/get",
        "params": {"id": "00000000-0000-0000-0000-000000000000"},
    }, "tasks/get with nonexistent task id")


if __name__ == "__main__":
    main()
