"""Error-scenario probes against the PFECRM A2A agent using the REAL wire
format discovered from the official SDK (proto-JSON transcoding, PascalCase
RPC method names), not the generic A2A JSON-RPC spec names."""
import json
import time
import uuid

import requests

from probe_a2a_pfecrm import BASE_URL, auth_headers, get_token


def post(headers, payload, label, accept_stream=False):
    h = dict(headers)
    if accept_stream:
        h["Accept"] = "text/event-stream"
    t0 = time.time()
    try:
        r = requests.post(BASE_URL, headers=h, json=payload, timeout=30, stream=accept_stream)
        dt = time.time() - t0
        print(f"\n=== {label}: HTTP {r.status_code} ({dt:.2f}s) ===")
        print("content-type:", r.headers.get("content-type"))
        print("x-servicefabric:", r.headers.get("x-servicefabric"))
        if r.headers.get("www-authenticate"):
            print("www-authenticate:", r.headers.get("www-authenticate"))
        if accept_stream and "text/event-stream" in (r.headers.get("content-type") or ""):
            for line in r.iter_lines(decode_unicode=True):
                if line:
                    print(line[:500])
        else:
            try:
                print(json.dumps(r.json(), indent=2)[:1500])
            except Exception:
                print(r.text[:1000])
    except requests.RequestException as e:
        print(f"\n=== {label}: TRANSPORT ERROR ({time.time()-t0:.2f}s) ===")
        print(repr(e))


def main():
    token = get_token()
    good = auth_headers(token)
    bad_token = auth_headers("not-a-real-token")

    # 1) Invalid bearer token, correct wire format
    post(bad_token, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendStreamingMessage",
        "params": {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                                "parts": [{"text": "hi"}]},
                   "configuration": {"acceptedOutputModes": ["text/plain"]}},
    }, "Invalid bearer token (correct wire format)", accept_stream=True)

    # 2) Correct method name, but malformed params (message missing entirely)
    post(good, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendStreamingMessage",
        "params": {},
    }, "SendStreamingMessage with empty params", accept_stream=True)

    # 3) Correct method, empty text part
    post(good, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendStreamingMessage",
        "params": {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                                "parts": [{"text": ""}]},
                   "configuration": {"acceptedOutputModes": ["text/plain"]}},
    }, "SendStreamingMessage with empty text", accept_stream=True)

    # 4) Correct method, bad role enum value
    post(good, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendStreamingMessage",
        "params": {"message": {"messageId": str(uuid.uuid4()), "role": "NOT_A_REAL_ROLE",
                                "parts": [{"text": "hi"}]},
                   "configuration": {"acceptedOutputModes": ["text/plain"]}},
    }, "SendStreamingMessage with invalid role enum", accept_stream=True)

    # 5) Nonexistent/garbage contextId (continuing a conversation that doesn't exist)
    post(good, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendStreamingMessage",
        "params": {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                                "contextId": "00000000-0000-0000-0000-000000000000",
                                "parts": [{"text": "continue please"}]},
                   "configuration": {"acceptedOutputModes": ["text/plain"]}},
    }, "SendStreamingMessage with nonexistent contextId", accept_stream=True)

    # 6) Non-streaming variant (SendMessage instead of SendStreamingMessage)
    post(good, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendMessage",
        "params": {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER",
                                "parts": [{"text": "hi, non-streaming please"}]},
                   "configuration": {"acceptedOutputModes": ["text/plain"]}},
    }, "SendMessage (non-streaming variant)")


if __name__ == "__main__":
    main()
