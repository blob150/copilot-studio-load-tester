"""
Discover which connectors a Standard-harness (D2E) agent actually depends on,
DIRECTLY from Dataverse metadata, instead of empirically guessing it by
replaying prompts and watching for consent cards.

Why this exists: preflight's connector-consent check used to have to walk
every configured prompt (or, in scripted mode, every turn of every script)
per account just to find out which connectors exist at all -- for a large
imported test-case set (dozens of turns) that meant dozens of disposable
conversations per account, taking minutes. But the connector list is a
property of the AGENT, not of the test data or the account -- it doesn't
change per run. Looking it up once via Dataverse turns "walk everything and
hope you trigger every connector's card" into "we already know there are
exactly N connectors; stop as soon as all N are resolved."

How: Copilot Studio agents are stored in Dataverse as a `bot` record, with
child `botcomponent` records for every topic AND every "action" (a connector
invocation node, e.g. a Dataverse/SharePoint/weather tool). An action
component's `data` column is a YAML-ish document containing a
`connectionReference: <botSchema>.<connectorLogicalName>.<guid>` line --
confirmed empirically (this session) against a real agent ("Early Standard
Test", DIAx tenant): querying botcomponents with componenttype=9 (action)
cleanly returned its 2 real connectors (Microsoft Dataverse /
shared_commondataserviceforapps, MSN Weather / shared_msnweather), matching
its actual prompt content exactly. The FULL connectionReference string
(not just the middle "connector type" segment) is used as the matching
key/"logical_name" throughout this module -- also confirmed empirically
(against a real un-consented identity) that a LIVE consent card's
"_mcsConsentLogicalName" field returns that same full string verbatim, so
anything that compares discovered connectors against observed consent
grants (preflight.py's early-exit and run_store's permanent consent cache)
needs them in identical form.

Only relevant for D2E (Standard-harness) agents -- GHCP-harness/A2A agents
never showed a connector consent card in this tool's testing (see
preflight.py), so callers should skip this for protocol=="a2a" entirely.

Auth: reuses the exact same cached Dataverse token lookup as transcript
enrichment (transcripts._dataverse_token) -- same app registration, same
scope, no separate sign-in needed.
"""
from __future__ import annotations

import re

import requests

from transcripts import _dataverse_token

# componenttype 9 == "Action" in a Copilot Studio bot's botcomponent table
# (topics are componenttype 9 too in some schemas as "Dialogs" would be a
# different id -- the ACTUAL distinguishing signal confirmed empirically is
# the presence of an "action:\n  kind: InvokeConnectorTaskAction" block with
# a connectionReference in the data payload, which only non-topic action
# components contain. We filter on componenttype=9 first as a narrowing
# predicate -- matches the real schema seen in testing -- and then confirm
# via the connectionReference regex so any topic components etc. that also
# happen to use componenttype=9 are simply skipped (connectionReference
# regex finds nothing in them).
_ACTION_COMPONENT_TYPE = 9

_CONNECTION_REF_RE = re.compile(r'connectionReference:\s*(\S+)')


def discover_agent_connectors(org_url: str, tenant_id: str, client_id: str,
                               schema: str) -> dict:
    """Returns {"status": "ok"|"unavailable"|"error",
    "connectors": [{"logical_name", "display_name"}], "note": ""}.

    "unavailable" covers every case where discovery simply isn't possible
    right now (no org_url configured, no cached Dataverse sign-in, bot not
    found under that schema name in this environment, etc.) -- callers
    should treat this exactly like "no connectors known" and fall back to
    the old full-prompt-walk behavior, NOT treat it as a hard failure."""
    out = {"status": "unavailable", "connectors": [], "note": ""}
    if not org_url or not schema:
        out["note"] = "No Dataverse org URL configured for this connection -- skipping connector auto-discovery."
        return out

    token, note = _dataverse_token(org_url, tenant_id, client_id)
    if not token:
        out["note"] = note
        return out

    api = org_url.rstrip("/") + "/api/data/v9.2"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "OData-MaxVersion": "4.0", "OData-Version": "4.0"}

    # OData string literals escape a single quote by doubling it.
    safe_schema = schema.replace("'", "''")
    try:
        r = requests.get(
            f"{api}/bots?$select=botid,name&$filter=schemaname eq '{safe_schema}'",
            headers=headers, timeout=30)
    except requests.RequestException as e:
        out["status"] = "error"
        out["note"] = f"Could not reach Dataverse to look up the bot record: {e}"
        return out
    if r.status_code != 200:
        out["status"] = "error"
        out["note"] = f"Bot lookup HTTP {r.status_code}: {r.text[:200]}"
        return out
    bots = r.json().get("value", [])
    if not bots:
        out["note"] = (
            f"No bot record found with schema name '{schema}' in this Dataverse "
            "environment -- the configured org_url may not be the environment "
            "this agent actually lives in. Skipping connector auto-discovery.")
        return out
    botid = bots[0]["botid"]

    try:
        r = requests.get(
            f"{api}/botcomponents?$select=name,data&"
            f"$filter=_parentbotid_value eq {botid} and componenttype eq {_ACTION_COMPONENT_TYPE}&"
            f"$top=200",
            headers=headers, timeout=30)
    except requests.RequestException as e:
        out["status"] = "error"
        out["note"] = f"Could not reach Dataverse to list the agent's actions: {e}"
        return out
    if r.status_code != 200:
        if r.status_code == 403 and "prv" in r.text and "privilege" in r.text.lower():
            out["note"] = (
                "Dataverse rejected the botcomponent query with a MISSING PRIVILEGE "
                "error -- the signed-in account's security role doesn't grant Read on "
                "the Bot Component table. Skipping connector auto-discovery (an "
                "environment admin can grant this; see Power Platform admin center -> "
                "Environment -> Security roles -> Read on 'Bot Component').")
        else:
            out["status"] = "error"
            out["note"] = f"Action-component query HTTP {r.status_code}: {r.text[:200]}"
        return out

    connectors: dict[str, str] = {}  # logical_name -> display_name
    for comp in r.json().get("value", []):
        data = comp.get("data") or ""
        m = _CONNECTION_REF_RE.search(data)
        if not m:
            continue  # an action component with no connector (rare) -- skip
        # IMPORTANT: use the FULL connectionReference string as the matching
        # key, not just the parsed-out middle segment. Confirmed empirically
        # (this session, against a real un-consented identity) that a LIVE
        # consent card's "_mcsConsentLogicalName" field returns the full
        # "<botSchema>.<connectorName>.<guid>" string verbatim -- NOT the
        # short connector-type name alone -- so this must match that exactly
        # or the subset-comparison/early-exit/cache logic in preflight.py
        # can never recognize a real consent as satisfying a known connector.
        logical_name = m.group(1)
        if not logical_name:
            continue
        if logical_name not in connectors:
            # The action's own display name (e.g. "MSN Weather - Get current
            # weather") is friendlier than the raw logical name, but the
            # operation-specific suffix after " - " is noise for a connector
            # identity label -- keep just the connector-ish prefix when that
            # separator is present.
            name = (comp.get("name") or logical_name).split(" - ")[0].strip()
            connectors[logical_name] = name or logical_name

    out["status"] = "ok"
    out["connectors"] = [{"logical_name": k, "display_name": v} for k, v in sorted(connectors.items())]
    if not out["connectors"]:
        out["note"] = (
            "Found the bot record but no connector-backed actions in it -- this "
            "agent may not use any external connectors, or may not expose them in "
            "a way this check recognizes.")
    return out
