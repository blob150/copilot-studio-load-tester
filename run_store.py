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

DB_PATH = Path(__file__).parent / "loadtester.db"
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
    tool_errors_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS presets (
    id TEXT PRIMARY KEY,
    name TEXT,
    created_iso TEXT,
    config_json TEXT
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
    _migrate_legacy_json()


def _migrate_events_columns() -> None:
    """Add conversation_id/user_id/prompt/response columns to pre-existing events
    tables (older DBs created before these were tracked)."""
    c = _conn()
    cols = {row["name"] for row in c.execute("PRAGMA table_info(events)").fetchall()}
    additions = {
        "conversation_id": "TEXT", "user_id": "TEXT",
        "prompt": "TEXT", "response": "TEXT",
        "tool_error": "INTEGER", "tool_errors_json": "TEXT",
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
              "transcript_json": "transcript_enrichment"}
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
            "user_id, prompt, response, tool_error, tool_errors_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, time.time(), kind, result.status, result.latency_ms,
             int(result.throttled), int(result.ok), (result.error or "")[:300],
             getattr(result, "tool_calls", 0), getattr(result, "knowledge_hits", 0),
             result.conversation_id, getattr(result, "user_id", None),
             getattr(result, "prompt", None), response_text,
             int(getattr(result, "tool_error", False)), tool_errors_json))
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


init_db()
