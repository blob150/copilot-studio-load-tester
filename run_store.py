"""
SQLite-backed storage for the load tester: runs, per-request events, and saved
config presets. Replaces the one-JSON-file-per-run approach so we can query/trend
across runs (e.g. p95 over time, success rate by agent) instead of hand-parsing CSVs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from paths import DATA_DIR

DB_PATH = DATA_DIR / "loadtester.db"
_lock = threading.Lock()
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created REAL,
    created_iso TEXT,
    label TEXT,
    environment_id TEXT,
    schema_name TEXT,
    host TEXT,
    status TEXT,
    config_json TEXT,
    summary_json TEXT,
    credits_json TEXT,
    live_tools_json TEXT,
    transcript_json TEXT,
    app_insights_json TEXT,
    ended REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created);
CREATE INDEX IF NOT EXISTS idx_runs_schema ON runs(schema_name);

CREATE TABLE IF NOT EXISTS events (
    run_id TEXT,
    ts REAL,
    kind TEXT,
    status INTEGER,
    latency_ms REAL,
    throttled INTEGER,
    ok INTEGER,
    error TEXT,
    tool_calls INTEGER,
    knowledge_hits INTEGER,
    conversation_id TEXT,
    user_id TEXT,
    prompt TEXT,
    response TEXT,
    tool_error INTEGER,
    tool_errors_json TEXT,
    account TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS presets (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_iso TEXT,
    config_json TEXT
);

CREATE TABLE IF NOT EXISTS auth_profiles (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_iso TEXT,
    tenant_id TEXT,
    client_id TEXT,
    auth_mode TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS prompt_sets (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_iso TEXT,
    prompts_json TEXT
);

-- Scripted multi-turn sessions: a saved GROUP of named scripts, where each
-- script is its own fully ordered turn-by-turn conversation (turn count can
-- differ per script -- a 5-turn script next to a 2-turn script in the same
-- set). This is deliberately a parent/child pair of tables (not one JSON
-- blob column) so each script is an addressable row, mirroring how
-- prompt_sets works today but one level deeper.
CREATE TABLE IF NOT EXISTS script_sets (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_iso TEXT
);

CREATE TABLE IF NOT EXISTS script_set_items (
    id TEXT PRIMARY KEY,
    script_set_id TEXT,
    seq INTEGER,
    turns_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_script_set_items_set ON script_set_items(script_set_id);

CREATE TABLE IF NOT EXISTS account_enabled (
    tenant_id TEXT,
    client_id TEXT,
    home_account_id TEXT,
    enabled INTEGER,
    PRIMARY KEY (tenant_id, client_id, home_account_id)
);

-- Permanent record of "this identity has been observed submitting Allow for
-- this connector, for this agent" -- confirmed empirically (see preflight.py)
-- that a Copilot Studio connector consent grant, once made, persists forever
-- for that (identity, connector) pair. Caching this locally means preflight
-- never has to re-probe an already-consented account by actually talking to
-- the agent again -- a real, measured cost (each probe is a genuine LLM
-- round-trip, tens of seconds) that otherwise repeats on every single
-- preflight run even though the outcome can never change.
CREATE TABLE IF NOT EXISTS connector_consents (
    tenant_id TEXT,
    client_id TEXT,
    home_account_id TEXT,
    schema_name TEXT,
    connector_logical_name TEXT,
    resolved_iso TEXT,
    PRIMARY KEY (tenant_id, client_id, home_account_id, schema_name, connector_logical_name)
);
"""


def _conn() -> sqlite3.Connection:
    # One connection per thread (Flask handles requests across threads; the engine
    # also writes from worker threads), each with WAL for concurrent read/write.
    if not hasattr(_local, "conn"):
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.row_factory = sqlite3.Row
        _local.conn = c
    return _local.conn


def init_db() -> None:
    with _lock:
        c = _conn()
        c.executescript(SCHEMA)
        c.commit()
    _migrate_events_columns()
    _migrate_runs_columns()
    _migrate_legacy_json()


def _migrate_runs_columns() -> None:
    """Add columns to pre-existing runs tables (older DBs created before a given
    enrichment source existed)."""
    c = _conn()
    cols = {row["name"] for row in c.execute("PRAGMA table_info(runs)").fetchall()}
    additions = {"app_insights_json": "TEXT"}
    with _lock:
        for name, coltype in additions.items():
            if name not in cols:
                c.execute(f"ALTER TABLE runs ADD COLUMN {name} {coltype}")
        c.commit()


def _migrate_events_columns() -> None:
    """Add conversation_id/user_id/prompt/response columns to pre-existing events
    tables (older DBs created before these were tracked)."""
    c = _conn()
    cols = {row["name"] for row in c.execute("PRAGMA table_info(events)").fetchall()}
    additions = {
        "conversation_id": "TEXT", "user_id": "TEXT",
        "prompt": "TEXT", "response": "TEXT",
        "tool_error": "INTEGER", "tool_errors_json": "TEXT",
        "account": "TEXT",
    }
    with _lock:
        for name, coltype in additions.items():
            if name not in cols:
                c.execute(f"ALTER TABLE events ADD COLUMN {name} {coltype}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_conv ON events(run_id, conversation_id)")
        c.commit()


def _migrate_legacy_json() -> None:
    """One-time import of the old runs/*.json files, if present and not yet migrated."""
    legacy_dir = Path(__file__).parent / "runs"
    if not legacy_dir.exists():
        return
    c = _conn()
    existing = {r["id"] for r in c.execute("SELECT id FROM runs").fetchall()}
    for p in legacy_dir.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if r.get("id") in existing:
            continue
        with _lock:
            c.execute(
                "INSERT OR IGNORE INTO runs (id, created, created_iso, label, "
                "environment_id, schema_name, host, status, config_json, summary_json, "
                "credits_json, live_tools_json, transcript_json, ended) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("id"), r.get("created"), r.get("created_iso"), r.get("label"),
                 r.get("environment_id"), r.get("schema"), r.get("host"), r.get("status"),
                 json.dumps(r.get("config")), json.dumps(r.get("summary")),
                 json.dumps(r.get("credits")), json.dumps(r.get("live_tools")),
                 json.dumps(r.get("transcript_enrichment")), r.get("ended")))
            c.commit()


# ---- runs -------------------------------------------------------------------

def new_run(config: dict) -> dict:
    run_id = uuid.uuid4().hex[:12]
    now = time.time()
    created_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    c = _conn()
    with _lock:
        c.execute(
            "INSERT INTO runs (id, created, created_iso, label, environment_id, "
            "schema_name, host, status, config_json, summary_json, credits_json, "
            "live_tools_json, transcript_json, ended) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, now, created_iso, config.get("label") or "Load test",
             config.get("environment_id", ""), config.get("schema", ""),
             config.get("host", ""), "running", json.dumps(config),
             None, json.dumps({"status": "pending", "note":
                 "Credit/consumption data aggregates daily and may take hours to surface."}),
             None, None, None))
        c.commit()
    return get(run_id)


def _row_to_run(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["schema"] = d.pop("schema_name", None)
    mapping = {"config_json": "config", "summary_json": "summary",
              "credits_json": "credits", "live_tools_json": "live_tools",
              "transcript_json": "transcript_enrichment",
              "app_insights_json": "app_insights"}
    for key, new_key in mapping.items():
        raw = d.pop(key, None)
        d[new_key] = json.loads(raw) if raw else None
    return d


def finalize(run: dict, summary: dict, status: str = "complete") -> None:
    c = _conn()
    live_tools = {
        "tool_calls": summary.get("tool_calls", 0),
        "knowledge_hits": summary.get("knowledge_hits", 0),
        "tool_names": summary.get("tool_names", {}),
        "tool_error_count": summary.get("tool_error_count", 0),
        "harness_guess": summary.get("harness_guess", "unknown"),
    }
    with _lock:
        c.execute(
            "UPDATE runs SET status=?, summary_json=?, live_tools_json=?, ended=? WHERE id=?",
            (status, json.dumps(summary), json.dumps(live_tools), time.time(), run["id"]))
        c.commit()
    run["status"] = status
    run["summary"] = summary
    run["live_tools"] = live_tools


def record_event(run_id: str, kind: str, result) -> None:
    """Persist one request outcome immediately (crash-safe, enables deep analysis)."""
    c = _conn()
    response_text = " ".join(getattr(result, "bot_texts", None) or [])[:4000]
    tool_errors = getattr(result, "tool_errors", None) or []
    tool_errors_json = json.dumps(tool_errors, default=str)[:4000] if tool_errors else None
    with _lock:
        c.execute(
            "INSERT INTO events (run_id, ts, kind, status, latency_ms, "
            "throttled, ok, error, tool_calls, knowledge_hits, conversation_id, "
            "user_id, prompt, response, tool_error, tool_errors_json, account) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, time.time(), kind, result.status, result.latency_ms,
             int(result.throttled), int(result.ok), (result.error or "")[:300],
             getattr(result, "tool_calls", 0), getattr(result, "knowledge_hits", 0),
             result.conversation_id, getattr(result, "user_id", None),
             getattr(result, "prompt", None), response_text,
             int(getattr(result, "tool_error", False)), tool_errors_json,
             getattr(result, "account", None)))
        c.commit()


def get(run_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _row_to_run(row) if row else None


def get_events(run_id: str) -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT * FROM events WHERE run_id=? ORDER BY ts", (run_id,)).fetchall()
    return [dict(r) for r in rows]


def get_sessions(run_id: str) -> list[dict]:
    """One row per conversation (session) in a run: turn count, timing, latency,
    error/throttle counts -- the index view for picking a session to drill into."""
    c = _conn()
    rows = c.execute(
        "SELECT conversation_id, user_id, "
        "COUNT(*) AS turns, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
        "AVG(latency_ms) AS avg_latency_ms, "
        "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS errors, "
        "SUM(throttled) AS throttled, "
        "SUM(COALESCE(tool_error,0)) AS tool_errors "
        "FROM events WHERE run_id=? AND conversation_id IS NOT NULL "
        "GROUP BY conversation_id ORDER BY first_ts", (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["avg_latency_ms"] = round(d["avg_latency_ms"] or 0)
        d["duration_s"] = round((d["last_ts"] or 0) - (d["first_ts"] or 0), 1)
        d["first_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["first_ts"])) \
            if d["first_ts"] else None
        out.append(d)
    return out


def get_session_detail(run_id: str, conversation_id: str) -> list[dict]:
    """Ordered turn-by-turn transcript for one session: prompt, response, latency,
    and any embedded tool/connector failures (e.g. Azure AI Search errors) found
    even when the overall request succeeded."""
    c = _conn()
    rows = c.execute(
        "SELECT ts, kind, status, latency_ms, throttled, ok, error, prompt, response, "
        "tool_calls, knowledge_hits, tool_error, tool_errors_json FROM events "
        "WHERE run_id=? AND conversation_id=? ORDER BY ts",
        (run_id, conversation_id)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ts_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["ts"]))
        raw_te = d.pop("tool_errors_json", None)
        d["tool_errors"] = json.loads(raw_te) if raw_te else []
        out.append(d)
    return out


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = int(round((pct / 100.0) * (len(sorted_vals) - 1)))
    return sorted_vals[k]


# Dimensions the "Breakdown by:" table (Live Test + History) can group
# performance/error metrics by. Each shares the exact same output shape
# (requests/success_rate/p50/p95/errors/throttled) so the UI can render all
# three with one generic table/renderer and just swap which query produced
# the rows -- rather than three bespoke near-identical tables.
BREAKDOWN_DIMENSIONS = ("account", "prompt", "turn_position")


def get_breakdown(run_id: str, dimension: str) -> list[dict]:
    """Aggregated performance/error breakdown for one run, grouped by:
      - "account": the REAL pooled-account username that backed each session
        (falls back to the synthetic virtual-user label for single-account
        auth modes, which have no per-session account to report) -- answers
        "was performance/errors significantly worse for one specific user."
      - "prompt": the exact prompt text sent (turn-kind events only -- "start"
        events carry no prompt) -- answers "did one specific question perform
        worse than the others."
      - "turn_position": 1st/2nd/3rd... turn within each conversation
        (derived via ROW_NUMBER(), since turn index was never itself stored)
        -- most meaningful for sequential/scripted-mode runs, where a given
        turn number reliably corresponds to the same semantic step; largely
        meaningless for random-pool mode, where turn 3 could be any prompt.
    p50/p95 are computed in Python (SQLite has no built-in percentile
    aggregate), which is fine here since a single run's event count is small
    enough to hold in memory per group."""
    if dimension not in BREAKDOWN_DIMENSIONS:
        return []
    c = _conn()
    if dimension == "account":
        rows = c.execute(
            "SELECT COALESCE(account, user_id, '(unknown)') AS grp, "
            "latency_ms, ok, throttled FROM events WHERE run_id=?", (run_id,)).fetchall()
    elif dimension == "prompt":
        rows = c.execute(
            "SELECT prompt AS grp, latency_ms, ok, throttled FROM events "
            "WHERE run_id=? AND kind='turn' AND prompt IS NOT NULL", (run_id,)).fetchall()
    else:  # turn_position
        rows = c.execute(
            "SELECT 'Turn ' || turn_no AS grp, latency_ms, ok, throttled, turn_no FROM ("
            "  SELECT latency_ms, ok, throttled, "
            "  ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY ts) AS turn_no "
            "  FROM events WHERE run_id=? AND kind='turn' AND conversation_id IS NOT NULL"
            ")", (run_id,)).fetchall()

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["grp"], []).append(r)

    out = []
    for grp, items in groups.items():
        lats = sorted(i["latency_ms"] for i in items if i["latency_ms"] is not None)
        total = len(items)
        ok_count = sum(1 for i in items if i["ok"])
        thr_count = sum(1 for i in items if i["throttled"])
        out.append({
            "label": grp,
            "requests": total,
            "success_rate": round(100 * ok_count / total, 1) if total else None,
            "p50": round(_percentile(lats, 50)) if lats else None,
            "p95": round(_percentile(lats, 95)) if lats else None,
            "errors": total - ok_count,
            "throttled": thr_count,
        })
    if dimension == "turn_position":
        out.sort(key=lambda d: int(d["label"].split(" ")[1]))
    else:
        out.sort(key=lambda d: -d["requests"])
    return out


def list_runs(limit: int = 200) -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT * FROM runs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_run(r) for r in rows]


def update_credits(run_id: str, credits: dict) -> dict | None:
    c = _conn()
    with _lock:
        c.execute("UPDATE runs SET credits_json=? WHERE id=?",
                  (json.dumps(credits), run_id))
        c.commit()
    return get(run_id)


def update_transcript_enrichment(run_id: str, result: dict) -> dict | None:
    c = _conn()
    with _lock:
        c.execute("UPDATE runs SET transcript_json=? WHERE id=?",
                  (json.dumps(result), run_id))
        c.commit()
    return get(run_id)


def update_app_insights(run_id: str, result: dict) -> dict | None:
    c = _conn()
    with _lock:
        c.execute("UPDATE runs SET app_insights_json=? WHERE id=?",
                  (json.dumps(result), run_id))
        c.commit()
    return get(run_id)


def save(run: dict) -> None:
    """Back-compat shim: persist a full run dict (used by older callers)."""
    c = _conn()
    with _lock:
        c.execute(
            "UPDATE runs SET label=?, environment_id=?, schema_name=?, host=?, "
            "status=?, config_json=?, summary_json=?, credits_json=?, "
            "live_tools_json=?, transcript_json=? WHERE id=?",
            (run.get("label"), run.get("environment_id"), run.get("schema"),
             run.get("host"), run.get("status"), json.dumps(run.get("config")),
             json.dumps(run.get("summary")), json.dumps(run.get("credits")),
             json.dumps(run.get("live_tools")), json.dumps(run.get("transcript_enrichment")),
             run["id"]))
        c.commit()


# ---- trending -----------------------------------------------------------------

def trend_series(schema_filter: str | None = None, limit: int = 50) -> list[dict]:
    """One row per run, oldest->newest, with the numbers useful for trend charts."""
    c = _conn()
    q = "SELECT id, created_iso, label, schema_name, summary_json, live_tools_json FROM runs"
    args: tuple = ()
    if schema_filter:
        q += " WHERE schema_name=?"
        args = (schema_filter,)
    q += " ORDER BY created ASC LIMIT ?"
    rows = c.execute(q, args + (limit,)).fetchall()
    out = []
    for r in rows:
        sm = json.loads(r["summary_json"]) if r["summary_json"] else {}
        lt = json.loads(r["live_tools_json"]) if r["live_tools_json"] else {}
        sm = sm or {}
        lt = lt or {}
        out.append({
            "id": r["id"], "created_iso": r["created_iso"], "label": r["label"],
            "schema": r["schema_name"],
            "p50": sm.get("p50", 0), "p95": sm.get("p95", 0),
            "success_rate": sm.get("success_rate", 0),
            "throttled": sm.get("throttled", 0), "total": sm.get("total", 0),
            "tool_calls": lt.get("tool_calls", 0),
        })
    return out


# ---- presets --------------------------------------------------------------

def save_preset(name: str, config: dict, preset_id: str | None = None) -> dict:
    c = _conn()
    pid = preset_id or uuid.uuid4().hex[:12]
    created_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        c.execute(
            "INSERT INTO presets (id, name, created_iso, config_json) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, config_json=excluded.config_json",
            (pid, name, created_iso, json.dumps(config)))
        c.commit()
    return {"id": pid, "name": name, "created_iso": created_iso, "config": config}


def list_presets() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT id, name, created_iso FROM presets ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_preset(preset_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM presets WHERE id=?", (preset_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["config"] = json.loads(d.pop("config_json"))
    return d


def delete_preset(preset_id: str) -> None:
    c = _conn()
    with _lock:
        c.execute("DELETE FROM presets WHERE id=?", (preset_id,))
        c.commit()


# ---- auth profiles ----------------------------------------------------
# A named (tenant_id, client_id, auth_mode) combo, independent of load-test
# presets, so switching which tenant/app-registration you're testing against
# doesn't require re-typing GUIDs or hunting through unrelated saved load
# configs. Never stores client_secret -- same policy as presets.

def save_auth_profile(name: str, tenant_id: str, client_id: str,
                      auth_mode: str = "pool", note: str = "",
                      profile_id: str | None = None) -> dict:
    c = _conn()
    pid = profile_id or uuid.uuid4().hex[:12]
    created_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        c.execute(
            "INSERT INTO auth_profiles (id, name, created_iso, tenant_id, client_id, "
            "auth_mode, note) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, tenant_id=excluded.tenant_id, "
            "client_id=excluded.client_id, auth_mode=excluded.auth_mode, note=excluded.note",
            (pid, name, created_iso, tenant_id, client_id, auth_mode, note))
        c.commit()
    return {"id": pid, "name": name, "created_iso": created_iso, "tenant_id": tenant_id,
            "client_id": client_id, "auth_mode": auth_mode, "note": note}


def list_auth_profiles() -> list[dict]:
    c = _conn()
    rows = c.execute(
        "SELECT id, name, created_iso, tenant_id, client_id, auth_mode, note "
        "FROM auth_profiles ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_auth_profile(profile_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM auth_profiles WHERE id=?", (profile_id,)).fetchone()
    return dict(row) if row else None


def delete_auth_profile(profile_id: str) -> None:
    c = _conn()
    with _lock:
        c.execute("DELETE FROM auth_profiles WHERE id=?", (profile_id,))
        c.commit()


# ---- prompt sets --------------------------------------------------------
# Named, reusable lists of test prompts, independent of load-test presets --
# lets you build up a library of question sets (e.g. "SOP questions",
# "weather smoke test") and apply any of them to the current test without
# re-typing, separate from the rest of a preset's load-shape settings.

def save_prompt_set(name: str, prompts: list[str], set_id: str | None = None) -> dict:
    c = _conn()
    pid = set_id or uuid.uuid4().hex[:12]
    created_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        c.execute(
            "INSERT INTO prompt_sets (id, name, created_iso, prompts_json) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, prompts_json=excluded.prompts_json",
            (pid, name, created_iso, json.dumps(prompts)))
        c.commit()
    return {"id": pid, "name": name, "created_iso": created_iso, "prompts": prompts}


def list_prompt_sets() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT id, name, created_iso FROM prompt_sets ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_prompt_set(set_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM prompt_sets WHERE id=?", (set_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["prompts"] = json.loads(d.pop("prompts_json"))
    return d


def delete_prompt_set(set_id: str) -> None:
    c = _conn()
    with _lock:
        c.execute("DELETE FROM prompt_sets WHERE id=?", (set_id,))
        c.commit()


# ---- script sets (scripted multi-turn sessions) --------------------------
# A saved, named GROUP of scripts for "Scripted sessions" mode: each script is
# its own fully ordered list of turn prompts, imported from a Copilot Studio
# test-case export (Test Case ID, Turn, Query[, Expected Response]) or typed
# by hand. At runtime each virtual user is assigned ONE WHOLE SCRIPT (picked
# randomly from the set) rather than cycling through a single shared pool --
# scripts are free to have DIFFERENT turn counts (a real exported test-case
# file naturally has test cases of varying length), each session's turn
# count is simply whatever its chosen script has. Stored as a parent
# (script_sets) + child (script_set_items, one row per script) pair so each
# script is independently addressable, not one opaque JSON blob.

def save_script_set(name: str, scripts: list[list[str]], set_id: str | None = None) -> dict:
    if not scripts:
        raise ValueError("At least one script is required.")
    c = _conn()
    sid = set_id or uuid.uuid4().hex[:12]
    created_iso = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        c.execute(
            "INSERT INTO script_sets (id, name, created_iso) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (sid, name, created_iso))
        # Replace-all-children on save: simplest correct semantics for an
        # editor UI (add/remove/reorder scripts freely), avoids having to
        # diff old vs. new script lists.
        c.execute("DELETE FROM script_set_items WHERE script_set_id=?", (sid,))
        for seq, turns in enumerate(scripts):
            c.execute(
                "INSERT INTO script_set_items (id, script_set_id, seq, turns_json) VALUES (?,?,?,?)",
                (uuid.uuid4().hex[:12], sid, seq, json.dumps(turns)))
        c.commit()
    return {"id": sid, "name": name, "created_iso": created_iso, "scripts": scripts}



def list_script_sets() -> list[dict]:
    c = _conn()
    rows = c.execute("SELECT id, name, created_iso FROM script_sets ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_script_set(set_id: str) -> dict | None:
    c = _conn()
    row = c.execute("SELECT * FROM script_sets WHERE id=?", (set_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    items = c.execute(
        "SELECT turns_json FROM script_set_items WHERE script_set_id=? ORDER BY seq", (set_id,)
    ).fetchall()
    d["scripts"] = [json.loads(r["turns_json"]) for r in items]
    return d


def delete_script_set(set_id: str) -> None:
    c = _conn()
    with _lock:
        c.execute("DELETE FROM script_set_items WHERE script_set_id=?", (set_id,))
        c.execute("DELETE FROM script_sets WHERE id=?", (set_id,))
        c.commit()


# ---- per-account enabled/disabled state ------------------------------------
# Lets a user exclude specific signed-in accounts from actually being used in
# a run's Account Pool, without removing them entirely (re-enable any time).
# Absence of a row means "enabled" (the historical default -- every cached
# account was used automatically before this existed).

def is_account_enabled(tenant_id: str, client_id: str, home_account_id: str) -> bool:
    c = _conn()
    row = c.execute(
        "SELECT enabled FROM account_enabled WHERE tenant_id=? AND client_id=? AND home_account_id=?",
        (tenant_id, client_id, home_account_id)).fetchone()
    return True if row is None else bool(row["enabled"])


def set_account_enabled(tenant_id: str, client_id: str, home_account_id: str, enabled: bool) -> None:
    c = _conn()
    with _lock:
        c.execute(
            "INSERT INTO account_enabled (tenant_id, client_id, home_account_id, enabled) "
            "VALUES (?,?,?,?) ON CONFLICT(tenant_id, client_id, home_account_id) "
            "DO UPDATE SET enabled=excluded.enabled",
            (tenant_id, client_id, home_account_id, 1 if enabled else 0))
        c.commit()


# ---- connector consent cache --------------------------------------------
# See connector_consents table comment in SCHEMA: consent is permanent per
# (identity, connector), so once observed it never needs re-checking.

def get_consented_connectors(tenant_id: str, client_id: str, home_account_id: str,
                              schema_name: str) -> set[str]:
    c = _conn()
    rows = c.execute(
        "SELECT connector_logical_name FROM connector_consents "
        "WHERE tenant_id=? AND client_id=? AND home_account_id=? AND schema_name=?",
        (tenant_id, client_id, home_account_id, schema_name)).fetchall()
    return {r["connector_logical_name"] for r in rows}


def mark_connectors_consented(tenant_id: str, client_id: str, home_account_id: str,
                               schema_name: str, connector_logical_names) -> None:
    if not connector_logical_names:
        return
    c = _conn()
    import time as _time
    now_iso = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime())
    with _lock:
        for name in connector_logical_names:
            c.execute(
                "INSERT OR REPLACE INTO connector_consents "
                "(tenant_id, client_id, home_account_id, schema_name, connector_logical_name, resolved_iso) "
                "VALUES (?,?,?,?,?,?)",
                (tenant_id, client_id, home_account_id, schema_name, name, now_iso))
        c.commit()


init_db()
