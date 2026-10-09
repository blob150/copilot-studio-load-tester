# Copilot Studio Agent Load Tester

A local tool for load-testing Microsoft Copilot Studio agents (standard harness and
GitHub Copilot harness) via the **Direct-to-Engine (D2E)** API — the same endpoint
used by the Microsoft 365 Agents SDK's `CopilotStudioClient`. It includes a web
dashboard for configuring runs, watching them live, and reviewing history/trends.

Built to answer two practical questions for a real deployment:

1. **Will this agent hold up under a traffic spike** (e.g. an event with thousands
   of concurrent users), and where does it start to throttle?
2. **What does that cost** — how many Copilot Credits / messages does a given load
   pattern consume, and what tools/knowledge sources does it exercise?

> ⚠️ This is a load-testing tool. Running it against an agent generates real traffic
> and (depending on licensing/capacity) can consume real Copilot Credits. Prefer a
> dev/test/sandbox environment and a dedicated test agent.

---

## How it works

Copilot Studio agents (both the **standard harness** and the **GitHub Copilot
harness**) expose the same programmatic conversation endpoint:

```
https://{env}.{region}.environment.api.powerplatform.com
    /copilotstudio/dataverse-backed/authenticated/bots/{schemaName}/conversations
    ?api-version=2022-03-01-preview
```

This is an **Entra ID–authenticated** REST/SSE endpoint (scope
`https://api.powerplatform.com/.default`, delegated permission
`CopilotStudio.Copilots.Invoke`) — it is *not* the older Direct Line channel. A
caller authenticates as a real user (delegated auth), starts a conversation, and
exchanges activities with the agent over Server-Sent Events.

This tool drives that endpoint with many concurrent simulated sessions, models
realistic multi-turn conversations with think-time between turns, and records
every request's outcome (status, latency, throttling, tool/knowledge-source
invocations) to a local SQLite database for live viewing and historical analysis.

### Architecture

| Component | Role |
|---|---|
| `token_provider.py` | Pluggable Entra ID auth: static bearer token, device-code (interactive, MFA-compliant, cached), or ROPC (for scripted multi-account pools) |
| `d2e_client.py` | Talks to the D2E endpoint; parses SSE/JSON; detects tool/knowledge-source invocations from the agent's plan events; flags throttling |
| `engine.py` | Load engine: models virtual users running multi-turn sessions in two shapes — **event spike** (ramp-up → hold → ramp-down) or **stepped RPM** (characterization) |
| `stats.py` | Thread-safe live aggregation (KPIs, time-series, groupings) for the dashboard |
| `run_store.py` | SQLite-backed persistence: runs, per-request events, saved config presets, trend queries |
| `credits.py` | Queries `pac licensing` for real Copilot Credit allocation/consumption (not just estimates) |
| `transcripts.py` | Historical enrichment: reads Dataverse conversation transcripts to get authoritative tool/connector call counts after the fact |
| `app.py` + `templates/index.html` | Flask web UI: live dashboard + run history + trends |

### Why this can't be a Power Platform canvas app

Generating real concurrent load requires spawning many parallel HTTP sessions with
independent Entra tokens — something a single-user, single-threaded canvas app
runtime cannot do (and shouldn't: you can't use the platform under test to stress
itself). The load **generator** must be code running on a machine/VM/container. A
Power Platform **front end** (e.g. a Dataverse-backed app for config + reporting)
calling out to this engine is a reasonable hybrid if you want a managed front door
later — but the engine itself stays here.

---

## Setup

### Prerequisites

- Python 3.11+
- An Entra ID **app registration** with:
  - Platform: **Mobile and desktop applications**, redirect URI
    `https://login.microsoftonline.com/common/oauth2/nativeclient`
  - **"Allow public client flows"** = Yes
  - **API permissions** → Power Platform API → **Delegated** →
    `CopilotStudio.Copilots.Invoke` → admin-consented
- (Optional, for credit/billing queries) [Power Platform CLI (`pac`)](https://aka.ms/PowerPlatformCLI)
  installed and authenticated to the target tenant (`pac auth create --tenant <tenantId>`).
- (Optional, for historical tool/connector enrichment) Dataverse access to the
  agent's environment (uses the same cached token).

### Creating the app registration (one-time, per tenant)

Using Azure CLI (adjust the tenant):

```powershell
az login --tenant <yourTenantId> --allow-no-subscriptions --use-device-code

# Power Platform API + the CopilotStudio.Copilots.Invoke delegated scope
$ppapi = "8578e004-a5c6-46e7-913e-12f58912df43"
$scopeId = (az ad sp show --id $ppapi --query "oauth2PermissionScopes[?value=='CopilotStudio.Copilots.Invoke'].id" -o tsv)

@"
[
  { "resourceAppId": "$ppapi", "resourceAccess": [ { "id": "$scopeId", "type": "Scope" } ] }
]
"@ | Out-File rra.json -Encoding utf8

$app = az ad app create --display-name "CopilotStudio-LoadTest" `
  --sign-in-audience AzureADMyOrg --is-fallback-public-client true `
  --public-client-redirect-uris "http://localhost" "https://login.microsoftonline.com/common/oauth2/nativeclient" `
  --required-resource-accesses "@rra.json" -o json | ConvertFrom-Json

az ad sp create --id $app.appId
az ad app permission admin-consent --id $app.appId   # requires admin rights

"Client ID: $($app.appId)"
```

> If your tenant blocks the Azure CLI's first-party app via Conditional Access,
> try `Connect-MgGraph` (Microsoft Graph PowerShell) instead, or create the app
> registration manually in the Entra admin center (App registrations → New
> registration → Authentication → add the Mobile/desktop platform and enable
> public client flows → API permissions → add the delegated scope above → grant
> admin consent).

### Install & run

```powershell
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000**.

In the dashboard:
1. Paste the agent's full D2E endpoint URL (host/schema auto-populate).
2. Fill in **Environment ID** (for credit lookups), **Tenant ID**, **Client ID**
   (from the app registration above).
3. Click **Authenticate now** and complete the device-code sign-in (one time —
   the token is cached in `.token_cache.bin` and reused across runs until it
   expires).
4. Configure the load shape, prompts, and think-time, then **Start Test**.
5. Use **Save config** to name and store the current form so you can reload it
   later without retyping.

---

## Load modes

- **Event spike** — `virtual_users`, `rampup_seconds`, `hold_seconds`,
  `rampdown_seconds`, `sessions_per_vu`. Models a real-world concentrated usage
  spike (e.g. an event driving thousands of users to an agent at once).
- **Stepped RPM** — `steps` (comma-separated target RPM values), `step_hold_seconds`,
  `max_concurrency`. Walks the request rate upward to find where throttling begins
  and characterize the environment's effective quota.

Both models multi-turn sessions (`turns_per_session`, `think_time_seconds`) and
rotate through a list of `prompts` per turn.

## Reading the results

**Live Test tab**: KPIs (requests, success %, errors, throttled, active sessions,
achieved RPM, p50/p95 latency, live tool calls, live knowledge-source hits),
throughput/latency charts, grouping by status code and by request kind
(start vs. turn), a live tool-invocation table, and a recent-events feed.

**Run History tab**: every run persisted with its config and final aggregates,
plus:
- **Tools (live)** — best-effort count parsed from the SSE stream in real time.
- **Tools (transcript)** — authoritative count from Dataverse conversation
  transcripts, fetched on demand via **Enrich tools** (requires the environment's
  Dataverse org URL).
- **Credits (MCS)** — real consumed/allocated Copilot Credit numbers from
  `pac licensing`, fetched on demand via **Refresh credits**. Consumption data
  aggregates on a periodic (roughly daily) snapshot, not in real time — the
  snapshot timestamp is shown so you can tell whether your test's traffic has
  been captured yet.
- A **trend chart** across all historical runs (p50/p95 latency, success rate,
  throttled count), optionally filtered by agent schema name.

## Known limitations / things to verify per-tenant

- The D2E endpoint and the delegated auth model are consistent across harnesses,
  but the **GitHub Copilot harness does not expose native Application Insights
  telemetry** the way the standard harness does — only this tool's client-side
  (black-box) metrics are available for it today.
- **ROPC** (non-interactive username/password auth, useful for simulating a pool
  of many distinct users without manual sign-in) requires the target accounts to
  be excluded from MFA/Conditional Access, and may be disabled entirely by
  tenant policy (Microsoft's Secure Future Initiative is phasing it out).
- Generative request quotas are enforced **per Dataverse environment**
  (typically 50–100 RPM by default, adjustable case-by-case) — size tests
  accordingly and expect throttling if you exceed it deliberately (that's
  often the point of the test).
- Tool/knowledge-source detection parses the agent's `DynamicPlanStepTriggered`
  plan events; this is specific to the standard-harness dynamic planning model
  observed during development and may need adjustment for other agent patterns.

## Repository layout

```
app.py                 Flask server
engine.py               Load engine (spike / stepped modes)
stats.py                Live stats aggregation
d2e_client.py            D2E HTTP client (SSE/JSON parsing, tool detection)
token_provider.py        Auth providers (static / device-code / ROPC)
run_store.py             SQLite persistence (runs, events, presets, trends)
credits.py               pac licensing credit/consumption queries
transcripts.py           Dataverse transcript enrichment (historical tool counts)
templates/index.html     Web dashboard
requirements.txt
config.example.json      Example config for the legacy CLI tools below
smoke_test.py / ramp.py  Original CLI tools (pre-UI); still usable standalone
```

## License

TBD — private repo for now.
