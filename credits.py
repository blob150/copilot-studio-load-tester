"""
Credit/consumption reconciliation via pac licensing.

Copilot Studio capacity consumption is exposed per-environment via
`pac licensing get-many-environment-entitlements`, which returns an array of
entitlement records; the Copilot Studio message/credit pool is the record whose
entitlementId == "MCSMessages" (productCategories includes "CopilotStudio").

That record carries BOTH allocated and consumed quantities, e.g.:
  entitlement.capacity.allocated.value   -> total allocated (e.g. 1000)
  entitlement.capacity.consumed.value    -> ACTUAL CONSUMED (e.g. 103.06)
  entitlement.capacity.consumed.lastUpdatedOn -> snapshot timestamp (periodic/daily
                                                 cadence; this is why consumption lags
                                                 -- it's a periodic snapshot, not live)
  entitlement.capacity.availableQuantity -> remaining
  entitlement.capacity.status            -> e.g. "WithinCapacity"

This queries that directly (instead of the allocation-only endpoint used earlier),
so "Refresh credits" can show real consumed/allocated numbers once Microsoft's
snapshot has caught up. The snapshot's lastUpdatedOn tells you exactly how stale the
number is -- if it predates your test run, it hasn't been captured yet.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time


def _pac(*args, timeout=60) -> tuple[int, str]:
    exe = shutil.which("pac")
    if not exe:
        return 1, "pac CLI not found on PATH"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "pac command timed out"


def _extract_json(text: str):
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                continue
    return None


def check_env_credits(environment_id: str) -> dict:
    """Real consumed/allocated snapshot for the MCSMessages (Copilot Studio) entitlement."""
    out = {
        "status": "unavailable",
        "checked_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "billing_policy_linked": None,
        "mcs_messages_allocated": None,
        "mcs_messages_consumed": None,
        "mcs_messages_available": None,
        "mcs_messages_status": None,
        "mcs_sessions_consumed": None,
        "mcs_sessions_allocated": None,
        "consumption_snapshot_iso": None,
        "enforcement": None,
        "note": "",
    }

    rc, text = _pac("licensing", "get-many-environment-entitlements",
                    "--environment", environment_id, "--json")
    data = _extract_json(text)
    mcs = None
    sessions = None
    if isinstance(data, list):
        for rec in data:
            # Exact entitlementId match only -- several unrelated entitlements also
            # carry productCategories including "CopilotStudio" (e.g. "AI"), so a
            # category-only match picks the wrong record.
            if rec.get("entitlementId") == "MCSMessages":
                mcs = rec.get("entitlement", {})
            elif rec.get("entitlementId") == "MCSSessions":
                sessions = rec.get("entitlement", {})
    if mcs:
        cap = mcs.get("capacity", {})
        consumed = cap.get("consumed", {})
        out["mcs_messages_allocated"] = cap.get("allocated", {}).get("value")
        out["mcs_messages_consumed"] = consumed.get("value")
        out["mcs_messages_available"] = cap.get("availableQuantity")
        out["mcs_messages_status"] = cap.get("status")
        out["consumption_snapshot_iso"] = consumed.get("lastUpdatedOn")
        out["enforcement"] = {r.get("ruleType"): r.get("enabled")
                              for r in cap.get("enforcementRules", [])}
        out["status"] = "ok"
        snap = out["consumption_snapshot_iso"] or ""
        out["note"] = (
            f"Consumption snapshot as of {snap}. This updates on a periodic "
            f"(roughly daily) cadence, not in real time -- if this timestamp predates "
            f"your test run, your traffic isn't reflected yet; re-check later."
        )
    else:
        out["note"] = ("Could not locate the MCSMessages entitlement for this "
                       "environment. Raw pac output shape may have changed.")
        out["status"] = "error" if rc != 0 else "not_found"

    if sessions:
        scap = sessions.get("capacity", {})
        out["mcs_sessions_allocated"] = scap.get("allocated", {}).get("value")
        out["mcs_sessions_consumed"] = scap.get("consumed", {}).get("value")

    rc2, text2 = _pac("licensing", "get-environment-billing-policy",
                      "--environment", environment_id)
    out["billing_policy_linked"] = not ("404" in text2 or "Not Found" in text2)
    return out
