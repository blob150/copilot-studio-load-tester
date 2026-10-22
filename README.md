# Copilot Studio Agent Load Tester

A local tool for load-testing Microsoft Copilot Studio agents via the
**Direct-to-Engine (D2E)** API — the same endpoint used by the Microsoft 365 Agents
SDK's `CopilotStudioClient`. It includes a web dashboard for configuring runs,
watching them live, and reviewing history/trends, plus a growing set of exploratory
scripts investigating newer external-invocation channels (A2A, MCP) for agents built
on the **GitHub Copilot (GHCP) harness** — see [GHCP harness support](#ghcp-harness-support-read-this-first)
below before testing a GHCP-harness agent.

Built to answer three practical questions for a real deployment:

1. **Will this agent hold up under a traffic spike** (e.g. an event with thousands
   of concurrent users), and where does it start to throttle?
2. **What does that cost** — how many Copilot Credits / messages does a given load
   pattern consume, and what tools/knowledge sources does it exercise?
3. **Can this agent even be invoked externally at all** — critically important for
   GHCP-harness agents, which the standard D2E endpoint flatly rejects (see below).

> ⚠️ This is a load-testing tool. Running it against an agent generates real traffic
> and (depending on licensing/capacity) can consume real Copilot Credits. Prefer a
> dev/test/sandbox environment and a dedicated test agent.

---

## GHCP harness support (read this first)

Copilot Studio currently has **two distinct agent-authoring experiences**: the
**standard harness** (classic Copilot Studio agents) and the newer **GitHub Copilot
harness** ("GHCP harness", the "new agent experience"). They do **not** expose the
same invocation surfaces:

| | Standard harness | GitHub Copilot harness |
|---|---|---|
| D2E endpoint (`/copilotstudio/dataverse-backed/.../conversations`) | ✅ Works | ❌ **Hard rejection** — HTTP 200 + canned message *"This action doesn't support agents built with the GitHub Copilot harness"*, then `turn.complete`. No real conversation happens. |
| Direct Line / Power Automate "Microsoft Copilot Studio" connector | ✅ Works | ❌ Also rejected (Microsoft-acknowledged, unresolved as of this writing — see `microsoft/Agents-for-net#1015`) |
| A2A client channel (`copilotstudio-a2a`, preview) | N/A | ⚠️ Auth works; backend currently returns a bare `404 ResourceNotFound` for every call in our testing (tenant-dependent rollout, not a client-side problem — see `probe_a2a*.py`) |
| MCP client channel (`copilotstudio-mcp`, preview) | N/A | ⚠️ Auth works; backend currently returns a clean JSON-RPC error *"The MCP endpoint is not enabled for this environment"* (see `probe_mcp.py`) |

**This tool auto-detects the D2E rejection** (`harness_unsupported` on each turn,
surfaced as a `harness_guess` per run and a red warning banner mid-test) so you never
mistake a 100%-rejected run for a successful one — but if your target agent is GHCP-
harness, **the only confirmed-working way to generate real load against it today is
the Agent node inside a Copilot Studio "workflow"**, driven from inside Studio itself
(no external REST contract exists for that path). The `probe_a2a*.py` / `probe_mcp.py`
scripts in this repo are kept as ready-to-rerun checks for when Microsoft finishes
rolling out the A2A/MCP channels — see [A2A / MCP exploration scripts](#a2a--mcp-exploration-scripts-experimental).

---

## How it works

The D2E endpoint is an **Entra ID–authenticated** REST/SSE endpoint:

```
https://{env}.{region}.environment.api.powerplatform.com
    /copilotstudio/dataverse-backed/authenticated/bots/{schemaName}/conversations
    ?api-version=2022-03-01-preview
```

(scope `https://api.powerplatform.com/.default`, delegated permission
`CopilotStudio.Copilots.Invoke`) — this is *not* the older Direct Line channel. A
caller authenticates as a real user (delegated auth), starts a conversation, and
exchanges activities with the agent over Server-Sent Events.

This tool drives that endpoint with many concurrent simulated sessions, models
realistic multi-turn conversations with think-time between turns, and records
every request's outcome (status, latency, throttling, tool/knowledge-source
invocations, embedded downstream tool errors) to a local SQLite database for live
viewing and historical analysis.

### Architecture

| Component | Role |
|---|---|
| `token_provider.py` | Pluggable Entra ID auth: static bearer token, device-code (interactive, MFA-compliant, cached), ROPC (scripted multi-account pools), or an **Account Pool** (multi-account device-code wizard — see below) |
| `d2e_client.py` | Talks to the D2E endpoint; parses SSE/JSON; detects tool/knowledge-source invocations and embedded downstream tool/connector errors from the agent's plan events; flags throttling and GHCP-harness rejection |
| `engine.py` | Load engine: models virtual users running multi-turn sessions in three shapes — **smoke test**, **event spike** (ramp-up → hold → ramp-down), or **stepped RPM** (characterization) |
| `stats.py` | Thread-safe live aggregation (KPIs incl. peak concurrency and unique identities used, time-series, groupings) for the dashboard |
| `run_store.py` | SQLite-backed persistence: runs, per-request events, saved config presets, trend queries, session/conversation drill-down queries |
| `credits.py` | Queries `pac licensing` for real Copilot Credit allocation/consumption (not just estimates) |
| `transcripts.py` | Historical enrichment: reads Dataverse conversation transcripts to get authoritative tool/connector call counts after the fact |
| `app.py` + `templates/index.html` | Flask web UI: live dashboard, run history (with CSV export), session explorer, account pool manager |
| `probe_a2a.py`, `probe_a2a_authcode.py`, `probe_a2a_cli.py`, `probe_a2a_sweep.py` | Standalone scripts testing the new **A2A client channel** for GHCP-harness agents (device-code, real auth-code/PKCE flow, an unauthenticated/first-party-client variant, and a protocol-variation sweep, respectively) |
| `probe_mcp.py` | Standalone script testing the new **MCP client channel** for GHCP-harness agents |
| `start_app.ps1` / `start_app.bat` / `stop_app.bat` | Desktop launcher — see [Launching the app](#launching-the-app) |

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
    `https://login.microsoftonline.com/common/oauth2/nativeclient` (and/or
    `http://localhost`, which Entra treats specially for public clients — matches
    any dynamically-chosen loopback port, needed for the A2A/MCP auth-code-flow probes)
  - **"Allow public client flows"** = Yes
  - **API permissions** → Power Platform API → **Delegated** →
    `CopilotStudio.Copilots.Invoke` → admin-consented
  - (Optional, only if you'll also explore the A2A/MCP channels) the same app
    registration can additionally be granted `MCS.InvokeAsA2A` and/or
    `MCS.InvokeAsMCP` against application ID `6f46e6ed-07bf-4616-80f0-47546850f654`
    ("Microsoft Copilot Studio Agent as MCP channel") — see `probe_a2a.py` / `probe_mcp.py`.
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

### Launching the app

Once set up, you don't need to run `python app.py` by hand every time:

- **`start_app.bat`** (double-click, or point a desktop shortcut at it) — checks if
  the server's already running on port 5000; if not, starts it silently in the
  background via `pythonw.exe` (no console window), then opens your default browser
  to the dashboard. Logs go to `server.log` / `server.err` in this folder.
- **`stop_app.bat`** — stops the server if it's running.
- The underlying logic is in `start_app.ps1` (`start_app.bat` is a thin wrapper that
  bypasses PowerShell's execution-policy prompt just for this one invocation — it
  does not change your system-wide policy).

To create a desktop shortcut: right-click `start_app.bat` → **Send to** → **Desktop
(create shortcut)**, or build one via PowerShell pointing its target at the full
path to `start_app.bat`. (Note: on machines with a OneDrive-redirected Desktop, the
real path is under `...\OneDrive - <org>\Desktop`, not the plain profile path.)

In the dashboard:
1. Paste the agent's full D2E endpoint URL (host/schema auto-populate).
2. Fill in **Environment ID** (for credit lookups), **Tenant ID**, **Client ID**
   (from the app registration above).
3. Click **Authenticate now** and complete the device-code sign-in (one time —
   the token is cached in `.token_cache.bin` and reused across runs until it
   expires). The sign-in code is shown in a large, easy-to-read/copy callout box.
4. Configure the load mode, prompts, and think-time, then **Start Test**.
5. Use **Save config** to name and store the current form so you can reload it
   later without retyping.

---

## Configuration reference

Every field below can be set in the dashboard form and is persisted verbatim (as
JSON) with each run and each saved preset — this is the authoritative list of every
parameter the engine actually reads.

### Connection & agent identity

| Parameter | Meaning |
|---|---|
| `url` | The full D2E conversation URL pasted into the form; parsed client-side to auto-fill `host`/`schema` (not sent to the engine directly). |
| `host` | The D2E endpoint hostname, e.g. `{env}.{region}.environment.api.powerplatform.com`. |
| `schema` | The agent's Dataverse bot schema name (the `{schemaName}` segment of the D2E URL). |
| `api_version` | D2E API version query param. Defaults to `2022-03-01-preview` in `D2EClient`; not currently a form field (edit the URL to change it). |
| `environment_id` | The Dataverse environment GUID — used for Copilot Credit lookups (`pac licensing`), unrelated to the conversation call itself. |
| `org_url` | The environment's Dataverse org URL (e.g. `https://orgxxxx.crm.dynamics.com`) — only used by the **Enrich tools** historical transcript lookup in Run History. |

### Authentication

| Parameter | Meaning |
|---|---|
| `auth_mode` | `static` (paste a bearer token directly — fastest smoke test), `devicecode` (interactive MFA-compliant sign-in, single cached account), or `pool` (Account Pool — see below). |
| `tenant_id` | Entra tenant GUID containing the agent and the app registration. |
| `client_id` | The app registration's Application (client) ID. |
| `static_token` | Only used when `auth_mode=static`: a bearer token pasted directly (e.g. from `pac auth` or browser devtools). Expires quickly — mainly for one-off smoke checks. |

**Account Pool**: click **"+ Add account"** repeatedly (Live Test tab or Accounts
tab) to sign in N distinct accounts via N separate device-code flows — each one gets
its own large, copy-friendly code callout. All pooled accounts share one persisted
MSAL cache; `engine.py` assigns each virtual user a **stable** pooled account for its
whole session (via `get_token_for(key)`), so one conversation never switches identity
mid-stream. The Accounts tab shows live token status (valid/expired) with accurate
countdowns, and lets you remove accounts.

### Load mode & shape

| Parameter | Applies to | Meaning |
|---|---|---|
| `mode` | all | `smoke`, `spike` (event spike), or `stepped` (stepped RPM). |
| `smoke_sessions` | smoke | How many sessions to fire **concurrently, once**, then stop. Default `3`. A quick end-to-end sanity check (auth/endpoint/prompts) before committing to a real load run. |
| `virtual_users` | spike | Target number of concurrent virtual users during the hold phase. Default `50`. |
| `rampup_seconds` | spike | Time to linearly ramp from 0 to `virtual_users`. Default `30`. |
| `hold_seconds` | spike | Time to hold at `virtual_users` after ramp-up. Default `120`. |
| `rampdown_seconds` | spike | Time to ramp back down to 0 at the end. Default `15`. |
| `sessions_per_vu` | spike | Max sessions each virtual user loops through before stopping; `0` = unlimited (keeps starting new sessions back-to-back until the phase ends). |
| `steps` | stepped | Comma-separated target RPM values to walk through in order, e.g. `20,40,60`. Used to find where throttling begins. |
| `step_hold_seconds` | stepped | How long to hold at each step's target RPM. Default `60`. |
| `max_concurrency` | stepped | Hard cap on concurrent in-flight sessions while hitting the target RPM. Default `100`. |

### Session behavior (all modes)

| Parameter | Meaning |
|---|---|
| `turns_per_session` | Number of conversation turns per session after the opening message. Default `4`. |
| `think_time_seconds` | `[min, max]` seconds to randomly wait between turns (simulates a real user reading/typing). Default `[5, 15]`. |
| `request_timeout` | Per-HTTP-request timeout in seconds passed to `D2EClient`. Default `120`. |
| `prompts` | One prompt per line in the form; one is chosen at random for each turn. |
| `stop_on_throttle` | Checkbox — if checked, the **entire run** stops the moment any single request comes back throttled (useful when you just want to find the exact onset point, not ride through it). |
| `label` | Free-text name for the run, shown in Run History (defaults to "Load test" if blank). |

---

## Reading the results

**Live Test tab**: KPIs (requests, success %, errors, throttled, active sessions,
**peak concurrent sessions**, achieved RPM, p50/p95 latency, live tool calls, live
knowledge-source hits, embedded downstream tool errors), throughput/latency charts,
grouping by status code and by request kind (start vs. turn), a live tool-invocation
table, an embedded tool/connector error feed, and a recent-events feed. A red warning
banner appears mid-run if any response is detected as a GHCP-harness rejection.

**Run History tab**: every run persisted with its config and final aggregates:
- **Mode**, **Target conc.** (the configured concurrency — reads the right field
  per mode: `smoke_sessions`/`max_concurrency`/`virtual_users`), **Peak conc.**
  (highest simultaneous active sessions actually observed — can diverge from target
  if throttling/errors prevented a full ramp), **Sessions** (distinct conversations),
  **Duration** (wall-clock, `created` → `ended`).
- **Harness (inferred)** — flags runs where the agent rejected the D2E endpoint
  outright (GHCP harness); red text + tooltip warns to treat those results as invalid.
- **Tools (live)** — best-effort count parsed from the SSE stream in real time.
- **Tools (transcript)** — authoritative count from Dataverse conversation
  transcripts, fetched on demand via **Enrich tools** (requires `org_url`).
- **Credits (MCS)** — real consumed/allocated Copilot Credit numbers from
  `pac licensing`, fetched on demand via **Refresh credits**. Consumption data
  aggregates on a periodic (roughly daily) snapshot, not in real time — the
  snapshot timestamp is shown so you can tell whether your test's traffic has
  been captured yet.
- A **trend chart** across all historical runs (p50/p95 latency, success rate,
  throttled count), optionally filtered by agent schema name.
- **Sessions** button opens the **Session Explorer**: pick any conversation from
  that run to see its full ordered transcript (prompt/response/latency/tool &
  knowledge counts/errors per turn).
- **Export** (per row) downloads that run's full per-turn event log as CSV.
  **Export all (CSV)** (top of the panel) downloads a one-row-per-run summary
  across all history, for offline analysis (Excel, Power BI, etc.) — both include
  every metric shown in the table plus a few not surfaced in the UI (achieved RPM,
  knowledge hits, tool error count).

Historical data older than a given feature (e.g. Peak conc. before it was added)
simply shows blank for that field — it isn't retroactively computable, but all new
runs populate it going forward.

## Known limitations / things to verify per-tenant

- **GHCP-harness agents cannot be tested via D2E at all** — see
  [GHCP harness support](#ghcp-harness-support-read-this-first) above. This tool
  detects and flags the rejection rather than silently reporting a false 100% success.
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
- The Flask dev server (`app.run(...)`) is explicitly **not production-grade** —
  this tool is single-user, single-machine, local-only by design.

## A2A / MCP exploration scripts (experimental)

Microsoft Copilot Studio recently (Oct 2026) began rolling out two new **preview**
channels specifically for GHCP-harness agents — **A2A client** (Agent2Agent
protocol) and **MCP client** (Model Context Protocol) — which would, if functional,
provide a real external invocation path for agents the D2E endpoint rejects. These
are gated to "early release cycle" Power Platform environments and are still being
rolled out; as of this writing neither is fully functional end-to-end in our test
tenant, but the auth/setup side is fully working and ready to retest at any time:

| Script | What it tests |
|---|---|
| `probe_a2a.py` | A2A channel via device-code auth — base `message/send` call + Agent Card discovery (`/.well-known/agent-card.json`) |
| `probe_a2a_authcode.py` | Same, but via a genuine browser-based OAuth 2.0 authorization-code flow (PKCE) — the grant type the official docs specifically require |
| `probe_a2a_cli.py` | Same target, using an existing Microsoft first-party public client (Azure CLI's) instead of our own app registration — tests whether a custom app registration is strictly required |
| `probe_a2a_sweep.py` | Protocol-variation sweep (alternate JSON-RPC method names, `api-version` guesses, `OPTIONS` probes) to rule out "calling it wrong" vs. "not provisioned yet" |
| `probe_mcp.py` | MCP channel via the real `initialize`/`tools/list`/`tools/call` JSON-RPC methods |

Current status (last verified 2026-10-22): A2A returns a bare infrastructure-level
`404 ResourceNotFound`; MCP returns a clean, specific `{"code":-32000,"message":
"The MCP endpoint is not enabled for this environment."}` — both with correctly-
scoped, verified tokens. Treat these as periodic-retest scripts, not broken code —
rerun any of them any time to check whether Microsoft's backend has caught up with
the channel's UI availability.

## Repository layout

```
app.py                  Flask server
engine.py                Load engine (smoke / spike / stepped modes)
stats.py                 Live stats aggregation (incl. peak concurrency)
d2e_client.py             D2E HTTP client (SSE/JSON parsing, tool + harness-rejection detection)
token_provider.py         Auth providers (static / device-code / ROPC / account pool)
run_store.py              SQLite persistence (runs, events, presets, trends, sessions)
credits.py                pac licensing credit/consumption queries
transcripts.py            Dataverse transcript enrichment (historical tool counts)
templates/index.html      Web dashboard
requirements.txt
config.example.json       Example config for the legacy CLI tools below
smoke_test.py / ramp.py  Original CLI tools (pre-UI); still usable standalone
probe_a2a*.py             A2A channel exploration (see above)
probe_mcp.py              MCP channel exploration (see above)
start_app.ps1/.bat        Desktop launcher (background start + auto-open browser)
stop_app.bat              Stops the background server
```

## License

TBD — private repo for now.
