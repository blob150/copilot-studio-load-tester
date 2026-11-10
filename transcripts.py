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

from token_provider import _load_cache, _cache_path_for

# tool/connector/action signals that may appear in transcript content JSON
_SIGNALS = {
    "tool": re.compile(r'"taskDialogId"\s*:\s*"P:([^"]+)"'),
    "connector": re.compile(r'"connectorId"\s*:\s*"([^"]+)"', re.I),
    "action": re.compile(r'"actionType"\s*:\s*"([^"]+)"', re.I),
    "knowledge": re.compile(r'"KnowledgeSource"'),
}

# The same cache-suffix conventions token_provider.py's providers actually write to
# (see AccountPoolManager/DeviceCodeProvider/ConfidentialAuthCodeProvider). This
# function previously only ever checked the single legacy shared `.token_cache.bin`
# file (via _load_cache() with no path), which is NOT where any of those providers
# actually persist tokens -- so "No cached Dataverse token" fired unconditionally
# for every tenant/client, even ones with perfectly valid, actively-used sign-ins.
_CACHE_SUFFIXES = ["pool", "devicecode", "confidential"]


def _dataverse_token(org_url: str, tenant_id: str, client_id: str) -> tuple[str | None, str]:
    """Returns (access_token or None, diagnostic note). Tries every cache file this
    tenant/client combo could plausibly have signed in through, across every account
    found in any of them, and reports the MOST SPECIFIC failure reason encountered
    (e.g. a real AADSTS consent error) rather than a generic catch-all."""
    scope = [org_url.rstrip("/") + "/.default"]
    seen_accounts = False
    last_error = None
    for suffix in _CACHE_SUFFIXES:
        path = _cache_path_for(tenant_id, client_id, suffix)
        if not path.exists():
            continue
        cache = _load_cache(path)
        app = msal.PublicClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=cache)
        for account in app.get_accounts():
            seen_accounts = True
            # acquire_token_silent() swallows the real failure reason and just
            # returns None on ANY error -- acquire_token_silent_with_error()
            # returns the actual {"error": ..., "error_description": ...} dict
            # (e.g. AADSTS65001/consent_required), which is the only way to
            # tell "needs one-time consent for this resource" apart from any
            # other silent-auth failure.
            res = app.acquire_token_silent_with_error(scope, account=account)
            if res and res.get("access_token"):
                return res["access_token"], ""
            if res and res.get("error"):
                # Keep the most informative one seen (AADSTS65001/consent_required
                # is the common real-world case: the app registration has never
                # been granted delegated access to THIS resource, which is a
                # one-time admin/user consent action, not a sign-in problem).
                last_error = f"{res.get('error')}: {res.get('error_description', '')[:300]}"
    if not seen_accounts:
        return None, ("No cached sign-in found for this tenant/app registration in any "
                       "of this tool's token caches. Sign in via the Live Test or Accounts "
                       "tab first, then retry.")
    if last_error:
        return None, (f"Found a cached sign-in, but Dataverse access was refused: {last_error}. "
                       "This usually means the app registration has never been granted "
                       "delegated access to THIS Dataverse environment specifically -- "
                       "a one-time consent grant, separate from your Copilot Studio sign-in.")
    return None, ("Found a cached sign-in, but silently acquiring a Dataverse token failed "
                  "for an unknown reason. Try re-authenticating and retry.")


def enrich_run(org_url: str, tenant_id: str, client_id: str,
               conversation_ids: list[str], max_convs: int = 200) -> dict:
    """Query transcripts for the given conversations and tally tool/connector signals."""
    out = {"status": "unavailable", "checked_convs": 0, "tool_calls": 0,
           "connector_calls": 0, "action_calls": 0, "knowledge_hits": 0,
           "tools": {}, "note": ""}
    token, note = _dataverse_token(org_url, tenant_id, client_id)
    if not token:
        out["note"] = note
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
