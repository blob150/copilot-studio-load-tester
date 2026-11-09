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
  - **"Allow public client flows"** = Yes — this is the important part. A
    PUBLIC client (no secret) is both the simpler AND the more secure setup for
    a locally-run tool: nothing to store/rotate on disk, every virtual user
    signs in as themselves (MFA-compliant), and access is revocable per-user
    instead of via one shared static credential. Only fall back to a
    confidential/secret-based app registration (see "A2A/MCP via a
    confidential client" below) if you must use a pre-existing registration
    you don't control that was already configured that way.
  - **API permissions** → **Delegated**, admin-consented:
    - Power Platform API → `CopilotStudio.Copilots.Invoke` (for D2E)
    - Microsoft Copilot Studio Agent MCP and A2A channels
      (`6f46e6ed-07bf-4616-80f0-47546850f654`) → `MCS.InvokeAsA2A` and/or
      `MCS.InvokeAsMCP` (for the A2A/MCP protocols)
  - **One registration covers all three protocols.** You don't need separate
    app registrations per protocol — add whichever scopes you plan to use to
    the same registration, and set `"protocol"` in your config
    (`"d2e"` / `"a2a"` / `"mcp"`) to pick which scope a given run authenticates
    for. The tool's token cache is shared per tenant+client, so signing in
    once lets you switch protocols without re-authenticating (as long as the
    scope was admin-consented up front).
- (Optional, for credit/billing queries) [Power Platform CLI (`pac`)](https://aka.ms/PowerPlatformCLI)
  installed and authenticated to the target tenant (`pac auth create --tenant <tenantId>`).
- (Optional, for historical tool/connector enrichment) Dataverse access to the
  agent's environment (uses the same cached token).

### Creating the app registration (one-time, per tenant)

Using Azure CLI (adjust the tenant; requires rights to create app
registrations and grant admin consent in that tenant — if you don't have
those rights, ask a tenant admin to run this once):

```powershell
az login --tenant <yourTenantId> --use-device-code

# Power Platform API (D2E) + Copilot Studio A2A/MCP channels (both first-party,
# multi-tenant resource apps -- same app IDs in every tenant)
$ppapi  = "8578e004-a5c6-46e7-913e-12f58912df43"
$a2amcp = "6f46e6ed-07bf-4616-80f0-47546850f654"

$d2eScope = (az ad sp show --id $ppapi  --query "oauth2PermissionScopes[?value=='CopilotStudio.Copilots.Invoke'].id" -o tsv)
$a2aScope = (az ad sp show --id $a2amcp --query "oauth2PermissionScopes[?value=='MCS.InvokeAsA2A'].id" -o tsv)
$mcpScope = (az ad sp show --id $a2amcp --query "oauth2PermissionScopes[?value=='MCS.InvokeAsMCP'].id" -o tsv)

@"
[
  { "resourceAppId": "$ppapi",  "resourceAccess": [ { "id": "$d2eScope", "type": "Scope" } ] },
  { "resourceAppId": "$a2amcp", "resourceAccess": [
      { "id": "$a2aScope", "type": "Scope" },
      { "id": "$mcpScope", "type": "Scope" }
  ] }
]
"@ | Out-File rra.json -Encoding utf8

$app = az ad app create --display-name "Copilot Studio Load Tester" `
  --sign-in-audience AzureADMyOrg --is-fallback-public-client true `
  --public-client-redirect-uris "http://localhost" "https://login.microsoftonline.com/common/oauth2/nativeclient" `
  --required-resource-accesses "@rra.json" -o json | ConvertFrom-Json

az ad sp create --id $app.appId
az ad app permission admin-consent --id $app.appId   # requires admin rights; grants tenant-wide, one time

"Client ID: $($app.appId)"
```

> If your tenant blocks the Azure CLI's first-party app via Conditional Access,
> try `Connect-MgGraph` (Microsoft Graph PowerShell) instead, or create the app
> registration manually in the Entra admin center (App registrations → New
> registration → Authentication → add the Mobile/desktop platform, add
> `http://localhost` as a redirect URI, and enable public client flows → API
> permissions → add the delegated scopes above → grant admin consent).

#### A2A/MCP via a confidential client (fallback, not recommended)

If you only have access to a **pre-existing** app registration that's
confidential (client secret required, "Allow public client flows" = No) and
can't create/modify a public-client registration in that tenant, the tool
still supports it: set `auth_mode` to `a2a_confidential` or
`mcp_confidential` and provide `client_secret` in the UI/config. This uses a
manual OAuth2 authorization-code flow (`ConfidentialAuthCodeProvider` in
`token_provider.py`) instead of device-code. Treat the secret as sensitive —
it is never written to saved presets (stripped server-side), but you are
responsible for not committing it to `config.json`.

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
| `protocol` | `d2e` (default, Direct-to-Engine/Bot Framework activity protocol) or `a2a` (preview A2A streaming protocol — see "A2A / MCP exploration scripts" below for how this was discovered/validated). Determines which client (`D2EClient`/`A2AClient`) the engine builds and which delegated scope auth resolves to. |
| `url` | The full D2E conversation URL pasted into the form; parsed client-side to auto-fill `host`/`schema` (not sent to the engine directly). Only used when `protocol=d2e`. |
| `host` | The D2E endpoint hostname, e.g. `{env}.{region}.environment.api.powerplatform.com`. |
| `schema` | The agent's Dataverse bot schema name (the `{schemaName}` segment of the D2E URL). |
| `api_version` | D2E API version query param. Defaults to `2022-03-01-preview` in `D2EClient`; not currently a form field (edit the URL to change it). |
| `a2a_url` | The A2A agent's base URL (or its `.well-known/agent-card.json` discovery URL — either works, auto-derived from the `url` field when `protocol=a2a`). Shape: `https://api.powerplatform.com/copilotstudio-a2a/tenants/{tenantId}/environments/{environmentId}/agents/{agentName}`. |
| `environment_id` | The Dataverse environment GUID — used for Copilot Credit lookups (`pac licensing`), unrelated to the conversation call itself. **Auto-filled** when you paste a URL: for D2E, the host subdomain IS the environment GUID with dashes removed (just split across two DNS labels), so it's reconstructed automatically; for A2A, it's parsed directly from the URL path. Only fills a blank field — never overwrites something you typed manually. |
| `org_url` | The environment's Dataverse org URL (e.g. `https://orgxxxx.crm.dynamics.com`) — only used by the **Enrich tools** historical transcript lookup in Run History. |

### Authentication

| Parameter | Meaning |
|---|---|
| `auth_mode` | `static` (paste a bearer token directly — fastest smoke test), `devicecode` (interactive MFA-compliant sign-in, single cached account), `pool` (Account Pool — see below), `a2a_confidential`/`mcp_confidential` (manual browser auth-code flow against a pre-existing CONFIDENTIAL/secret-required app registration — fallback only, see "Creating the app registration" above). |
| `tenant_id` | Entra tenant GUID containing the agent and the app registration. |
| `client_id` | The app registration's Application (client) ID. One registration works for all protocols — see "Creating the app registration". |
| `client_secret` | Only used when `auth_mode` is `a2a_confidential`/`mcp_confidential`. Never persisted to saved presets or Auth profiles (stripped server-side). |
| `static_token` | Only used when `auth_mode=static`: a bearer token pasted directly (e.g. from `pac auth` or browser devtools). Expires quickly — mainly for one-off smoke checks. |

**How the four auth controls fit together** (Live Test tab, numbered steps 1–4):
1. **Auth profile** — a named shortcut for *which* tenant/app registration to
   use. Loading one just fills in Tenant ID/Client ID/Auth mode below; it
   does **not** sign you in by itself.
2. **Tenant ID / Client ID** — the actual identity values the run uses,
   whether typed manually or filled by a profile.
3. **Auth mode** — *how* you'll sign in (device code / account pool /
   static token / confidential client).
4. **Sign in** — the actual interactive step, specific to the mode chosen:
   `devicecode`/`a2a_confidential` show an **"Authenticate now"** button
   here (click it, complete the browser prompt); `pool` shows the Account
   Pool wizard instead; `static` skips this step entirely (nothing to sign
   into — you already have a token).
   A multi-session run under `devicecode`/`a2a_confidential` will be
   **rejected immediately** if step 4 hasn't been completed yet (rather than
   hanging) — see "Known limitations" below for why.

**Auth profiles**: save/load/delete named `(tenant_id, client_id, auth_mode)`
combos via **Save profile** / the dropdown + **Load** / **Delete** in step 1 —
independent of load-test presets, so switching which tenant you're pointed
at doesn't mean retyping GUIDs or hunting through unrelated saved configs.
Never stores a secret.

**Account Pool**: click **"+ Add account"** repeatedly (Live Test tab or Accounts
tab) to sign in N distinct accounts via N separate device-code flows — each one gets
its own large, copy-friendly code callout. All pooled accounts share one persisted
MSAL cache; `engine.py` assigns each virtual user a **stable** pooled account for its
whole session (via `get_token_for(key)`), so one conversation never switches identity
mid-stream. The Accounts tab shows live token status (valid/expired) with accurate
countdowns, and lets you remove accounts. The pool resolves its scope from `protocol`
(same app registration, same cached accounts — switching `protocol` between runs
re-uses the signed-in accounts and silently acquires a token for the new scope,
no re-sign-in needed, as long as that scope was admin-consented up front).
The Accounts tab shows accounts across **every tenant you have a saved Auth
profile for** in one combined table (with a Tenant/Profile column) — not
just whichever tenant happens to be in the form right now.

### Saved configurations (presets)

The dropdown at the top of the Live Test tab saves/loads/deletes full
load-test configs (everything on the form). Two distinct actions avoid
accidentally creating duplicates:
- **Save as new** — always creates a new entry; requires a name.
- **Update selected** — overwrites whichever preset is currently selected
  in the dropdown with the form's current settings, keeping its existing
  name and id (no retyping the name, no duplicate created).

### Prompt sets

Click **🗂️ Manage sets** next to the Prompts box to open a small popup for
building a library of reusable, named question lists (e.g. "SOP questions",
"weather smoke test") independent of load-test presets. Save/update/delete
sets, or click **Apply to current test** to copy a set straight into the
main Prompts box and close the popup.

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
- **`devicecode`/`a2a_confidential` auth modes require a completed sign-in
  (step 4) before starting any run with more than one session.** All virtual
  user threads share one provider instance, and it holds its internal lock
  for the entire interactive sign-in flow — so without a cached token
  already in place, the first thread would block waiting for you to sign
  in (with every other thread blocking behind it) and the run would look
  permanently "stuck" with no visible prompt. `api_start` now checks for a
  cached token upfront and rejects immediately with a clear message instead.
  Use **Account Pool** mode for multi-account/multi-session runs, or
  complete step 4 first for `devicecode`/confidential modes.

## A2A streaming protocol (preview)

Microsoft Copilot Studio added a **preview** A2A (Agent2Agent protocol)
channel, giving a real external invocation path for agents even where it
isn't needed for D2E — and, notably, for GHCP-harness agents that D2E
rejects outright. As of this writing it's a staged rollout: some
environments return `x-servicefabric: ResourceNotFound` (not provisioned
yet) while others work end-to-end (`x-servicefabric: NoRetry`) — there is no
way to tell from the Studio UI alone; the Agent Card discovery call
(`GET {a2a_url}/.well-known/agent-card.json`, unauthenticated) is the
fastest way to check which bucket a given environment is in.

**This is now a first-class engine mode, not just exploration scripts** —
set `protocol: "a2a"` in the dashboard/config and it runs through the same
`LoadEngine`/`StatsAggregator`/Run History pipeline as D2E. See `a2a_client.py`
for the real wire format (proto-JSON transcoded, PascalCase method names
like `SendStreamingMessage` — **not** the generic A2A spec's lowercase
`message/send`, which this endpoint rejects) and the error-handling matrix
(soft failures like an empty-text completion, and the fact that a garbage
`contextId` is silently accepted as a new conversation rather than erroring).

| Script | What it tests |
|---|---|
| `probe_a2a.py` / `probe_a2a_authcode.py` / `probe_a2a_cli.py` / `probe_a2a_sweep.py` | Earlier exploration against our own (then-unprovisioned) tenant — kept for reference; superseded by the scripts below once a working environment was found. |
| `probe_mcp.py` | MCP channel exploration (`initialize`/`tools/list`/`tools/call`) against our own tenant. |
| `probe_a2a_pfecrm.py` / `probe_a2a_pfecrm_sdk.py` | First successful end-to-end A2A conversation (confidential-client flow, then re-validated via Microsoft's official `a2a-sdk`) — this is where the real wire format above was discovered. |
| `probe_a2a_errors_v2.py` | The error-scenario matrix referenced in `a2a_client.py`'s docstring (invalid token, malformed params, empty text, garbage contextId, etc.). |
| `probe_mcp_pfecrm.py` | Full working MCP conversation + error scenarios, for comparing A2A vs. MCP error ergonomics (A2A recommended as primary — see `plan.md`/commit history for the full write-up). |
| `probe_pfecrm_public_client.py` | Validates the proper no-secret public-client app registration pattern (see "Creating the app registration" above) actually works for A2A/MCP, not just D2E. |

`ms_sample/` contains Microsoft's official reference client code
(`a2a_client.py`, `mcp_client.py`, etc., from `microsoft/CopilotStudioSamples`)
— useful as a ground-truth reference if a new environment behaves
unexpectedly again.

## Repository layout

```
app.py                  Flask server
engine.py                Load engine (smoke / spike / stepped modes; d2e or a2a protocol)
stats.py                 Live stats aggregation (incl. peak concurrency)
d2e_client.py             D2E HTTP client (SSE/JSON parsing, tool + harness-rejection detection)
a2a_client.py             A2A HTTP client (SSE streaming, same TurnResult contract as D2E)
token_provider.py         Auth providers (static / device-code / ROPC / account pool / confidential auth-code)
run_store.py              SQLite persistence (runs, events, presets, trends, sessions)
credits.py                pac licensing credit/consumption queries
transcripts.py            Dataverse transcript enrichment (historical tool counts)
templates/index.html      Web dashboard
requirements.txt
config.example.json       Example config (D2E)
config.a2a.example.json   Example config (A2A)
smoke_test.py / ramp.py  Original CLI tools (pre-UI); still usable standalone
probe_a2a*.py             A2A channel exploration/validation (see above)
probe_mcp*.py             MCP channel exploration/validation (see above)
ms_sample/                Microsoft's official A2A/MCP reference client code
start_app.ps1/.bat        Desktop launcher (background start + auto-open browser)
stop_app.bat              Stops the background server
```

## License

TBD — private repo for now.

