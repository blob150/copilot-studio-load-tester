"""
Historical enrichment: count tool / connector / node executions per run by reading
Dataverse conversation transcripts, keyed off the conversationIds captured during the run.

Copilot Studio writes a `conversationtranscript` record per session whose `content`
column holds the full activity log (JSON). Tool/connector/agent-action invocations and
node executions appear there authoritatively (unlike the live SSE best-effort count).

Because transcripts can lag and require Dataverse access, this runs on demand when the
user "circles back" — the same moment they re-check credit consumption.

Auth: uses an Entra token for the environment's Dataverse (scope {org}/.default) via the
cached MSAL public client (same app registration). Falls back gracefully if unavailable.
"""
from __future__ import annotations

import json
import re

import msal
import requests

from token_provider import _load_cache

# tool/connector/action signals that may appear in transcript content JSON
_SIGNALS = {
    "tool": re.compile(r'"taskDialogId"\s*:\s*"P:([^"]+)"'),
    "connector": re.compile(r'"connectorId"\s*:\s*"([^"]+)"', re.I),
    "action": re.compile(r'"actionType"\s*:\s*"([^"]+)"', re.I),
    "knowledge": re.compile(r'"KnowledgeSource"'),
}


def _dataverse_token(org_url: str, tenant_id: str, client_id: str) -> str | None:
    cache = _load_cache()
    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache)
    accounts = app.get_accounts()
    if not accounts:
        return None
    scope = [org_url.rstrip("/") + "/.default"]
    res = app.acquire_token_silent(scope, account=accounts[0])
    return res.get("access_token") if res else None


def enrich_run(org_url: str, tenant_id: str, client_id: str,
               conversation_ids: list[str], max_convs: int = 200) -> dict:
    """Query transcripts for the given conversations and tally tool/connector signals."""
    out = {"status": "unavailable", "checked_convs": 0, "tool_calls": 0,
           "connector_calls": 0, "action_calls": 0, "knowledge_hits": 0,
           "tools": {}, "note": ""}
    token = _dataverse_token(org_url, tenant_id, client_id)
    if not token:
        out["note"] = ("No cached Dataverse token. Sign in (the app registration needs "
                       "delegated access to this environment's Dataverse) and retry.")
        return out

    api = org_url.rstrip("/") + "/api/data/v9.2"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "OData-MaxVersion": "4.0", "OData-Version": "4.0"}
    tools: dict[str, int] = {}
    checked = 0
    for conv in conversation_ids[:max_convs]:
        # transcripts are keyed by the bot conversation id inside 'content'
        url = (f"{api}/conversationtranscripts?$select=content&"
               f"$filter=contains(content,'{conv}')&$top=5")
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                if checked == 0:
                    out["note"] = f"Transcript query HTTP {r.status_code}: {r.text[:160]}"
                    out["status"] = "error"
                    return out
                continue
            for rec in r.json().get("value", []):
                content = rec.get("content") or ""
                for t in _SIGNALS["tool"].findall(content):
                    tools[t] = tools.get(t, 0) + 1
                    out["tool_calls"] += 1
                out["connector_calls"] += len(_SIGNALS["connector"].findall(content))
                out["action_calls"] += len(_SIGNALS["action"].findall(content))
                out["knowledge_hits"] += len(_SIGNALS["knowledge"].findall(content))
            checked += 1
        except requests.RequestException:
            continue
    out["tools"] = tools
    out["checked_convs"] = checked
    out["status"] = "ok" if checked else "no_transcripts_yet"
    if not checked and not out["note"]:
        out["note"] = "No transcripts found yet (they can lag). Re-check later."
    return out
