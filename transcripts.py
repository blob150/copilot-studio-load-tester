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
import time

import msal
import requests

from token_provider import _load_cache, _cache_path_for

# tool/connector/action signals that may appear in transcript content JSON
_SIGNALS = {
    # Legacy classic-Topics taskDialogId marker (values like "P:<pluginId>").
    # CONFIRMED STALE for the modern "Dynamic Planning" / Generative
    # Orchestration authoring experience: a real Standard-harness transcript
    # (agent "Laptop Info Finder", which DID invoke 3 real tools, confirmed via
    # Application Insights) matched this pattern ZERO times -- its taskDialogId
    # values never start with "P:" at all (see _extract_dynamic_plan_tool_calls
    # below for that schema's actual marker). Kept only for whatever
    # older/classic-Topics agents might still produce this shape; do not treat
    # "0 matches" here as "this harness made no tool calls".
    "tool": re.compile(r'"taskDialogId"\s*:\s*"P:([^"]+)"'),
    "connector": re.compile(r'"connectorId"\s*:\s*"([^"]+)"', re.I),
    "action": re.compile(r'"actionType"\s*:\s*"([^"]+)"', re.I),
    "knowledge": re.compile(r'"KnowledgeSource"'),
}

# actionType values that are internal dialog/control-flow plumbing (message
# sending, variable assignment, branching, telemetry, dialog-stack control),
# NOT a real external tool/connector/action invocation. Confirmed present
# (SendActivity/SetVariable/ConditionGroup/ConditionItem/
# LogCustomTelemetryEvent/CancelAllDialogs) in a real transcript that had only
# 3 genuine tool calls (per Application Insights) -- every actionType match in
# that transcript was this internal noise, none were real. This denylist is
# NOT guaranteed exhaustive (other internal action types may exist that
# haven't been observed yet), so "action_calls" can still include undiscovered
# noise -- treat it as a weak/unreliable signal even after this filter.
_INTERNAL_DIALOG_ACTION_TYPES = {
    "SendActivity", "SetVariable", "ConditionGroup", "ConditionItem",
    "LogCustomTelemetryEvent", "CancelAllDialogs",
}

# GHCP-harness (A2A) agents report tool/MCP invocations through a different
# triple instead of taskDialogId:
#   "toolCallId":"...", "toolName":"read_query", "toolCallStatus":"Started"|"Completed"
# Each real invocation emits TWO of these records (Started, then Completed)
# sharing the same toolCallId, and each one also happens to carry a
# "connectorId" field (since the tool is bridged through an MCP-over-connector
# call) -- which is why a GHCP-harness run with real tool calls previously
# showed up as "tool_calls: 0" (the taskDialogId regex never matches this
# harness) while "connector_calls" silently absorbed ~2x the real tool-call
# count instead. Dedupe by toolCallId so each real invocation is counted once.
#
# NOTE: this marker was originally assumed to be GHCP-exclusive, but that was
# never directly confirmed against a real Standard-harness Dataverse
# transcript (only inferred from the Standard-harness Application Insights
# spans, which DO use toolCallId-shaped fields for MCP tool calls -- see
# app_insights.py). Treat "detects this pattern" as "found a toolCallId-style
# tool call", not as proof of which harness produced it.
_MCP_TOOL_CALL_ID_RE = re.compile(r'"toolCallId"\s*:\s*"([^"]+)"')
_MCP_TOOL_NAME_RE = re.compile(r'"toolName"\s*:\s*"([^"]+)"')


def _extract_mcp_tool_calls(content: str) -> dict[str, int]:
    """Return {toolName: count} for GHCP-harness tool calls in one transcript,
    counting each unique toolCallId once regardless of how many Started/
    Completed/etc. status events it produced."""
    seen: dict[str, str] = {}
    for m in _MCP_TOOL_CALL_ID_RE.finditer(content):
        call_id = m.group(1)
        if call_id in seen:
            continue
        # toolName appears alongside toolCallId within the same compact JSON
        # object; a short lookahead window is enough without needing a full
        # JSON parse of the (often huge) transcript.
        name_m = _MCP_TOOL_NAME_RE.search(content, m.end(), m.end() + 300)
        if name_m:
            seen[call_id] = name_m.group(1)
    tools: dict[str, int] = {}
    for name in seen.values():
        tools[name] = tools.get(name, 0) + 1
    return tools


# Modern "Dynamic Planning" / Generative Orchestration transcript schema -- a
# THIRD, distinct tool-call marker, separate from both the legacy "P:"-prefixed
# taskDialogId above and GHCP's toolCallId/toolName/toolCallStatus triple.
# CONFIRMED via a real Standard-harness ("Laptop Info Finder") transcript that
# matched NEITHER existing pattern at all despite genuinely invoking 3 real
# tools -- this authoring generation instead emits a DynamicPlanStepTriggered /
# DynamicPlanStepBindUpdate / DynamicPlanStepFinished activity sequence per
# step, all sharing one stepId. The real tool name and success/failure live on
# the "Finished" event:
#   - taskDialogId "MCP:<agent>.action.<mcpSchema>:<toolName>"  -> an MCP tool
#     call; the real tool name is the LAST ':'-separated segment.
#   - taskDialogId "<agent>.action.<ActionName>"                -> a direct
#     connector/Power-Automate-flow action; use the segment after the last '.'
#     as a readable name.
#   - value.observation.isError (true/false) marks success/failure -- NOT an
#     HTTP-style status/ResultCode field like Application Insights uses.
# Parses the content as JSON (confirmed valid JSON in every real sample seen
# this session) rather than regex-scanning, since correctly pairing step
# triggers with their outcomes and branching on taskDialogId shape would be
# much more failure-prone as ad-hoc regexes. Falls back to "found nothing"
# rather than raising if content ever isn't valid/expected JSON.
def _extract_dynamic_plan_tool_calls(content: str) -> dict[str, int]:
    """Return {toolName: count} for Dynamic-Planning-schema tool calls in one
    transcript, counting one call per DynamicPlanStepFinished event."""
    tools: dict[str, int] = {}
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return tools
    activities = data.get("activities") if isinstance(data, dict) else None
    if not isinstance(activities, list):
        return tools
    for a in activities:
        if not isinstance(a, dict) or a.get("name") != "DynamicPlanStepFinished":
            continue
        value = a.get("value") or {}
        task_id = value.get("taskDialogId") or ""
        if not task_id:
            continue
        name = task_id.rsplit(":", 1)[-1] if task_id.startswith("MCP:") else task_id.rsplit(".", 1)[-1]
        tools[name] = tools.get(name, 0) + 1
    return tools

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
               conversation_ids: list[str], max_convs: int = 200,
               run_started_epoch: float | None = None) -> dict:
    """Query transcripts for the given conversations and tally tool/connector signals.

    run_started_epoch (optional): the run's start time (time.time()-style epoch
    seconds). When given, a not-yet-synced result reports how much longer to
    expect to wait, based on the empirically observed ~30-35 minute lag between
    a conversation's start and its transcript record landing in Dataverse for
    this environment (NOT the vague "periodic/daily" lag that applies to the
    separate credit-consumption snapshot)."""
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
    mcp_tool_call_total = 0
    dynamic_plan_tool_call_total = 0
    records_found = 0
    for conv in conversation_ids[:max_convs]:
        # transcripts are keyed by the bot conversation id inside 'content'
        url = (f"{api}/conversationtranscripts?$select=content&"
               f"$filter=contains(content,'{conv}')&$top=5")
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                if checked == 0:
                    # 403 + "missing prv...privilege" is a DIFFERENT failure mode
                    # than the sign-in/consent issues _dataverse_token() already
                    # distinguishes -- the user IS signed in and the app DOES
                    # have Dataverse access, but their Dataverse SECURITY ROLE
                    # lacks (or lost) Read privilege on the conversationtranscript
                    # table specifically. Give an actionable, specific message
                    # instead of a generic "HTTP 403: <raw body>" dump, since
                    # this requires a completely different fix (an admin
                    # granting a security-role privilege) than a re-sign-in or
                    # app-registration consent grant would.
                    if r.status_code == 403 and "prv" in r.text and "privilege" in r.text.lower():
                        out["note"] = (
                            "Dataverse rejected this with a MISSING PRIVILEGE error (not a "
                            "sign-in/consent issue) -- the signed-in account's Dataverse "
                            "security role doesn't currently grant Read on the "
                            "conversationtranscript table. An environment admin needs to "
                            "grant that (Power Platform admin center -> Environment -> "
                            "Security roles -> Read on 'Conversation Transcript', at "
                            f"Business Unit or Organization scope). Raw error: {r.text[:300]}"
                        )
                    else:
                        out["note"] = f"Transcript query HTTP {r.status_code}: {r.text[:160]}"
                    out["status"] = "error"
                    return out
                continue
            recs = r.json().get("value", [])
            records_found += len(recs)
            for rec in recs:
                content = rec.get("content") or ""
                for t in _SIGNALS["tool"].findall(content):
                    tools[t] = tools.get(t, 0) + 1
                    out["tool_calls"] += 1
                # GHCP-harness agents (A2A) never match the taskDialogId
                # signal above -- their tool calls show up here instead,
                # deduped by toolCallId so Started+Completed pairs don't
                # double-count a single invocation.
                for name, count in _extract_mcp_tool_calls(content).items():
                    tools[name] = tools.get(name, 0) + count
                    out["tool_calls"] += count
                    mcp_tool_call_total += count
                # Modern "Dynamic Planning" / Generative Orchestration agents
                # match NEITHER signal above -- see
                # _extract_dynamic_plan_tool_calls for why this is a required
                # third detector, not an optional extra.
                for name, count in _extract_dynamic_plan_tool_calls(content).items():
                    tools[name] = tools.get(name, 0) + count
                    out["tool_calls"] += count
                    dynamic_plan_tool_call_total += count
                out["connector_calls"] += len(_SIGNALS["connector"].findall(content))
                # Exclude known internal dialog/control-flow actionType noise
                # (message sends, variable assignment, branching, etc.) --
                # confirmed these are NOT real tool/connector actions (see
                # _INTERNAL_DIALOG_ACTION_TYPES). Not guaranteed to catch every
                # possible noise value, so this remains a weak signal even
                # after filtering.
                out["action_calls"] += sum(
                    1 for m in _SIGNALS["action"].findall(content)
                    if m not in _INTERNAL_DIALOG_ACTION_TYPES)
                out["knowledge_hits"] += len(_SIGNALS["knowledge"].findall(content))
            checked += 1
        except requests.RequestException:
            continue
    out["tools"] = tools
    out["checked_convs"] = checked
    # `checked` only means "the query succeeded for N conversations" -- it says
    # nothing about whether Copilot Studio had actually synced a transcript
    # record to Dataverse for any of them yet. Reporting "ok" in that case was
    # indistinguishable from a genuine zero-tool-calls result, so track
    # records_found separately and flag the lag case explicitly.
    if not checked:
        out["status"] = "no_transcripts_yet"
        if not out["note"]:
            out["note"] = "No transcripts found yet (they can lag). Re-check later."
    elif not records_found:
        out["status"] = "no_transcripts_yet"
        lag_note = (
            f"Queried {checked} conversation(s) successfully, but Dataverse has no "
            f"conversationtranscript record for any of them yet."
        )
        if run_started_epoch is not None:
            elapsed_min = (time.time() - run_started_epoch) / 60
            typical_lag_min = 35  # observed range ~30-34 min; pad slightly
            remaining_min = typical_lag_min - elapsed_min
            if remaining_min > 0:
                lag_note += (
                    f" This environment's transcripts typically land ~30-35 min after a "
                    f"conversation starts; it's been {elapsed_min:.0f} min so far -- try "
                    f"again in about {remaining_min:.0f} more minute(s)."
                )
            else:
                lag_note += (
                    f" It's been {elapsed_min:.0f} min, longer than this environment's "
                    f"usual ~30-35 min sync lag -- re-check again shortly; if it's still "
                    f"empty after ~45-60 min total, the sync job may be stuck."
                )
        else:
            lag_note += (
                " Copilot Studio can take several minutes (sometimes longer) to sync "
                "transcripts after a run ends -- re-check later rather than treating this "
                "as a true zero-tool-calls result."
            )
        out["note"] = lag_note
    else:
        out["status"] = "ok"
        detail_parts = []
        if mcp_tool_call_total:
            detail_parts.append(
                f"{mcp_tool_call_total} via toolCallId/toolName/toolCallStatus markers (deduped)")
        if dynamic_plan_tool_call_total:
            detail_parts.append(
                f"{dynamic_plan_tool_call_total} via Dynamic-Planning taskDialogId markers "
                f"(DynamicPlanStepFinished events)")
        if detail_parts:
            # RESOLVED (previously open question): is toolCallId/toolName
            # exclusive to the GHCP harness within DATAVERSE TRANSCRIPTS
            # specifically (a separate question from whether it appears in
            # Application Insights spans, where Standard harness's MCP tool
            # calls DO use toolCallId-shaped OTel fields -- see
            # app_insights.py). Checked one real Standard-harness ("Laptop
            # Info Finder") Dataverse transcript that genuinely invoked 3
            # tools: it matched the toolCallId pattern ZERO times -- its
            # transcript instead used the Dynamic-Planning taskDialogId
            # schema exclusively (handled above as its own detector). So
            # within Dataverse transcripts, toolCallId does appear to be
            # GHCP-specific after all; this is based on one verified sample,
            # not an exhaustive survey, so still treat as the best evidence
            # available rather than an absolute guarantee.
            out["note"] = (
                "Detected tool calls: "
                + "; ".join(detail_parts) + ". connector_calls still counts raw "
                "\"connectorId\" field occurrences (includes ~2 per toolCallId-style call "
                "for Started+Completed) -- it is not an independent signal. The toolCallId "
                "pattern has, in the one Standard-harness sample checked so far, only ever "
                "appeared in GHCP-harness transcripts -- Standard harness instead used the "
                "Dynamic-Planning taskDialogId schema exclusively."
            )
    return out
