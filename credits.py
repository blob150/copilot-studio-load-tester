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
import re
import shutil
import subprocess
import time

from paths import DATA_DIR


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


# ---------------------------------------------------------------------------
# `pac`'s default auth profile is a single machine-wide setting, not scoped per
# environment/tenant, and tests here routinely run against environments that
# live in a *different* tenant than whatever profile happened to be selected
# last (e.g. interactively, or by an unrelated test). A silently-wrong default
# profile makes `get-many-environment-entitlements` fail with "No Power
# Platform environment was found" (or simply return the wrong tenant's data),
# which read as a mysterious/unrelated "error" in the UI. Instead of requiring
# the user to manually `pac auth select` the right profile before every check,
# resolve and switch to whichever cached profile can actually see the run's
# environment_id, and remember that mapping so repeat checks are instant.
_PROFILE_CACHE_PATH = DATA_DIR / ".pac_env_profile_cache.json"
_PROFILE_LINE_RE = re.compile(r"^\s*\[(\d+)\]")


def _load_profile_cache() -> dict:
    try:
        return json.loads(_PROFILE_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_profile_cache(cache: dict) -> None:
    try:
        _PROFILE_CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        pass  # best-effort; worst case we just re-search next time


def _list_pac_profile_indexes() -> list[int]:
    rc, text = _pac("auth", "list")
    indexes = []
    for line in text.splitlines():
        m = _PROFILE_LINE_RE.match(line)
        if m:
            indexes.append(int(m.group(1)))
    return indexes


def _profile_sees_environment(environment_id: str) -> bool:
    # `pac admin list` enumerates every environment in the ACTIVE profile's
    # tenant, regardless of which single environment that profile was
    # originally created against -- so this is a reliable "is this the right
    # tenant?" probe even when no profile is scoped to this exact environment.
    rc, text = _pac("admin", "list", timeout=90)
    return rc == 0 and environment_id in text


def ensure_pac_profile_for_environment(environment_id: str) -> tuple[bool, str]:
    """Make `pac`'s active default profile one that can see `environment_id`,
    switching profiles (and caching the match) if the current one can't.
    Returns (ok, note) -- note is only populated on failure."""
    cache = _load_profile_cache()
    cached_idx = cache.get(environment_id)
    if cached_idx is not None:
        rc, _ = _pac("auth", "select", "--index", str(cached_idx))
        if rc == 0 and _profile_sees_environment(environment_id):
            return True, ""
        cache.pop(environment_id, None)  # stale mapping (profile removed/changed)

    if _profile_sees_environment(environment_id):
        return True, ""  # whatever's already active happens to work

    for idx in _list_pac_profile_indexes():
        rc, _ = _pac("auth", "select", "--index", str(idx))
        if rc != 0:
            continue
        if _profile_sees_environment(environment_id):
            cache[environment_id] = idx
            _save_profile_cache(cache)
            return True, ""

    return False, (
        "No cached `pac` sign-in profile can see this environment. Run "
        "`pac auth create --environment <id>` for the tenant this test ran "
        "against (see Live Test tab for the environment id), then retry."
    )


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

    ok, note = ensure_pac_profile_for_environment(environment_id)
    if not ok:
        out["status"] = "error"
        out["note"] = note
        return out

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
