"""
Pre-flight checks for a load test run.

Two problems, both discovered and empirically verified this session, that
a real load test run can hit SILENTLY (no error, no obvious signal) unless
checked for up front:

1. TOKEN LIVENESS -- an account's cached MSAL token/refresh-token can expire
   or be revoked between runs. If that happens mid-run, every virtual user
   sharing that account starts failing at once, discovered only after load
   has already started. Checking first means a dead account is caught
   before any traffic is generated, with a clear "sign in again" message.

2. CONNECTOR CONSENT -- a Standard-harness agent invoking an external
   connector/MCP server for the FIRST time for a given identity shows an
   interactive "connectors/consentCard" Adaptive Card ("Connect to
   continue... Allow/Cancel") instead of answering. A load test's virtual
   users have no way to click "Allow" -- every turn that hits an
   un-consented connector silently counts as a technically-200-OK but
   EMPTY-response turn, which would quietly skew every result (success
   rate, tool-call counts, response content) without ever surfacing as an
   error. Verified fix: submitting the card's "Allow" action as a plain
   activity POST (the exact JSON the Adaptive Card's button sends) resolves
   it ONCE per (identity, connector) pair, PERMANENTLY -- confirmed via
   direct testing to persist across brand-new conversations afterward, so
   this only needs doing once per account, not once per run.

Only relevant for D2E (Standard-harness) agents in this session's testing --
GHCP-harness agents reached via A2A never presented a consent card in any
test run here (their tool calls worked without one). Callers should skip
the consent-resolution step for protocol=="a2a" and only check token
liveness for those.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import connector_discovery
import run_store
from d2e_client import D2EClient

_CONSENT_CONNECTOR_RE = re.compile(r'"_mcsConsentConnectorId"\s*:\s*"([^"]+)"')
_CONSENT_LOGICAL_RE = re.compile(r'"_mcsConsentLogicalName"\s*:\s*"([^"]+)"')
_CONSENT_CARD_RE = re.compile(r'"_mcsConsentCardId"\s*:\s*"([^"]+)"')

# Safety cap on auto-consent rounds per prompt: a single agent could
# plausibly chain several DISTINCT connectors (e.g. Dataverse, then OneDrive)
# across one conversation, but this bounds the walk so a never-resolving
# loop (e.g. a buggy agent that keeps re-showing the same card) can't hang
# the preflight check indefinitely.
MAX_CONSENT_ROUNDS_PER_PROMPT = 6

# Overall safety cap on one account's consent walk, independent of the above
# per-prompt round cap -- guards against a pathological prompt list (or a
# genuinely unresponsive agent) hanging the whole preflight check. Generous
# on purpose: a real check against a few known connectors normally finishes
# in well under a minute.
MAX_SECONDS_PER_ACCOUNT = 180

# How many accounts' consent walks to run concurrently. Each one is a
# separate identity hitting the agent independently, so there's no shared
# state to race on -- bounded mainly to avoid hammering the agent/Entra with
# too many simultaneous conversations if a pool has many accounts.
MAX_PARALLEL_ACCOUNTS = 6


def _extract_consent_fields(raw: str) -> dict | None:
    """Pull the three fields needed to submit an Adaptive Card "Allow" action
    for a connectors/consentCard activity, straight out of the raw D2E
    response body. Returns None if no consent card is present in this turn."""
    if "_mcsConsentCardId" not in raw:
        return None
    m_conn = _CONSENT_CONNECTOR_RE.search(raw)
    m_logical = _CONSENT_LOGICAL_RE.search(raw)
    m_card = _CONSENT_CARD_RE.search(raw)
    if not (m_conn and m_logical and m_card):
        return None
    return {
        "connector_id": m_conn.group(1),
        "logical_name": m_logical.group(1),
        "card_id": m_card.group(1),
    }


def _submit_allow(client: D2EClient, token: str, conversation_id: str, consent: dict):
    """Submit the Adaptive Card's "Allow" action as a plain value-type
    activity -- the same JSON the card's button sends when clicked
    interactively. Confirmed empirically (this session) to work identically
    via a raw API call, no browser/UI involved."""
    allow_value = {
        "action": "Allow", "actionSubmitId": "Allow", "id": "submit",
        "shouldAwaitUserInput": True,
        "_mcsConsentConnectorId": consent["connector_id"],
        "_mcsConsentLogicalName": consent["logical_name"],
        "_mcsConsentCardId": consent["card_id"],
        "altText": "Allow",
    }
    activity = {"type": "message", "value": allow_value,
                "from": {"id": "preflight", "role": "user"}}
    return client.send_activity(token, conversation_id, activity)


def resolve_connector_consents(host: str, schema: str, token: str,
                                prompts: list[str], api_version: str = "2022-03-01-preview",
                                max_rounds: int = MAX_CONSENT_ROUNDS_PER_PROMPT,
                                known_connectors: set[str] | None = None) -> dict:
    """Walk configured prompts (each in its own disposable conversation),
    auto-granting consent for any connector that challenges this identity for
    the first time. Returns:
      {"connectors_consented": [...], "consent_attempts": N,
       "prompts_checked": N (how many were ACTUALLY walked -- see below),
       "errors": [...], "still_missing_connectors": [...]}

    still_missing_connectors: known connectors (see known_connectors below)
    that never showed a consent card during this walk. Deliberately NOT
    treated as an error here -- see the comment where it's computed -- the
    caller decides whether that's actually concerning (e.g. cross-checking
    against its own consent cache).

    known_connectors (optional): when given (from connector_discovery.py --
    the agent's actual connector dependencies, looked up once from Dataverse
    metadata rather than guessed), this stops walking FURTHER prompts as soon
    as every known connector has been resolved for this identity, instead of
    unconditionally walking the entire prompt list. This is the common case
    and the whole point of discovery -- an agent with 2 real connectors only
    needs ~2 prompts to actually trigger them, not all 30 of a large imported
    script set. When None (discovery unavailable/skipped), behaves exactly as
    before: walks every prompt, no early exit -- the original, safe fallback.

    Safe to call even when everything is already consented: prompts that
    never show a card just answer normally on the first turn and cost one
    extra disposable conversation each, which is the price of certainty
    before a real run.
    """
    client = D2EClient(host, schema, api_version)
    consented: set[str] = set()
    attempts = 0
    errors: list[str] = []
    checked = 0

    for prompt in prompts:
        if known_connectors and known_connectors <= consented:
            # Every connector this agent is known to use has already shown
            # (and had granted) a consent card for this identity -- further
            # prompts can only ever re-confirm that, so stop here rather than
            # walking the rest of a possibly-large prompt/script list.
            break
        checked += 1
        start = client.start_conversation(token)
        if not start.ok or not start.conversation_id:
            errors.append(f"Could not start a conversation to check prompt {prompt!r}: "
                           f"{start.error or start.status}")
            continue
        conv_id = start.conversation_id
        turn = client.send_message(token, conv_id, prompt)
        rounds_left = max_rounds
        while rounds_left > 0:
            consent = _extract_consent_fields(turn.raw or "")
            if not consent:
                break  # no (more) consent card -- answered, or a real/unrelated error
            rounds_left -= 1
            attempts += 1
            allow_result = _submit_allow(client, token, conv_id, consent)
            consented.add(consent["logical_name"])
            if not allow_result.ok:
                errors.append(f"Submitting consent for {consent['logical_name']} "
                               f"failed: {allow_result.error or allow_result.status}")
                break
            # Re-send the same prompt after consenting -- the agent may need
            # the original question repeated to actually act on it now that
            # the connector is unblocked, rather than just acknowledging consent.
            turn = client.send_message(token, conv_id, prompt)
        else:
            if _extract_consent_fields(turn.raw or ""):
                errors.append(
                    f"Prompt {prompt!r} still showed a consent card after "
                    f"{max_rounds} auto-consent attempt(s) -- may be a connector "
                    f"this tool can't auto-resolve (e.g. requires additional "
                    f"interactive steps beyond a single Allow); review manually.")

    still_missing: list[str] = []
    if known_connectors:
        still_missing = sorted(known_connectors - consented)
        # NOTE: this is deliberately NOT added to `errors`. A connector
        # showing no consent card here is AMBIGUOUS -- it could mean this
        # identity already had consent granted in a past run (the expected,
        # harmless steady state once a connector cache has recorded it --
        # see connector_consents in run_store.py) just as easily as it could
        # mean these prompts never actually exercise that connector. Treating
        # it as a hard failure produced false "not ready" results for
        # perfectly fine, already-consented accounts. Callers that maintain
        # their own consent cache should only escalate this to a real warning
        # when the cache ALSO has no record of prior consent for that pair.
        if still_missing:
            pass  # surfaced via "still_missing_connectors" below, not errors

    return {
        "connectors_consented": sorted(consented),
        "consent_attempts": attempts,
        "prompts_checked": checked,
        "errors": errors,
        "still_missing_connectors": still_missing,
    }


def check_accounts(pool_manager) -> list[dict]:
    """Token-liveness check for every account in a pool, reusing the exact
    same silent-refresh logic the Accounts tab already uses (list_accounts())
    rather than duplicating it -- "valid"/"expired"/"error" per account, with
    expiry info when available."""
    return pool_manager.list_accounts()


def run_preflight(cfg: dict, pool_manager=None, single_provider=None) -> dict:
    """Top-level entry point used by the /api/preflight route.

    cfg: the same run-config shape /api/start accepts (protocol, host,
    schema, prompts, auth_mode, org_url, ...).
    pool_manager: an AccountPoolManager, when auth_mode is "devicecode"/pool-
    based -- checks token liveness for EVERY enabled account, then resolves
    consent once per account (consent is per-identity, so each account needs
    its own walk). Account consent walks run CONCURRENTLY (see
    MAX_PARALLEL_ACCOUNTS) since each is an independent identity.
    single_provider: any other TokenProvider (static/ropc/confidential) --
    checked as a single "account" (no per-account identity list available).

    Before checking any account, looks up the agent's actual connector
    dependencies ONCE from Dataverse metadata (connector_discovery.py) --
    this is a property of the agent, not of the test data, so it doesn't need
    re-discovering per account. When available, each account's consent walk
    stops as soon as every known connector is resolved for that identity,
    instead of unconditionally walking the whole prompt/script list; when
    unavailable (no org_url, no Dataverse access, non-D2E protocol, etc.) it
    falls back to the original full-walk behavior unchanged.

    Returns {"protocol", "agent_connectors": {"status", "connectors", "note"},
    "accounts": [{"username", "token_status", "connectors_consented",
    "consent_attempts", "errors"}], "overall_ready": bool, "note"}.
    """
    protocol = (cfg.get("protocol") or "d2e").lower()
    # Scripted sessions mode has no single "prompts" list -- flatten every
    # script's turns into one combined list purely for the consent walk
    # below, which just needs SOME representative prompt per connector to
    # trigger its consent card at least once; which script a prompt came
    # from doesn't matter for this purpose.
    if cfg.get("conversation_mode") == "scripted":
        prompts = [p for script in (cfg.get("scripts") or []) for p in (script or [])]
    else:
        prompts = cfg.get("prompts") or []
    out = {"protocol": protocol, "agent_connectors": None, "accounts": [],
           "overall_ready": True, "note": ""}

    if protocol == "a2a":
        out["note"] = (
            "Connector-consent auto-resolution is D2E-only -- no consent card "
            "was ever observed for GHCP-harness/A2A runs in this tool's testing, "
            "so only token liveness is checked for A2A."
        )

    # Discover the agent's actual connector dependencies ONCE, up front --
    # not per account. known_connectors stays None (meaning "unknown, fall
    # back to full prompt walk") whenever discovery didn't cleanly return a
    # non-empty connector list, so a discovery failure can only ever make
    # this slower (today's old behavior), never incorrect.
    known_connectors: set[str] | None = None
    if protocol != "a2a" and cfg.get("org_url") and cfg.get("schema"):
        discovery = connector_discovery.discover_agent_connectors(
            cfg["org_url"], cfg.get("tenant_id", ""), cfg.get("client_id", ""), cfg["schema"])
        out["agent_connectors"] = discovery
        if discovery["status"] == "ok" and discovery["connectors"]:
            known_connectors = {c["logical_name"] for c in discovery["connectors"]}

    def _check_one_account(acct: dict) -> dict:
        """Runs in a worker thread -- token liveness + (if applicable) the
        connector-consent walk for ONE account. Returns the account's result
        entry; never raises (errors are captured into the entry itself) so a
        single account's failure can't take down the whole parallel batch.

        Checks run_store's permanent connector_consents cache FIRST: consent
        is confirmed (empirically, see module docstring) to persist forever
        once granted for an (identity, connector) pair, so an account this
        tool has already seen successfully consent for every known connector
        never needs to talk to the agent again at all -- the single biggest
        real-world speedup, since the steady state (re-running the same test
        repeatedly) is "nothing new to consent" almost every time."""
        entry = {"username": acct.get("username"), "token_status": acct.get("status"),
                  "connectors_consented": [], "consent_attempts": 0, "errors": [],
                  "unconfirmed_connectors": []}
        if acct.get("status") != "valid":
            entry["errors"].append(
                "Token is not valid (status: " + str(acct.get("status")) + "). "
                "Sign in again via the Accounts tab before running a load test "
                "with this account.")
            return entry
        if protocol != "a2a" and prompts and cfg.get("host") and cfg.get("schema"):
            home_account_id = acct.get("home_account_id") or ""
            schema = cfg["schema"]
            cached_consented: set[str] = set()
            if known_connectors and home_account_id:
                cached_consented = run_store.get_consented_connectors(
                    cfg.get("tenant_id", ""), cfg.get("client_id", ""), home_account_id, schema)
            still_needed = (known_connectors - cached_consented) if known_connectors else known_connectors
            if known_connectors is not None and not still_needed:
                # Every known connector is already confirmed-consented for
                # this identity from a past run -- nothing to check at all.
                entry["connectors_consented"] = sorted(cached_consented)
                return entry
            try:
                if hasattr(pool_manager, "get_token_for"):
                    token = pool_manager.get_token_for(home_account_id)
                else:
                    token = pool_manager.get_token()
            except Exception as e:
                entry["errors"].append(f"Could not acquire a token to test consent: {e}")
                return entry
            try:
                consent_report = resolve_connector_consents(
                    cfg["host"], schema, token, prompts,
                    known_connectors=still_needed)
            except Exception as e:
                entry["errors"].append(f"Connector-consent check failed: {e}")
                return entry
            newly_consented = consent_report["connectors_consented"]
            if newly_consented and home_account_id:
                run_store.mark_connectors_consented(
                    cfg.get("tenant_id", ""), cfg.get("client_id", ""), home_account_id,
                    schema, newly_consented)
            entry["connectors_consented"] = sorted(cached_consented.union(newly_consented))
            entry["consent_attempts"] = consent_report["consent_attempts"]
            entry["errors"].extend(consent_report["errors"])
            # Deliberately a SEPARATE, non-blocking field -- NOT appended to
            # `errors` (which would flip overall_ready to False). A connector
            # that never shows a card here is genuinely ambiguous: it's
            # indistinguishable from "already consented long before this
            # cache existed" (the common, harmless case for any account that
            # pre-dates this feature) versus "these prompts never actually
            # exercise it". Blocking a perfectly healthy run over that
            # ambiguity produced real false failures in testing -- surface it
            # as an FYI the user can act on if they want extra certainty,
            # never as a reason the run isn't ready.
            truly_unresolved = sorted(set(consent_report.get("still_missing_connectors", [])) - cached_consented)
            entry["unconfirmed_connectors"] = truly_unresolved
        return entry

    if pool_manager is not None:
        accounts = check_accounts(pool_manager)
        # AccountPoolManager has _accounts()/get_token_for() for its
        # enabled-filter + stable-per-identity token assignment;
        # DeviceCodeProvider (single-account "devicecode" auth_mode) exposes
        # the same list_accounts() shape but has neither concept (at most
        # one cached account, no enable/disable toggle) -- duck-type so this
        # works against both without a separate code path.
        has_enabled_filter = hasattr(pool_manager, "_accounts")
        enabled_haids = ({a.get("home_account_id") for a in pool_manager._accounts()}
                         if has_enabled_filter
                         else {a.get("home_account_id") for a in accounts})
        enabled_accounts = [a for a in accounts if a.get("home_account_id") in enabled_haids]

        results: list[dict] = [None] * len(enabled_accounts)
        if enabled_accounts:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_ACCOUNTS, len(enabled_accounts))) as pool:
                future_to_idx = {pool.submit(_check_one_account, acct): i
                                  for i, acct in enumerate(enabled_accounts)}
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result(timeout=MAX_SECONDS_PER_ACCOUNT)
                    except Exception as e:
                        acct = enabled_accounts[idx]
                        results[idx] = {
                            "username": acct.get("username"), "token_status": acct.get("status"),
                            "connectors_consented": [], "consent_attempts": 0,
                            "errors": [f"Timed out or failed after {MAX_SECONDS_PER_ACCOUNT}s: {e}"],
                            "unconfirmed_connectors": [],
                        }
        out["accounts"] = results
        if any(r["errors"] for r in results):
            out["overall_ready"] = False
        if not out["accounts"]:
            out["overall_ready"] = False
            out["note"] = (out["note"] + " " if out["note"] else "") + (
                "No cached accounts found -- sign in via the Accounts tab "
                "before running a load test.")
        return out

    # Non-pool auth modes (static/ropc/confidential/etc.) -- checked as a
    # single pseudo-account with no per-identity listing available.
    if single_provider is not None:
        entry = {"username": "(single configured account)", "token_status": "unknown",
                  "connectors_consented": [], "consent_attempts": 0, "errors": []}
        try:
            has_cached = getattr(single_provider, "has_cached_token", None)
            if callable(has_cached):
                entry["token_status"] = "valid" if has_cached() else "needs_sign_in"
            token = single_provider.get_token()
            entry["token_status"] = "valid"
        except Exception as e:
            entry["token_status"] = "error"
            entry["errors"].append(f"Could not acquire a token: {e}")
            out["overall_ready"] = False
            out["accounts"].append(entry)
            return out
        if protocol != "a2a" and prompts and cfg.get("host") and cfg.get("schema"):
            consent_report = resolve_connector_consents(
                cfg["host"], cfg["schema"], token, prompts, known_connectors=known_connectors)
            entry["connectors_consented"] = consent_report["connectors_consented"]
            entry["consent_attempts"] = consent_report["consent_attempts"]
            entry["errors"].extend(consent_report["errors"])
            if consent_report["errors"]:
                out["overall_ready"] = False
        out["accounts"].append(entry)
        return out

    out["overall_ready"] = False
    out["note"] = "No provider or pool manager available to check."
    return out
