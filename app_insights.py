"""
Application Insights / Log Analytics enrichment: query the OpenTelemetry GenAI
spans Copilot Studio writes for each agent conversation, correlated by the same
conversation_ids the run already tracks (the same key used for Dataverse
transcript enrichment in transcripts.py).

Key discovery from manual investigation (see session notes): for a *workspace-based*
("Redfield"/IngestionMode=LogAnalytics) Application Insights resource, the classic
schema tables (requests/traces/customEvents/dependencies/...) queried through the
`az monitor app-insights query` / Application Insights REST API are EMPTY -- the
actual GenAI tool-call spans land in the underlying Log Analytics workspace's native
table `AppDependencies` (DependencyType == "GenAI"), with `gen_ai.*` fields packed
into the `Properties` JSON blob. This module queries that table directly via
`az monitor log-analytics query` against the resolved workspace GUID.

Auth: shells out to the already-authenticated `az` CLI (same pattern as credits.py's
`pac` wrapper) rather than requesting a new Entra API permission on the load
tester's app registration -- avoids repeating the AADSTS650057 saga for yet another
resource. Like `pac`, `az`'s active subscription is a single machine-wide setting,
so every call re-asserts the subscription this resource lives in before querying.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from paths import DATA_DIR

CONFIG_PATH = DATA_DIR / "appinsights_config.json"

_RESOURCE_RE = re.compile(
    r"subscriptions/(?P<sub>[0-9a-fA-F-]+)/resourceGroups/(?P<rg>[^/]+)"
    r"/providers/[Mm]icrosoft\.[Ii]nsights/components/(?P<name>[^/?#]+)")


def _az(*args, timeout=90) -> tuple[int, str]:
    exe = shutil.which("az")
    if not exe:
        return 1, "Azure CLI (az) not found on PATH"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "az command timed out"


def _run_kql(workspace_id: str, query: str, timeout=90) -> tuple[int, str]:
    """`az monitor log-analytics query --analytics-query` silently mis-parses
    multi-statement KQL (e.g. a `let` followed by newlines) when passed inline
    as a single CLI argument -- it comes back with a generic "No tabular
    expression statement found" semantic error even though the exact same text
    works when read from a file via the `@path` form. Always go through a temp
    file to sidestep that."""
    with tempfile.NamedTemporaryFile("w", suffix=".kql", delete=False) as f:
        f.write(query)
        path = f.name
    try:
        return _az("monitor", "log-analytics", "query", "--workspace", workspace_id,
                    "--analytics-query", f"@{path}", timeout=timeout)
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def _extract_json(text: str):
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                continue
    return None


def parse_resource_reference(text: str) -> dict | None:
    """Accepts either a full Azure Portal URL (like the one pasted from the
    browser address bar) or a bare ARM resource ID -- both contain the same
    subscriptions/.../resourceGroups/.../components/<name> segment."""
    m = _RESOURCE_RE.search(text or "")
    if not m:
        return None
    return {"subscription_id": m.group("sub"), "resource_group": m.group("rg"),
            "resource_name": m.group("name")}


def _load_config() -> dict | None:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return None


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_config() -> dict | None:
    return _load_config()


def ensure_subscription(subscription_id: str) -> tuple[bool, str]:
    """Make `az`'s active subscription the one this resource lives in. Unlike
    the pac-profile search in credits.py, we already know exactly which
    subscription is needed (it's embedded in the resource reference the user
    configured), so this is a direct switch, not a search."""
    rc, text = _az("account", "show", "--query", "id", "-o", "tsv")
    if rc == 0 and text.strip() == subscription_id:
        return True, ""
    rc, text = _az("account", "set", "--subscription", subscription_id)
    if rc != 0:
        return False, (
            f"Could not switch the Azure CLI to subscription {subscription_id}: "
            f"{text.strip()[:300]}. Make sure you're logged in (`az login`) with an "
            f"account that has access to it."
        )
    return True, ""


def _resolve_workspace(subscription_id: str, resource_group: str, resource_name: str) -> dict:
    """Returns {ok, app_id, workspace_id, note}. Looks up the App Insights
    component's underlying Log Analytics workspace GUID -- required because
    workspace-based resources store data under the workspace, not the
    component, and `az monitor log-analytics query` needs the workspace GUID
    (not the component's AppId)."""
    resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/microsoft.insights/components/{resource_name}"
    )
    rc, text = _az("resource", "show", "--ids", resource_id)
    data = _extract_json(text)
    if rc != 0 or not data:
        return {"ok": False, "note": f"Could not find resource {resource_id}: {text.strip()[:300]}"}
    props = data.get("properties", {})
    app_id = props.get("AppId")
    ws_resource_id = props.get("WorkspaceResourceId")
    if not ws_resource_id:
        return {"ok": False, "note": (
            "This App Insights resource has no linked Log Analytics workspace "
            "(classic, not workspace-based) -- querying AppDependencies by workspace "
            "GUID won't work here; this module would need the classic "
            "`az monitor app-insights query --app` path instead."
        )}
    rc2, text2 = _az("monitor", "log-analytics", "workspace", "show",
                      "--ids", ws_resource_id, "--query", "customerId", "-o", "tsv")
    if rc2 != 0 or not text2.strip():
        return {"ok": False, "note": f"Could not resolve workspace GUID: {text2.strip()[:300]}"}
    return {"ok": True, "app_id": app_id, "workspace_id": text2.strip(),
            "workspace_resource_id": ws_resource_id}


def configure(resource_ref: str) -> dict:
    """Parse a pasted Portal URL / resource ID, verify `az` can reach it, resolve
    its workspace GUID, and persist the result so future run checks don't need
    to re-resolve it every time."""
    parsed = parse_resource_reference(resource_ref)
    if not parsed:
        return {"ok": False, "note": (
            "Couldn't find a subscriptions/.../resourceGroups/.../providers/"
            "microsoft.insights/components/<name> segment in that text. Paste the "
            "Azure Portal URL for the resource's Logs blade, or its raw ARM resource ID."
        )}
    ok, note = ensure_subscription(parsed["subscription_id"])
    if not ok:
        return {"ok": False, "note": note}
    resolved = _resolve_workspace(parsed["subscription_id"], parsed["resource_group"],
                                   parsed["resource_name"])
    if not resolved["ok"]:
        return {"ok": False, "note": resolved["note"]}
    cfg = {
        "resource_ref": resource_ref,
        "subscription_id": parsed["subscription_id"],
        "resource_group": parsed["resource_group"],
        "resource_name": parsed["resource_name"],
        "app_id": resolved["app_id"],
        "workspace_id": resolved["workspace_id"],
        "workspace_resource_id": resolved["workspace_resource_id"],
        "configured_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_config(cfg)
    return {"ok": True, "note": f"Connected to {parsed['resource_name']} "
            f"(workspace {resolved['workspace_id']}).", **cfg}


# GenAI span field extraction, shared by both queries below.
_EXTEND = (
    "extend convId = tostring(parse_json(Properties)['gen_ai.conversation.id']),"
    " opName = tostring(parse_json(Properties)['gen_ai.operation.name']),"
    " toolName = tostring(parse_json(Properties)['gen_ai.tool.name']),"
    " toolType = tostring(parse_json(Properties)['gen_ai.tool.type']),"
    " errType = tostring(parse_json(Properties)['error.type']),"
    " statusMsg = tostring(parse_json(Properties)['Status.message'])"
)


def _kql_datetime(epoch: float) -> str:
    # KQL's datetime() literal wants "Z", not a "+00:00" offset, and chokes on
    # excess fractional-second precision -- strip both.
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_window_clause(run_started_epoch: float | None, run_ended_epoch: float | None) -> str:
    if run_started_epoch is None:
        return "where TimeGenerated > ago(2d)"
    start = _kql_datetime(run_started_epoch - 300)
    end = _kql_datetime((run_ended_epoch or run_started_epoch) + 4 * 3600)
    return f"where TimeGenerated between (datetime({start}) .. datetime({end}))"


def check_run(conversation_ids: list[str], run_started_epoch: float | None = None,
              run_ended_epoch: float | None = None, max_convs: int = 200) -> dict:
    out = {"status": "unconfigured", "checked_convs": 0, "tool_calls": 0,
           "operations": [], "errors": [], "note": ""}
    cfg = _load_config()
    if not cfg:
        out["note"] = "Application Insights isn't configured yet -- set it up in the History tab."
        return out
    if not conversation_ids:
        out["status"] = "error"
        out["note"] = "This run has no conversation_ids to correlate against."
        return out

    ok, note = ensure_subscription(cfg["subscription_id"])
    if not ok:
        out["status"] = "error"
        out["note"] = note
        return out

    conv_json = json.dumps(conversation_ids[:max_convs])
    time_clause = _time_window_clause(run_started_epoch, run_ended_epoch)

    summary_query = (
        f"let convIds = dynamic({conv_json});\n"
        f"AppDependencies\n"
        f"| {time_clause}\n"
        f"| {_EXTEND}\n"
        f"| where convId in (convIds)\n"
        f"| summarize Calls=count(), Success=countif(Success=='True'), "
        f"Fail=countif(Success=='False'), AvgDurMs=round(avg(todouble(DurationMs)),0) "
        f"by opName, toolName, toolType\n"
        f"| order by opName, Calls desc"
    )
    rc, text = _run_kql(cfg["workspace_id"], summary_query)
    if rc != 0:
        out["status"] = "error"
        out["note"] = f"Query failed: {text.strip()[:300]}"
        return out
    rows = _extract_json(text) or []
    operations = []
    tool_calls = 0
    mcp_overhead_calls = 0
    for r in rows:
        tool_type = r.get("toolType") or ""
        # MCP session-lifecycle plumbing (handshake/initialize, session-ready
        # notification, tools/list discovery) gets tagged with the SAME
        # operation=="execute_tool" as a real tool invocation under the
        # Standard harness, but is NOT a user-facing "tool call" -- it fires
        # automatically once per MCP session regardless of what the agent
        # actually does. Confirmed via real telemetry: a single Standard-harness
        # conversation showed 3 of these (Initialize x2, ListTools x1) before
        # its first genuine tool use. Left uncounted/unfiltered, this both
        # inflates tool_calls and clutters the per-tool table with noise that
        # GHCP-harness runs never emit (GHCP's A2A bridge doesn't surface this
        # handshake as telemetry at all), making the two harnesses' numbers
        # look artificially different for reasons that have nothing to do with
        # what the agent actually did.
        is_mcp_lifecycle = tool_type.startswith("MCP:Initialize") or tool_type.startswith("MCP:ListTools")
        tool_name = r.get("toolName") or ""
        # Normalize the real MCP tool-invocation name across harnesses: Standard
        # harness names it "InvokeMCP.<tool>" (e.g. "InvokeMCP.read_query")
        # while GHCP names the exact same underlying tool just "<tool>" (e.g.
        # "read_query") -- confirmed by cross-checking identical Dataverse
        # queries in both harnesses. Without this, the same tool would show up
        # as two different rows depending on which harness ran it, breaking
        # any cross-harness/cross-run comparison.
        if tool_name.startswith("InvokeMCP.") and len(tool_name) > len("InvokeMCP."):
            tool_name = tool_name[len("InvokeMCP."):]
        op = {"operation": r.get("opName") or "", "tool": tool_name,
              "tool_type": tool_type, "calls": int(r.get("Calls") or 0),
              "success": int(r.get("Success") or 0), "fail": int(r.get("Fail") or 0),
              "avg_duration_ms": r.get("AvgDurMs")}
        if op["operation"] == "execute_tool" and is_mcp_lifecycle:
            mcp_overhead_calls += op["calls"]
            continue  # excluded from `operations` and from tool_calls entirely
        operations.append(op)
        if op["operation"] == "execute_tool":
            tool_calls += op["calls"]
    out["operations"] = operations
    out["tool_calls"] = tool_calls
    out["mcp_overhead_calls"] = mcp_overhead_calls

    if not rows:
        out["checked_convs"] = 0
        out["status"] = "no_data_yet"
        out["note"] = (
            "No Application Insights telemetry found yet for this run's conversations "
            "in the queried window. This source is usually fast (~1-2 min after a "
            "conversation ends) -- if it's been longer, the agent may not be wired to "
            "this App Insights resource, or the run predates it being configured."
        )
        return out

    errors_query = (
        f"let convIds = dynamic({conv_json});\n"
        f"AppDependencies\n"
        f"| {time_clause}\n"
        f"| {_EXTEND}\n"
        f"| where convId in (convIds) and Success == 'False'\n"
        f"| summarize Count=count() by toolName, toolType, errType, statusMsg\n"
        f"| order by Count desc"
    )
    rc2, text2 = _run_kql(cfg["workspace_id"], errors_query)
    errors = []
    if rc2 == 0:
        for r in (_extract_json(text2) or []):
            tool_type = r.get("toolType") or ""
            if tool_type.startswith("MCP:Initialize") or tool_type.startswith("MCP:ListTools"):
                continue  # same overhead exclusion as above -- not a real tool failure
            tool_name = r.get("toolName") or ""
            if tool_name.startswith("InvokeMCP.") and len(tool_name) > len("InvokeMCP."):
                tool_name = tool_name[len("InvokeMCP."):]
            errors.append({"tool": tool_name, "error_type": r.get("errType") or "",
                           "message": r.get("statusMsg") or "", "count": int(r.get("Count") or 0)})
    out["errors"] = errors

    out["checked_convs"] = len({c for c in conversation_ids})
    out["status"] = "ok"
    note = f"{tool_calls} tool call(s) across {len(operations)} operation/tool combination(s)."
    if mcp_overhead_calls:
        note += (f" (+{mcp_overhead_calls} MCP session-setup call(s) -- handshake/tools-list "
                 f"overhead, not real tool usage -- excluded from these counts.)")
    out["note"] = note
    return out
