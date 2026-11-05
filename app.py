"""
Flask UI for the Copilot Studio agent load tester.
Local-only. Sits on top of token_provider + d2e_client + engine + stats + run_store.
"""
from __future__ import annotations

import csv
import io
import re
import threading

from flask import Flask, Response, jsonify, render_template, request

import credits as credits_mod
import run_store
from engine import LoadEngine
from stats import StatsAggregator
from token_provider import AccountPoolManager, DeviceCodeProvider, StaticTokenProvider
from token_provider import build_provider as tp_build_provider

app = Flask(__name__)

# --- global current-run state ---------------------------------------------
_state = {
    "engine": None,
    "stats": None,
    "run": None,
    "thread": None,
    "device_prompt": None,
    "provider": None,
    "auth_status": "none",   # none | pending | signed_in | error
    "auth_user": None,
    "pool_manager": None,    # AccountPoolManager (lazily created, keyed by tenant+client)
}
_lock = threading.Lock()

URL_RE = re.compile(
    r"https://(?P<host>[^/]+)/copilotstudio/[^/]+/authenticated/bots/"
    r"(?P<schema>[^/]+)/conversations\?api-version=(?P<ver>[\w\-]+)")

A2A_URL_RE = re.compile(
    r"https://(?P<a2a_host>[^/]+)/copilotstudio-a2a/tenants/(?P<tenant_id>[^/]+)/"
    r"environments/(?P<environment_id>[^/]+)/agents/(?P<agent_id>[^/?#]+)")


def parse_endpoint(url: str) -> dict:
    m = URL_RE.search(url or "")
    if not m:
        return {}
    return {"host": m.group("host"), "schema": m.group("schema"),
            "api_version": m.group("ver")}


def parse_a2a_endpoint(url: str) -> dict:
    """Accepts either the agent base URL or the .well-known/agent-card.json
    discovery URL -- both carry the same tenant/environment/agent path
    segments, so this just strips anything after the agent id."""
    m = A2A_URL_RE.search(url or "")
    if not m:
        return {}
    base = (f"https://{m.group('a2a_host')}/copilotstudio-a2a/tenants/{m.group('tenant_id')}/"
            f"environments/{m.group('environment_id')}/agents/{m.group('agent_id')}")
    return {"a2a_url": base, "tenant_id": m.group("tenant_id"),
            "environment_id": m.group("environment_id"), "agent_id": m.group("agent_id")}


def build_provider(cfg: dict):
    mode = cfg.get("auth_mode", "devicecode")
    if mode == "static":
        return StaticTokenProvider(cfg["static_token"])
    if mode == "pool":
        return _get_pool_manager(cfg["tenant_id"], cfg["client_id"])
    if mode in ("a2a_confidential", "mcp_confidential"):
        return tp_build_provider(cfg)

    def prompt(flow):
        with _lock:
            _state["device_prompt"] = {"message": flow["message"],
                                       "url": flow.get("verification_uri"),
                                       "code": flow.get("user_code")}
    return DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"], prompt=prompt)


def _get_pool_manager(tenant_id: str, client_id: str) -> AccountPoolManager:
    with _lock:
        pm = _state.get("pool_manager")
        if pm is None or pm.tenant_id != tenant_id or pm.client_id != client_id:
            pm = AccountPoolManager(tenant_id, client_id)
            _state["pool_manager"] = pm
        return pm


@app.route("/api/pool/add", methods=["POST"])
def api_pool_add():
    """Wizard step: launch one more interactive device-code sign-in for the pool."""
    body = request.json or {}
    tenant_id, client_id = body.get("tenant_id"), body.get("client_id")
    if not tenant_id or not client_id:
        return jsonify({"error": "tenant_id and client_id are required"}), 400
    pm = _get_pool_manager(tenant_id, client_id)
    flow = pm.begin_add_account()
    return jsonify(flow)


@app.route("/api/pool/status")
def api_pool_status():
    with _lock:
        pm = _state.get("pool_manager")
    if not pm:
        return jsonify({"status": "none"})
    return jsonify(pm.pending_status())


@app.route("/api/pool/accounts")
def api_pool_accounts():
    tenant_id = request.args.get("tenant_id")
    client_id = request.args.get("client_id")
    if tenant_id and client_id:
        pm = _get_pool_manager(tenant_id, client_id)
    else:
        with _lock:
            pm = _state.get("pool_manager")
    return jsonify(pm.list_accounts() if pm else [])


@app.route("/api/pool/remove", methods=["POST"])
def api_pool_remove():
    body = request.json or {}
    with _lock:
        pm = _state.get("pool_manager")
    if pm:
        pm.remove_account(body.get("home_account_id"))
    return jsonify({"ok": True})


@app.route("/api/authenticate", methods=["POST"])
def api_authenticate():
    """On-demand sign-in: pre-warm the token cache from inside the app."""
    cfg = request.json or {}
    with _lock:
        if _state["auth_status"] == "pending":
            return jsonify({"status": "pending",
                            "device_prompt": _state["device_prompt"]})
        _state["auth_status"] = "pending"
        _state["device_prompt"] = None
        provider = build_provider(cfg)
        _state["provider"] = provider

    def do_auth():
        try:
            provider.get_token()
            with _lock:
                _state["auth_status"] = "signed_in"
                _state["device_prompt"] = None
                # record which account (best-effort)
                try:
                    accts = provider._app.get_accounts()  # type: ignore[attr-defined]
                    _state["auth_user"] = accts[0]["username"] if accts else "signed in"
                except Exception:
                    _state["auth_user"] = "signed in"
        except Exception as e:
            with _lock:
                _state["auth_status"] = "error"
                _state["auth_user"] = str(e)[:200]

    threading.Thread(target=do_auth, daemon=True).start()
    return jsonify({"status": "pending"})


@app.route("/api/auth_status")
def api_auth_status():
    with _lock:
        return jsonify({"status": _state["auth_status"],
                        "user": _state["auth_user"],
                        "device_prompt": _state["device_prompt"]})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/parse_url", methods=["POST"])
def api_parse_url():
    url = request.json.get("url", "")
    a2a = parse_a2a_endpoint(url)
    if a2a:
        return jsonify(a2a)
    return jsonify(parse_endpoint(url))


@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = request.json or {}
    protocol = (cfg.get("protocol") or "d2e").lower()
    if protocol == "a2a":
        # Server-side safety net: derive a2a_url/tenant/env/agent from the
        # pasted URL if blank (same pattern as the D2E host/schema derivation).
        if cfg.get("url") and not cfg.get("a2a_url"):
            parsed = parse_a2a_endpoint(cfg["url"])
            for k, v in parsed.items():
                if not cfg.get(k):
                    cfg[k] = v
        if not cfg.get("a2a_url"):
            return jsonify({"error": "A2A agent URL missing. Paste the agent base "
                                      "URL or its .well-known/agent-card.json URL."}), 400
        if cfg.get("auth_mode") == "a2a_confidential" and not cfg.get("client_secret"):
            return jsonify({"error": "client_secret is required for auth_mode "
                                      "a2a_confidential."}), 400
    else:
        # Server-side safety net: derive host/schema/api_version from the URL if blank.
        if cfg.get("url") and (not cfg.get("host") or not cfg.get("schema")):
            parsed = parse_endpoint(cfg["url"])
            for k, v in parsed.items():
                if not cfg.get(k):
                    cfg[k] = v
        if not cfg.get("host") or not cfg.get("schema"):
            return jsonify({"error": "Host/schema missing. Paste a valid endpoint URL."}), 400
    with _lock:
        prev = _state.get("stats")
        thread_busy = _state["thread"] and _state["thread"].is_alive()
        load_running = prev is not None and prev.running
        # Only block if an actual load is still generating traffic. A thread that is
        # merely doing post-run credit/enrichment work must not block a new run.
        if thread_busy and load_running:
            return jsonify({"error": "A run is already in progress."}), 409
        _state["device_prompt"] = None
        run = run_store.new_run(cfg)
        stats = StatsAggregator(on_record=lambda kind, result:
                                run_store.record_event(run["id"], kind, result))
        # Reuse a pre-authenticated provider when available and compatible.
        prov = _state.get("provider")
        if cfg.get("auth_mode") == "devicecode" and prov is not None \
                and getattr(prov, "client_id", None) == cfg.get("client_id"):
            provider = prov
        else:
            provider = build_provider(cfg)
        engine = LoadEngine(cfg, provider, stats)
        _state.update(engine=engine, stats=stats, run=run)

        def runner():
            try:
                mode = cfg.get("mode")
                if mode == "stepped":
                    engine.run_stepped()
                elif mode == "smoke":
                    engine.run_smoke()
                else:
                    engine.run()
            finally:
                snap = stats.snapshot()
                # conversation_ids live inside the summary snapshot (summary_json);
                # the enrich endpoint reads them from run["summary"]["conversation_ids"].
                run_store.finalize(run, snap)
                # Credit/consumption data lags hours; do NOT auto-check at run end.
                # Use the "Refresh credits" button on the Run History row later.

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        _state["thread"] = t
    return jsonify({"ok": True, "run_id": run["id"]})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _lock:
        if _state["engine"]:
            _state["engine"].stop()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    with _lock:
        stats = _state["stats"]
        run = _state["run"]
        prompt = _state["device_prompt"]
    if not stats:
        return jsonify({"running": False, "phase": "idle", "device_prompt": prompt})
    snap = stats.snapshot()
    snap["device_prompt"] = prompt
    snap["run_id"] = run["id"] if run else None
    return jsonify(snap)


@app.route("/api/runs")
def api_runs():
    return jsonify(run_store.list_runs())


@app.route("/api/runs/<run_id>")
def api_run(run_id):
    r = run_store.get(run_id)
    return (jsonify(r), 200) if r else (jsonify({"error": "not found"}), 404)


@app.route("/api/runs/<run_id>/credits", methods=["POST"])
def api_run_credits(run_id):
    r = run_store.get(run_id)
    if not r:
        return jsonify({"error": "not found"}), 404
    env = r.get("environment_id")
    if not env:
        return jsonify({"error": "no environment_id on run"}), 400
    c = credits_mod.check_env_credits(env)
    run_store.update_credits(run_id, c)
    return jsonify(c)


@app.route("/api/runs/<run_id>/enrich", methods=["POST"])
def api_run_enrich(run_id):
    """Historical tool/connector enrichment from Dataverse conversation transcripts."""
    import transcripts
    r = run_store.get(run_id)
    if not r:
        return jsonify({"error": "not found"}), 404
    body = request.json or {}
    org_url = body.get("org_url") or (r.get("config") or {}).get("org_url")
    if not org_url:
        return jsonify({"error": "org_url required (the environment's Dataverse URL, "
                        "e.g. https://orgxxxx.crm.dynamics.com)"}), 400
    cfg = r.get("config") or {}
    conv_ids = (r.get("summary") or {}).get("conversation_ids", [])
    result = transcripts.enrich_run(
        org_url, cfg.get("tenant_id", ""), cfg.get("client_id", ""), conv_ids)
    run_store.update_transcript_enrichment(run_id, result)
    return jsonify(result)


@app.route("/api/runs/<run_id>/events")
def api_run_events(run_id):
    """Full per-request event log for a run (for deep-dive / export)."""
    return jsonify(run_store.get_events(run_id))


@app.route("/api/runs/<run_id>/sessions")
def api_run_sessions(run_id):
    """One row per conversation/session in this run, for the session picker."""
    return jsonify(run_store.get_sessions(run_id))


@app.route("/api/runs/<run_id>/sessions/<conversation_id>")
def api_run_session_detail(run_id, conversation_id):
    """Ordered turn-by-turn transcript (prompt, response, latency) for one session."""
    return jsonify(run_store.get_session_detail(run_id, conversation_id))


def _target_concurrency(cfg: dict) -> str:
    """Requested/configured concurrency, whose meaning differs by load mode."""
    mode = cfg.get("mode")
    if mode == "smoke":
        return cfg.get("smoke_sessions", "")
    if mode == "stepped":
        return cfg.get("max_concurrency", "")
    return cfg.get("virtual_users", "")  # spike (default)


def _duration_seconds(run: dict) -> float | None:
    created, ended = run.get("created"), run.get("ended")
    if created is None or ended is None:
        return None
    return round(ended - created, 1)


@app.route("/api/runs/export.csv")
def api_runs_export_csv():
    """Run History summary across all runs, as CSV, for offline analysis."""
    runs = run_store.list_runs()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "run_id", "created_iso", "label", "schema", "mode", "status",
        "target_concurrency", "peak_concurrency", "sessions", "unique_users",
        "duration_seconds", "total_requests", "success_rate_pct", "throttled",
        "achieved_rpm", "p50_ms", "p95_ms", "tool_calls", "knowledge_hits",
        "tool_error_count", "harness_guess",
        "mcs_messages_consumed", "mcs_messages_allocated",
    ])
    for r in runs:
        cfg = r.get("config") or {}
        sm = r.get("summary") or {}
        cr = r.get("credits") or {}
        w.writerow([
            r.get("id"), r.get("created_iso"), r.get("label"), r.get("schema"),
            cfg.get("mode"), r.get("status"),
            _target_concurrency(cfg), sm.get("peak_active_sessions"),
            sm.get("conversation_count"), sm.get("unique_user_count"),
            _duration_seconds(r), sm.get("total"), sm.get("success_rate"),
            sm.get("throttled"), sm.get("achieved_rpm"), sm.get("p50"), sm.get("p95"),
            sm.get("tool_calls"), sm.get("knowledge_hits"), sm.get("tool_error_count"),
            sm.get("harness_guess"),
            cr.get("mcs_messages_consumed"), cr.get("mcs_messages_allocated"),
        ])
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=run_history_summary.csv"})


@app.route("/api/runs/<run_id>/export.csv")
def api_run_export_csv(run_id):
    """Full per-request event log for one run, as CSV, for deep-dive / offline analysis."""
    r = run_store.get(run_id)
    if not r:
        return jsonify({"error": "not found"}), 404
    events = run_store.get_events(run_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "ts", "kind", "conversation_id", "user_id", "status", "ok", "throttled",
        "latency_ms", "tool_calls", "knowledge_hits", "tool_error", "error",
        "prompt", "response",
    ])
    for e in events:
        w.writerow([
            e.get("ts"), e.get("kind"), e.get("conversation_id"), e.get("user_id"),
            e.get("status"), e.get("ok"), e.get("throttled"), e.get("latency_ms"),
            e.get("tool_calls"), e.get("knowledge_hits"), e.get("tool_error"),
            e.get("error"), e.get("prompt"), e.get("response"),
        ])
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", r.get("label") or run_id)
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=run_{label}_{run_id}_events.csv"})


@app.route("/api/trends")
def api_trends():
    schema = request.args.get("schema") or None
    return jsonify(run_store.trend_series(schema_filter=schema))


# ---- saved configuration presets -------------------------------------------

@app.route("/api/presets", methods=["GET"])
def api_presets_list():
    return jsonify(run_store.list_presets())


@app.route("/api/presets", methods=["POST"])
def api_presets_save():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    # Never persist secrets (client_secret, static_token) into saved presets --
    # server-side enforcement, independent of whether the client UI stripped
    # them, since this is the data boundary that actually writes to disk.
    config = dict(body.get("config", {}))
    config.pop("client_secret", None)
    config.pop("static_token", None)
    preset = run_store.save_preset(name, config, body.get("id"))
    return jsonify(preset)


@app.route("/api/presets/<preset_id>", methods=["GET"])
def api_presets_get(preset_id):
    p = run_store.get_preset(preset_id)
    return (jsonify(p), 200) if p else (jsonify({"error": "not found"}), 404)


@app.route("/api/presets/<preset_id>", methods=["DELETE"])
def api_presets_delete(preset_id):
    run_store.delete_preset(preset_id)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("Agent Load Tester UI -> http://127.0.0.1:5000")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
