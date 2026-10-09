"""
Flask UI for the Copilot Studio agent load tester.
Local-only. Sits on top of token_provider + d2e_client + engine + stats + run_store.
"""
from __future__ import annotations

import re
import threading

from flask import Flask, jsonify, render_template, request

import credits as credits_mod
import run_store
from engine import LoadEngine
from stats import StatsAggregator
from token_provider import DeviceCodeProvider, StaticTokenProvider

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
}
_lock = threading.Lock()

URL_RE = re.compile(
    r"https://(?P<host>[^/]+)/copilotstudio/[^/]+/authenticated/bots/"
    r"(?P<schema>[^/]+)/conversations\?api-version=(?P<ver>[\w\-]+)")


def parse_endpoint(url: str) -> dict:
    m = URL_RE.search(url or "")
    if not m:
        return {}
    return {"host": m.group("host"), "schema": m.group("schema"),
            "api_version": m.group("ver")}


def build_provider(cfg: dict):
    mode = cfg.get("auth_mode", "devicecode")
    if mode == "static":
        return StaticTokenProvider(cfg["static_token"])

    def prompt(flow):
        with _lock:
            _state["device_prompt"] = {"message": flow["message"],
                                       "url": flow.get("verification_uri"),
                                       "code": flow.get("user_code")}
    return DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"], prompt=prompt)


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
    return jsonify(parse_endpoint(request.json.get("url", "")))


@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = request.json or {}
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
                if cfg.get("mode") == "stepped":
                    engine.run_stepped()
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
    preset = run_store.save_preset(name, body.get("config", {}), body.get("id"))
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
