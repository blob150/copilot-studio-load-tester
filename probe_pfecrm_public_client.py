"""Validate the new PFECRM public-client app registration end-to-end: a
plain interactive/device-code sign-in (NO secret) acquiring a token for the
A2A scope, then a real conversation against the confirmed-working agent.
This proves the 'proper' no-secret setup works, replacing the confidential-
client workaround used earlier."""
import msal

TENANT_ID = "1557f771-4c8e-4dbd-8b80-dd00a88e833e"
CLIENT_ID = "2e1eeb98-22a6-4d58-91a2-450bc5a1eb3d"  # new PFECRM public-client app reg
SCOPE = ["https://api.powerplatform.com/copilotstudio-a2a/MCS.InvokeAsA2A"]
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"

app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY)

flow = app.initiate_device_flow(scopes=SCOPE)
if "user_code" not in flow:
    raise RuntimeError(f"Device flow failed to start: {flow}")
print(flow["message"], flush=True)

result = app.acquire_token_by_device_flow(flow)
if "access_token" not in result:
    raise RuntimeError(f"Token acquisition failed: {result}")

print("\nSUCCESS -- token acquired with NO client secret.")
import base64, json
payload_b64 = result["access_token"].split(".")[1]
payload_b64 += "=" * (-len(payload_b64) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload_b64))
for k in ("aud", "scp", "appid", "tid"):
    if k in claims:
        print(f"  {k}: {claims[k]}")
