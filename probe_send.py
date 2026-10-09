"""Probe the continue-conversation contract. Token is read silently from cache."""
import json
import requests
from token_provider import DeviceCodeProvider
import json as _j

cfg = _j.loads(open("config.json").read())
prov = DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"])
token = prov.get_token()
base = (f"https://{cfg['host']}/copilotstudio/dataverse-backed/authenticated/bots/"
        f"{cfg['schema']}")
av = cfg["api_version"]
H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
     "Accept": "text/event-stream, application/json"}

# fresh conversation
r = requests.post(f"{base}/conversations?api-version={av}", headers=H,
                  json={"emitStartConversationEvent": True}, timeout=60)
conv = None
for line in r.text.splitlines():
    if line.startswith("data:"):
        try:
            d = json.loads(line[5:].strip())
            conv = (d.get("conversation") or {}).get("id") or conv
        except Exception:
            pass
print("conv:", conv)

Q = "What is the standard operating procedure for onboarding?"
variants = {
    "A_bare_message": {"type": "message", "text": Q},
    "B_with_conv": {"type": "message", "text": Q,
                     "conversation": {"id": conv}},
    "C_locale_textformat": {"type": "message", "text": Q, "locale": "en-US",
                             "textFormat": "plain"},
    "D_wrapped_activity": {"activity": {"type": "message", "text": Q}},
}
url = f"{base}/conversations/{conv}?api-version={av}"
for name, body in variants.items():
    rr = requests.post(url, headers=H, json=body, timeout=60)
    snippet = rr.text[:300].replace("\n", " ")
    print(f"\n[{name}] status={rr.status_code} ct={rr.headers.get('Content-Type')}")
    print(f"   body: {snippet}")
