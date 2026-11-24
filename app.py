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

import app_insights as app_insights_mod
import credits as credits_mod
import preflight as preflight_mod
import run_store
import setup_wizard as setup_wizard_mod
from engine import LoadEngine
from stats import StatsAggregator
from token_provider import AccountPoolManager, DeviceCodeProvider, StaticTokenProvider
from token_provider import SCOPE as SCOPE_DEFAULT
from token_provider import build_provider as tp_build_provider
from token_provider import scope_for_protocol as tp_scope_for_protocol

app = Flask(__name__)

# Unique per process start. The frontend polls /api/boot_id and force-reloads
# itself the moment this changes -- this is how an already-open browser tab
# notices the dev server restarted (e.g. after we edit app.py/templates) and
# picks up the new code automatically, instead of silently continuing to run
# whatever JS it loaded at the start of the session. See _no_cache below for
# the complementary fix (stale *response* caching on a fresh load/refresh).
import uuid as _uuid
_BOOT_ID = _uuid.uuid4().hex


@app.route("/api/boot_id")
def api_boot_id():
    return jsonify({"boot_id": _BOOT_ID})


@app.errorhandler(Exception)
def _api_json_errors(e):
    """Any unhandled exception on an /api/* route returns a JSON body
    instead of Flask's default HTML error page. Without this, a server-side
    failure (e.g. a malformed tenant_id/client_id causing MSAL to raise)
    made `await r.json()` in the frontend throw an uncaught parse error
    inside a setInterval poll loop -- which silently killed the poll with
    NO visible error, leaving the UI stuck on "Starting sign-in..." forever
    with no indication anything had gone wrong. Non-API routes (just the
    one HTML page here) fall through to Flask's normal error handling."""
    if request.path.startswith("/api/"):
        import traceback
        app.logger.error("Unhandled error on %s: %s", request.path, e)
        traceback.print_exc()
        return jsonify({"error": str(e) or e.__class__.__name__}), 500
    raise e


@app.after_request
def _no_cache(resp):
    """This is a single-page local admin tool: the entire UI (HTML/CSS/JS)
    lives in one inline template, and we edit it constantly. Without explicit
    headers, browsers are free to heuristically cache a GET response with no
    Cache-Control at all (or reuse it via back/forward navigation, bfcache,
    or a simple refresh), which silently serves a stale page after every code
    change -- the repeated "the layout broke again" reports were this, not
    real regressions. Force every response to always be re-fetched."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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
    "pool_managers": {},     # {(tenant_id, client_id, tuple(scope)): AccountPoolManager}
                             # keyed dict (not a single slot) so switching between
                             # tenants/app registrations never discards an
                             # already-signed-in pool -- each tenant you've used
                             # keeps its own live manager + account list.
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


_D2E_HOST_ENV_RE = re.compile(
    r"^(?P<p1>[0-9a-f]{30})\.(?P<p2>[0-9a-f]{2})\.environment\.", re.IGNORECASE)


def environment_id_from_d2e_host(host: str) -> str | None:
    """The D2E host's subdomain IS the environment GUID with dashes removed,
    just split into two labels around the region segment (30 hex chars, then
    a 2-hex region code, e.g. 'f3') -- e.g. a host like
    '<30-hex-chars>.<2-hex-region>.environment.api.powerplatform.com' maps
    back to the dashed GUID '<8>-<4>-<4>-<4>-<12>' form. Lets the UI auto-fill
    Environment ID the same way A2A URLs already auto-fill tenant/environment,
    instead of requiring it to be typed in separately."""
    m = _D2E_HOST_ENV_RE.match(host or "")
    if not m:
        return None
    hex32 = (m.group("p1") + m.group("p2")).lower()
    if len(hex32) != 32:
        return None
    return f"{hex32[0:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


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
    scope = tp_scope_for_protocol(cfg.get("protocol"))
    if mode == "static":
        return StaticTokenProvider(cfg["static_token"])
    if mode == "pool":
        return _get_pool_manager(cfg["tenant_id"], cfg["client_id"], scope)
    if mode in ("a2a_confidential", "mcp_confidential"):
        return tp_build_provider(cfg)

    def prompt(flow):
        with _lock:
            _state["device_prompt"] = {"message": flow["message"],
                                       "url": flow.get("verification_uri"),
                                       "code": flow.get("user_code")}
    return DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"], prompt=prompt, scope=scope)


def _get_pool_manager(tenant_id: str, client_id: str, scope: list[str] | None = None) -> AccountPoolManager:
    scope = scope or SCOPE_DEFAULT
    key = (tenant_id, client_id, tuple(scope))
    with _lock:
        pm = _state["pool_managers"].get(key)
    if pm is not None:
        return pm
    # Construct OUTSIDE the global lock -- AccountPoolManager's constructor
    # can make a real network call (MSAL authority/OIDC discovery), which is
    # slow-to-fail for a malformed tenant_id/client_id (rather than failing
    # instantly). Holding _lock across that would serialize EVERY other
    # request behind one bad/slow input -- e.g. a client repeatedly retrying
    # a bad value every 1.5s (as the pool-add polling loop does) could stall
    # unrelated requests for the whole app. A harmless duplicate construction
    # in a rare race is fine -- cheap, and the dict assignment is atomic.
    # is_enabled does a live DB lookup each call (not cached at construction
    # time), so toggling an account on/off in the Accounts tab takes effect
    # on the very next run -- no need to rebuild the pool manager.
    pm = AccountPoolManager(
        tenant_id, client_id, scope=scope,
        is_enabled=lambda haid: run_store.is_account_enabled(tenant_id, client_id, haid))
    with _lock:
        _state["pool_managers"].setdefault(key, pm)
        return _state["pool_managers"][key]


@app.route("/api/pool/add", methods=["POST"])
def api_pool_add():
    """Wizard step: launch one more interactive device-code sign-in for the pool."""
    body = request.json or {}
    tenant_id, client_id = body.get("tenant_id"), body.get("client_id")
    if not tenant_id or not client_id:
        return jsonify({"error": "tenant_id and client_id are required"}), 400
    scope = tp_scope_for_protocol(body.get("protocol"))
    pm = _get_pool_manager(tenant_id, client_id, scope)
    flow = pm.begin_add_account()
    return jsonify(flow)


@app.route("/api/pool/status")
def api_pool_status():
    tenant_id, client_id = request.args.get("tenant_id"), request.args.get("client_id")
    if not tenant_id or not client_id:
        return jsonify({"status": "none"})
    scope = tp_scope_for_protocol(request.args.get("protocol"))
    pm = _get_pool_manager(tenant_id, client_id, scope)
    return jsonify(pm.pending_status())


@app.route("/api/pool/accounts")
def api_pool_accounts():
    tenant_id = request.args.get("tenant_id")
    client_id = request.args.get("client_id")
    if not tenant_id or not client_id:
        return jsonify([])
    scope = tp_scope_for_protocol(request.args.get("protocol"))
    pm = _get_pool_manager(tenant_id, client_id, scope)
    accounts = pm.list_accounts()
    # Defense in depth: a MSAL home_account_id always ends in
    # "<object_id>.<tenant_id>" -- if any cache file ever ends up holding an
    # account for the WRONG tenant (e.g. from a stale/corrupted file left
    # over by a past bug, or a future caller that constructs a pool manager
    # with a mismatched tenant/client pairing), silently returning it would
    # show one tenant's real identities under a completely different
    # tenant/profile's accounts list -- confusing and a privacy concern.
    # Drop anything that doesn't match instead.
    accounts = [a for a in accounts if (a.get("home_account_id") or "").endswith("." + tenant_id)]
    for a in accounts:
        a["enabled"] = run_store.is_account_enabled(tenant_id, client_id, a.get("home_account_id"))
    return jsonify(accounts)


@app.route("/api/pool/accounts_all")
def api_pool_accounts_all():
    """Aggregate accounts across EVERY known (tenant_id, client_id) combo --
    every saved auth profile, plus any ad-hoc tenant/client passed in query
    params that isn't already covered by a saved profile -- each account
    tagged with which tenant/profile it belongs to. Without this, switching
    to a second tenant's account pool made the first tenant's accounts
    invisible (nothing on screen told you a second tenant's accounts even
    existed), which is what this endpoint and the matching UI fix for.

    Checks BOTH the Account Pool cache AND the single-account devicecode
    cache for each target, since a tenant might have been used via either
    "Authenticate now" (devicecode) or the pool wizard -- a tenant you only
    ever used one of those two ways shouldn't be invisible here."""
    scope = tp_scope_for_protocol(request.args.get("protocol"))
    seen: set[tuple[str, str]] = set()
    targets: list[dict] = []
    for p in run_store.list_auth_profiles():
        key = (p["tenant_id"], p["client_id"])
        if key not in seen:
            seen.add(key)
            targets.append({"tenant_id": p["tenant_id"], "client_id": p["client_id"],
                            "profile_name": p["name"]})
    adhoc_tenant = request.args.get("tenant_id")
    adhoc_client = request.args.get("client_id")
    if adhoc_tenant and adhoc_client and (adhoc_tenant, adhoc_client) not in seen:
        targets.append({"tenant_id": adhoc_tenant, "client_id": adhoc_client,
                        "profile_name": None})

    out = []
    for t in targets:
        seen_haids: set[str] = set()
        pm = _get_pool_manager(t["tenant_id"], t["client_id"], scope)
        for a in pm.list_accounts():
            a["tenant_id"] = t["tenant_id"]
            a["client_id"] = t["client_id"]
            a["profile_name"] = t["profile_name"]
            a["enabled"] = run_store.is_account_enabled(t["tenant_id"], t["client_id"], a.get("home_account_id"))
            seen_haids.add(a.get("home_account_id"))
            out.append(a)
        dc = DeviceCodeProvider(t["tenant_id"], t["client_id"], prompt=lambda f: None, scope=scope)
        for a in dc.list_accounts():
            if a.get("home_account_id") in seen_haids:
                continue  # same account signed in via both modes -- don't double-list
            a["tenant_id"] = t["tenant_id"]
            a["client_id"] = t["client_id"]
            a["profile_name"] = t["profile_name"]
            a["enabled"] = run_store.is_account_enabled(t["tenant_id"], t["client_id"], a.get("home_account_id"))
            out.append(a)
    return jsonify(out)


@app.route("/api/pool/toggle", methods=["POST"])
def api_pool_toggle():
    """Mark a specific cached account as enabled/disabled for actual USE in
    a run, without removing it from the cache (toggle it back on any time).
    Independent of which tenant/pool is currently selected in the Live Test
    form -- the request carries its own tenant_id/client_id."""
    body = request.json or {}
    tenant_id, client_id = body.get("tenant_id"), body.get("client_id")
    home_account_id = body.get("home_account_id")
    if not tenant_id or not client_id or not home_account_id:
        return jsonify({"error": "tenant_id, client_id, and home_account_id are required"}), 400
    run_store.set_account_enabled(tenant_id, client_id, home_account_id, bool(body.get("enabled", True)))
    return jsonify({"ok": True})


@app.route("/api/pool/remove", methods=["POST"])
def api_pool_remove():
    body = request.json or {}
    tenant_id, client_id = body.get("tenant_id"), body.get("client_id")
    if not tenant_id or not client_id:
        return jsonify({"error": "tenant_id and client_id are required"}), 400
    scope = tp_scope_for_protocol(body.get("protocol"))
    pm = _get_pool_manager(tenant_id, client_id, scope)
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
    try:
        # build_provider can raise (e.g. a malformed tenant_id/client_id
        # makes MSAL's constructor fail, sometimes only after a slow-to-fail
        # network call for authority/OIDC discovery) -- deliberately called
        # OUTSIDE _lock so a slow/bad input here can't stall every other
        # request in the app behind this one. Without the try/except,
        # auth_status was left stuck at "pending" forever -- which also
        # permanently blocked all FUTURE "Authenticate now" attempts, since
        # the early-return above treats "pending" as already-in-progress.
        provider = build_provider(cfg)
    except Exception as e:
        with _lock:
            _state["auth_status"] = "error"
            _state["auth_user"] = str(e)[:300]
        return jsonify({"status": "error", "error": str(e)[:300]}), 400
    with _lock:
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
    d2e = parse_endpoint(url)
    if d2e.get("host"):
        env_id = environment_id_from_d2e_host(d2e["host"])
        if env_id:
            d2e["environment_id"] = env_id
    return jsonify(d2e)


def _normalize_cfg_endpoint(cfg: dict) -> tuple[dict, tuple | None]:
    """Shared by /api/start and /api/preflight: derive host/schema (D2E) or
    a2a_url/tenant/env/agent (A2A) from a pasted URL if blank, and validate
    the minimum fields are present. Returns (cfg, None) on success, or
    (cfg, (error_response_dict, http_status)) on failure -- caller should
    `return jsonify(err[0]), err[1]` when the second element isn't None."""
    protocol = (cfg.get("protocol") or "d2e").lower()
    if protocol == "a2a":
        if cfg.get("url") and not cfg.get("a2a_url"):
            parsed = parse_a2a_endpoint(cfg["url"])
            for k, v in parsed.items():
                if not cfg.get(k):
                    cfg[k] = v
        if not cfg.get("a2a_url"):
            return cfg, ({"error": "A2A agent URL missing. Paste the agent base "
                                    "URL or its .well-known/agent-card.json URL."}, 400)
        if cfg.get("auth_mode") == "a2a_confidential" and not cfg.get("client_secret"):
            return cfg, ({"error": "client_secret is required for auth_mode "
                                    "a2a_confidential."}, 400)
    else:
        if cfg.get("url") and (not cfg.get("host") or not cfg.get("schema")):
            parsed = parse_endpoint(cfg["url"])
            for k, v in parsed.items():
                if not cfg.get(k):
                    cfg[k] = v
        if not cfg.get("host") or not cfg.get("schema"):
            return cfg, ({"error": "Host/schema missing. Paste a valid endpoint URL."}, 400)
        if cfg.get("host") and not cfg.get("environment_id"):
            env_id = environment_id_from_d2e_host(cfg["host"])
            if env_id:
                cfg["environment_id"] = env_id
    return cfg, None


@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = request.json or {}
    cfg, err = _normalize_cfg_endpoint(cfg)
    if err:
        return jsonify(err[0]), err[1]
    protocol = (cfg.get("protocol") or "d2e").lower()
    with _lock:
        prev = _state.get("stats")
        thread_busy = _state["thread"] and _state["thread"].is_alive()
        load_running = prev is not None and prev.running
        # Only block if an actual load is still generating traffic. A thread that is
        # merely doing post-run credit/enrichment work must not block a new run.
        if thread_busy and load_running:
            return jsonify({"error": "A run is already in progress."}), 409
        prov = _state.get("provider")

    # Reuse a pre-authenticated provider when available and compatible --
    # must match tenant, client, AND scope (protocol), not just client_id,
    # otherwise a provider authenticated for a different tenant or a
    # different protocol's scope would be silently reused and fail (or
    # worse, silently hit the wrong tenant's agent).
    want_scope = tp_scope_for_protocol(cfg.get("protocol"))
    if cfg.get("auth_mode") == "devicecode" and prov is not None \
            and getattr(prov, "client_id", None) == cfg.get("client_id") \
            and getattr(prov, "tenant_id", None) == cfg.get("tenant_id") \
            and getattr(prov, "scope", None) == want_scope:
        provider = prov
    else:
        # Deliberately called OUTSIDE _lock: build_provider() can end up in
        # _get_pool_manager(), which acquires _lock itself. _lock is a plain
        # (non-reentrant) threading.Lock, so calling it again from a thread
        # that already holds it self-deadlocks PERMANENTLY -- that thread
        # never releases the lock again, which in turn freezes every other
        # request in the app that touches _lock (including a plain GET
        # /api/status), making "Start Test" look like it does nothing at
        # all when in fact the whole server silently wedged. Constructing
        # providers only ever needs to happen once per tenant/client/scope
        # combo (cached in _state["pool_managers"]), so there is no
        # correctness reason for this to be inside the critical section.
        provider = build_provider(cfg)

    # Hard-fail BEFORE creating a run record or starting any virtual user
    # threads if this provider would need an interactive sign-in
    # (device-code or a confidential auth-code flow) mid-run. Every
    # virtual user thread shares the SAME provider instance, and its
    # internal lock is held for the ENTIRE interactive flow (up to ~15
    # minutes for device code) -- so the first thread blocks waiting for
    # you to sign in, and every other thread blocks behind it, with the
    # running test showing "N active, 0 completed" forever and no
    # visible prompt (see plan.md 2026-11-09 "A2A smoke test appeared
    # stuck"). Pool mode already fails fast via get_token_for's
    # RuntimeError, so it's exempt from this check.
    has_check = getattr(provider, "has_cached_token", None)
    if has_check is not None and not has_check():
        return jsonify({
            "error": "This app registration/tenant has no cached sign-in yet. "
                     "Click \"Authenticate now\" and complete the sign-in prompt "
                     "BEFORE starting a run with more than one session -- "
                     "otherwise every virtual user blocks waiting behind the "
                     "same interactive sign-in with no visible progress."
        }), 400

    with _lock:
        # Re-check "already running" -- a race is possible between the first
        # check above and now (another request could have started a run
        # while this one was building its provider), and this is cheap
        # insurance against double-starting.
        prev = _state.get("stats")
        thread_busy = _state["thread"] and _state["thread"].is_alive()
        if thread_busy and prev is not None and prev.running:
            return jsonify({"error": "A run is already in progress."}), 409

        _state["device_prompt"] = None
        run = run_store.new_run(cfg)
        stats = StatsAggregator(on_record=lambda kind, result:
                                run_store.record_event(run["id"], kind, result))
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


@app.route("/api/preflight", methods=["POST"])
def api_preflight():
    """Check that a run is actually ready to generate meaningful load BEFORE
    starting one: every configured account's token is live, and (for D2E)
    every configured prompt's connector has already been consented for each
    account -- auto-granting consent via a disposable conversation per
    prompt/account if not. See preflight.py for why both checks matter."""
    cfg = request.json or {}
    cfg, err = _normalize_cfg_endpoint(cfg)
    if err:
        return jsonify(err[0]), err[1]

    mode = cfg.get("auth_mode", "devicecode")
    scope = tp_scope_for_protocol(cfg.get("protocol"))
    pool_manager = None
    single_provider = None
    if mode == "pool":
        pool_manager = _get_pool_manager(cfg["tenant_id"], cfg["client_id"], scope)
    elif mode == "devicecode":
        # Checked the same way as pool mode (list_accounts()/duck-typing in
        # preflight.py) -- NOT via DeviceCodeProvider.get_token(), which
        # would trigger an interactive device-code flow if nothing is
        # cached. Preflight must never block waiting for interactive sign-in;
        # it should just report "needs sign in" and let the user use the
        # existing "Authenticate now" button first.
        pool_manager = DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"], scope=scope)
    elif mode == "static":
        single_provider = StaticTokenProvider(cfg["static_token"])
    elif mode in ("a2a_confidential", "mcp_confidential"):
        single_provider = tp_build_provider(cfg)
    else:
        return jsonify({"error": f"Unknown auth_mode: {mode}"}), 400

    result = preflight_mod.run_preflight(cfg, pool_manager=pool_manager,
                                         single_provider=single_provider)
    return jsonify(result)


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
        org_url, cfg.get("tenant_id", ""), cfg.get("client_id", ""), conv_ids,
        run_started_epoch=r.get("created"))
    run_store.update_transcript_enrichment(run_id, result)
    return jsonify(result)


@app.route("/api/config/appinsights", methods=["GET"])
def api_get_appinsights_config():
    cfg = app_insights_mod.get_config()
    return jsonify(cfg or {"configured": False})


@app.route("/api/config/appinsights", methods=["POST"])
def api_set_appinsights_config():
    body = request.json or {}
    resource_ref = (body.get("resource_ref") or "").strip()
    if not resource_ref:
        return jsonify({"ok": False, "note": "Paste the Azure Portal URL or ARM resource ID "
                        "for the Application Insights resource."}), 400
    result = app_insights_mod.configure(resource_ref)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/runs/<run_id>/appinsights", methods=["POST"])
def api_run_appinsights(run_id):
    """Historical tool/connector enrichment from Application Insights GenAI spans
    (OpenTelemetry), correlated by the same conversation_ids used for Dataverse
    transcript enrichment -- usually available within ~1-2 minutes of a
    conversation ending, far faster than the Dataverse transcript sync."""
    r = run_store.get(run_id)
    if not r:
        return jsonify({"error": "not found"}), 404
    conv_ids = (r.get("summary") or {}).get("conversation_ids", [])
    result = app_insights_mod.check_run(
        conv_ids, run_started_epoch=r.get("created"), run_ended_epoch=r.get("ended"))
    run_store.update_app_insights(run_id, result)
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


@app.route("/api/runs/<run_id>/breakdown")
def api_run_breakdown(run_id):
    """Aggregated performance/error breakdown for one run, grouped by
    ?dimension=account|prompt|turn_position -- powers the "Breakdown by:"
    table shown in both Live Test and History (one generic endpoint/renderer
    for all three dimensions rather than bespoke per-dimension routes)."""
    dimension = request.args.get("dimension", "account")
    if dimension not in run_store.BREAKDOWN_DIMENSIONS:
        return jsonify({"error": f"dimension must be one of {run_store.BREAKDOWN_DIMENSIONS}"}), 400
    return jsonify(run_store.get_breakdown(run_id, dimension))


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


# ---- auth profiles (named tenant_id/client_id/auth_mode combos) -----------
# Independent of load-test presets: lets you switch between tenants/app
# ---- first-time setup wizard -----------------------------------------------
# Thin layer on top of existing auth machinery (see setup_wizard.py docstring)
# -- prereq check + static guide content only; actual sign-in/save reuses
# /api/authenticate, /api/auth_status, and /api/auth_profiles unchanged.

@app.route("/api/setup/prereqs")
def api_setup_prereqs():
    return jsonify(setup_wizard_mod.check_prereqs())


@app.route("/api/setup/entra_guide")
def api_setup_entra_guide():
    return jsonify(setup_wizard_mod.ENTRA_SETUP_GUIDE)


@app.route("/api/setup/status")
def api_setup_status():
    """Whether this looks like a brand-new install (no saved Auth Profile
    yet) -- the frontend uses this to decide whether to auto-open the setup
    wizard on load, without needing a separate first-run marker file."""
    profiles = run_store.list_auth_profiles()
    return jsonify({"is_first_run": len(profiles) == 0, "profile_count": len(profiles)})


# registrations (e.g. a DIAx profile and a PFECRM profile) without retyping
# GUIDs or hunting through unrelated saved load configs. Never stores
# client_secret -- same policy as presets.

@app.route("/api/auth_profiles", methods=["GET"])
def api_auth_profiles_list():
    return jsonify(run_store.list_auth_profiles())


@app.route("/api/auth_profiles", methods=["POST"])
def api_auth_profiles_save():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    tenant_id = (body.get("tenant_id") or "").strip()
    client_id = (body.get("client_id") or "").strip()
    if not name or not tenant_id or not client_id:
        return jsonify({"error": "name, tenant_id, and client_id are required"}), 400
    profile = run_store.save_auth_profile(
        name, tenant_id, client_id, body.get("auth_mode", "pool"),
        body.get("note", ""), body.get("id"))
    return jsonify(profile)


@app.route("/api/auth_profiles/<profile_id>", methods=["GET"])
def api_auth_profiles_get(profile_id):
    p = run_store.get_auth_profile(profile_id)
    return (jsonify(p), 200) if p else (jsonify({"error": "not found"}), 404)


@app.route("/api/auth_profiles/<profile_id>", methods=["DELETE"])
def api_auth_profiles_delete(profile_id):
    run_store.delete_auth_profile(profile_id)
    return jsonify({"ok": True})


# ---- prompt sets (named, reusable question lists) --------------------------

@app.route("/api/prompt_sets", methods=["GET"])
def api_prompt_sets_list():
    return jsonify(run_store.list_prompt_sets())


@app.route("/api/prompt_sets", methods=["POST"])
def api_prompt_sets_save():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    prompts = [p for p in (body.get("prompts") or []) if isinstance(p, str) and p.strip()]
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not prompts:
        return jsonify({"error": "at least one prompt is required"}), 400
    pset = run_store.save_prompt_set(name, prompts, body.get("id"))
    return jsonify(pset)


@app.route("/api/prompt_sets/<set_id>", methods=["GET"])
def api_prompt_sets_get(set_id):
    p = run_store.get_prompt_set(set_id)
    return (jsonify(p), 200) if p else (jsonify({"error": "not found"}), 404)


@app.route("/api/prompt_sets/<set_id>", methods=["DELETE"])
def api_prompt_sets_delete(set_id):
    run_store.delete_prompt_set(set_id)
    return jsonify({"ok": True})


# ---- script sets (named groups of scripted multi-turn conversations) ------
# Mirrors the prompt_sets routes above, one level deeper: each saved "set" is
# a group of SCRIPTS, and each script is its own ordered list of turn
# prompts. Scripts are free to have different turn counts -- a real
# Copilot-Studio-exported test-case file naturally has test cases of varying
# length. Used by "Scripted sessions" mode, where each virtual user runs ONE
# WHOLE SCRIPT from the set start-to-finish.

@app.route("/api/script_sets", methods=["GET"])
def api_script_sets_list():
    return jsonify(run_store.list_script_sets())


@app.route("/api/script_sets", methods=["POST"])
def api_script_sets_save():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    raw_scripts = body.get("scripts") or []
    scripts = []
    for s in raw_scripts:
        turns = [t for t in (s or []) if isinstance(t, str) and t.strip()]
        if turns:
            scripts.append(turns)
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not scripts:
        return jsonify({"error": "at least one non-empty script is required"}), 400
    try:
        sset = run_store.save_script_set(name, scripts, body.get("id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(sset)


@app.route("/api/script_sets/<set_id>", methods=["GET"])
def api_script_sets_get(set_id):
    s = run_store.get_script_set(set_id)
    return (jsonify(s), 200) if s else (jsonify({"error": "not found"}), 404)


@app.route("/api/script_sets/<set_id>", methods=["DELETE"])
def api_script_sets_delete(set_id):
    run_store.delete_script_set(set_id)
    return jsonify({"ok": True})


@app.route("/api/script_sets/parse_csv", methods=["POST"])
def api_script_sets_parse_csv():
    """Parses test-case CSV exports into scripted multi-turn sessions. Two
    known export shapes are supported through the same endpoint:

    - New-harness export: one row per TURN, columns Test Case ID, Turn,
      Query (Expected Response, if present, is never read since this tool
      only simulates the user side of the conversation). Turn order comes
      from the explicit Turn column.
    - Standard-harness ("GeneralQuality" eval) export: a few leading
      "# ..." metadata/comment lines before the real header, then columns
      conversationNumber, question, response. There is no explicit turn
      column -- turn order is inferred from each row's position within its
      conversationNumber group (first-seen order), since the export always
      lists a conversation's turns in order.

    Rows are grouped by test-case/conversation id in first-seen order; a
    missing Turn column falls back to occurrence order instead of erroring.
    Scripts may end up with different turn counts, which is expected and
    allowed -- a real exported file naturally varies in length per test
    case. Uses Python's csv module (not a naive comma-split) so quoted
    fields containing commas are handled correctly, and matches column
    names case-insensitively with surrounding whitespace stripped since
    exports can vary slightly in header casing/spacing."""
    import csv
    import io
    body = request.json or {}
    raw = body.get("csv") or ""

    # Standard-harness exports prefix the real header with a few "# ..."
    # metadata lines (and a bare "#" separator line) -- strip those (and any
    # leading blank lines) so csv.DictReader sees the actual header row
    # first. Also strip a leading BOM, which Excel/PowerShell CSV exports
    # commonly include on the first line.
    text = raw.lstrip("\ufeff")
    lines = text.splitlines()
    start = 0
    while start < len(lines) and (not lines[start].strip() or lines[start].strip().lstrip('"').startswith("#")):
        start += 1
    text = "\n".join(lines[start:])

    try:
        reader = csv.DictReader(io.StringIO(text))
    except csv.Error as e:
        return jsonify({"error": f"Could not parse CSV: {e}"}), 400
    if not reader.fieldnames:
        return jsonify({"error": "CSV has no header row."}), 400
    # Case/whitespace-insensitive header lookup (e.g. "Test Case ID" vs
    # "test_case_id") so minor export formatting differences don't break
    # the import. Accepts either the new-harness column names or the
    # Standard-harness ("conversationNumber"/"question") equivalents.
    norm = {(h or "").strip().lower().replace(" ", "").replace("_", ""): h for h in reader.fieldnames}
    id_col = norm.get("testcaseid") or norm.get("conversationnumber")
    query_col = norm.get("query") or norm.get("question")
    turn_col = norm.get("turn")  # optional -- Standard-harness exports don't have one
    missing = [n for n, c in [("Test Case ID / conversationNumber", id_col),
                              ("Query / question", query_col)] if not c]
    if missing:
        return jsonify({"error": f"CSV is missing required column(s): {', '.join(missing)}. "
                                  f"Found columns: {', '.join(reader.fieldnames)}"}), 400

    groups: dict[str, list[tuple[int, str]]] = {}
    order: list[str] = []
    for i, row in enumerate(reader):
        tcid = (row.get(id_col) or "").strip()
        query = (row.get(query_col) or "").strip()
        if not tcid or not query:
            continue
        if turn_col:
            try:
                turn_num = int((row.get(turn_col) or "").strip())
            except ValueError:
                return jsonify({"error": f"Row {i+2}: Turn value \"{row.get(turn_col)}\" is not a number."}), 400
        else:
            # No explicit turn column -- use occurrence order within this
            # test case/conversation group (1-based) as the turn number.
            turn_num = len(groups.get(tcid, [])) + 1
        if tcid not in groups:
            groups[tcid] = []
            order.append(tcid)
        groups[tcid].append((turn_num, query))

    if not groups:
        return jsonify({"error": "No usable rows found in that CSV."}), 400
    scripts = [[q for _, q in sorted(groups[tcid], key=lambda t: t[0])] for tcid in order]
    return jsonify({"scripts": scripts})


if __name__ == "__main__":
    import socket
    import threading
    import time
    import webbrowser

    def _port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0

    if _port_in_use(5000):
        # Already running (e.g. the user double-clicked the exe a second
        # time) -- just surface the existing instance instead of crashing
        # with an unhelpful "Address already in use" traceback and no
        # console to read it from on a packaged exe.
        webbrowser.open("http://127.0.0.1:5000")
        raise SystemExit(0)

    def _open_browser_when_ready():
        # Give the Flask dev server a moment to actually bind the port before
        # opening the browser -- opening instantly (before the socket is
        # listening) would show a connection-refused page on a packaged exe
        # where there's no console output to reassure the user it's starting.
        time.sleep(1.2)
        webbrowser.open("http://127.0.0.1:5000")

    print("Agent Load Tester UI -> http://127.0.0.1:5000")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
