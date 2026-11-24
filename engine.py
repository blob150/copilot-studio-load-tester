"""
Load engine: models virtual users running multi-turn sessions against the D2E agent,
with an event-spike load shape (ramp-up -> hold -> ramp-down) or a stepped RPM profile.
Feeds a StatsAggregator live and is start/stoppable from the UI.
"""
from __future__ import annotations

import random
import threading
import time

from a2a_client import A2AClient
from d2e_client import D2EClient
from stats import StatsAggregator


class LoadEngine:
    def __init__(self, config: dict, provider, stats: StatsAggregator):
        self.cfg = config
        self.provider = provider
        self.stats = stats
        timeout = float(config.get("request_timeout", 120))
        protocol = (config.get("protocol") or "d2e").lower()
        self.protocol = protocol
        if protocol == "a2a":
            # a2a_url is the full agent base URL, e.g.
            # https://api.powerplatform.com/copilotstudio-a2a/tenants/{t}/
            # environments/{e}/agents/{a}  (see app.py parse_a2a_url)
            self.client = A2AClient(config["a2a_url"], timeout=timeout)
        else:
            self.client = D2EClient(config["host"], config["schema"],
                                    config.get("api_version", "2022-03-01-preview"),
                                    timeout=timeout)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active = 0
        self._active_lock = threading.Lock()

    # ---- control ----------------------------------------------------------
    def stop(self):
        self._stop.set()

    def is_stopped(self) -> bool:
        return self._stop.is_set()

    # ---- a single virtual user session ------------------------------------
    def _get_token(self, session_key: str) -> str:
        """Use a stable per-session account when the provider supports pooling
        (AccountPoolManager.get_token_for), so one conversation keeps one identity
        throughout. Falls back to the plain get_token() for single-account providers."""
        getter = getattr(self.provider, "get_token_for", None)
        return getter(session_key) if getter else self.provider.get_token()

    def _account_for(self, session_key: str) -> str | None:
        """Real pooled-account username backing this session, for attribution
        in run history (the "By account" breakdown) -- separate from
        session_key itself, which is just the synthetic virtual-user label
        (e.g. "vu3", "smoke0") and was previously the ONLY identity info ever
        recorded anywhere. Uses the same deterministic selection as
        _get_token() so it always matches the account that was actually
        used. None for single-account providers (static/devicecode/
        confidential-client) -- those have no per-session account to report,
        the one identity is implied by the whole run's config."""
        getter = getattr(self.provider, "username_for", None)
        return getter(session_key) if getter else None

    def _session(self, user_id: str):
        with self._active_lock:
            self._active += 1
            self.stats.set_active(self._active)
        account = self._account_for(user_id)
        try:
            token = self._get_token(user_id)
            start = self.client.start_conversation(token)
            start.user_id = user_id
            start.account = account
            self.stats.record("start", start)
            if not start.conversation_id or self._stop.is_set():
                return
            conv = start.conversation_id
            # Scripted mode: each session is assigned ONE WHOLE SCRIPT (a
            # fully ordered list of turn prompts), picked at random from the
            # configured set. Scripts may have different turn counts (e.g.
            # imported from a Copilot Studio test-case export, where test
            # cases naturally vary in length) -- this session's turn count is
            # simply whatever its chosen script has. Pool mode (default) is
            # completely unchanged below.
            scripted = (self.cfg.get("conversation_mode") == "scripted")
            if scripted:
                scripts = [s for s in (self.cfg.get("scripts") or []) if s]
                turn_prompts = random.choice(scripts) if scripts else ["Hello"]
                sequential = True  # a script is always run in its own order
            else:
                turn_prompts = self.cfg.get("prompts") or ["Hello"]
                sequential = (self.cfg.get("prompt_order") == "sequential")
            turns = len(turn_prompts) if scripted else int(self.cfg.get("turns_per_session", 4))
            tmin, tmax = self.cfg.get("think_time_seconds", [5, 15])
            for turn_idx in range(turns):
                if self._stop.is_set():
                    break
                token = self._get_token(user_id)
                # Sequential: turn N uses prompts[N] in order, scripting a real
                # multi-turn conversation (e.g. a follow-up question that only
                # makes sense after the first) -- wraps around if there are
                # more turns than prompt lines. Random (default): independently
                # sample a prompt each turn, as before.
                prompt_text = turn_prompts[turn_idx % len(turn_prompts)] if sequential \
                    else random.choice(turn_prompts)
                r = self.client.send_message(token, conv, prompt_text,
                                             user_id=user_id)
                r.user_id = user_id
                r.account = account
                r.prompt = prompt_text
                self.stats.record("turn", r)
                if self.cfg.get("stop_on_throttle") and r.throttled:
                    self._stop.set()
                    break
                # Think time paces the gap BETWEEN turns, emulating a real
                # user reading/typing before their next message -- it has no
                # purpose after the LAST turn (there is no next turn to pace),
                # so sleeping here anyway just adds dead time with nothing
                # recorded, inflating total run duration well past when the
                # chart's last real data point is (e.g. an 8s test with 3x
                # single-turn sessions was previously reporting ~41s, because
                # every session still slept a full random 15-45s "think"
                # after its one and only turn before the thread exited).
                if turn_idx < turns - 1:
                    think = random.uniform(float(tmin), float(tmax))
                    end = time.time() + think
                    while time.time() < end and not self._stop.is_set():
                        time.sleep(0.1)
        finally:
            with self._active_lock:
                self._active -= 1
                self.stats.set_active(self._active)

    # ---- load shapes ------------------------------------------------------
    def _spawn(self, user_id: str):
        t = threading.Thread(target=self._session, args=(user_id,), daemon=True)
        t.start()
        self._threads.append(t)

    def run(self):
        """Event-spike shape: ramp-up to target VUs, hold, ramp-down. Each VU loops
        new sessions until its phase ends or max duration / stop is hit."""
        self.stats.begin()
        target = int(self.cfg.get("virtual_users", 50))
        rampup = float(self.cfg.get("rampup_seconds", 30))
        hold = float(self.cfg.get("hold_seconds", 120))
        rampdown = float(self.cfg.get("rampdown_seconds", 15))
        max_sessions_per_vu = int(self.cfg.get("sessions_per_vu", 0))  # 0 = unlimited

        t0 = time.time()
        uid = 0
        # Mirror ramp-up's stagger for ramp-down too: without this, EVERY VU
        # shared the exact same global cutoff (t0+rampup+hold+rampdown), so
        # concurrency stayed pinned at the full target through the whole
        # hold+ramp-down window and then every session hard-stopped at the
        # same instant -- a cliff, not a gradual ramp-down (the "ramp-down"
        # phase label was purely cosmetic; self._stop_after was computed but
        # never actually read anywhere). Giving VU i its own cutoff,
        # staggered across the ramp-down window in the same order they were
        # started, makes active-session count actually decline gradually,
        # matching what the chart/label imply.
        rampdown_slot = (rampdown / target) if (rampdown > 0 and target > 0) else 0

        # Each "virtual user" is a long-lived loop launching sessions back-to-back.
        def vu_loop(vu_index):
            launched = 0
            vu_cutoff = t0 + rampup + hold + (vu_index + 1) * rampdown_slot
            while not self._stop.is_set():
                # respect ramp schedule: a VU only starts once its slot opens
                self._session(f"vu{vu_index}")
                launched += 1
                if max_sessions_per_vu and launched >= max_sessions_per_vu:
                    break
                if time.time() > vu_cutoff:
                    break

        # Ramp-up: stagger VU starts across rampup window
        self.stats.set_phase("ramp-up")
        vu_threads = []
        for i in range(target):
            if self._stop.is_set():
                break
            t = threading.Thread(target=vu_loop, args=(i,), daemon=True)
            t.start()
            vu_threads.append(t)
            if rampup > 0 and target > 1:
                time.sleep(rampup / target)

        # Hold
        self.stats.set_phase("hold")
        hold_end = t0 + rampup + hold
        while time.time() < hold_end and not self._stop.is_set():
            time.sleep(0.2)

        # Ramp-down: VUs self-terminate on their own staggered cutoff above;
        # this just waits out the window and then does a final hard stop as
        # a safety net for anything still mid-session past the window.
        self.stats.set_phase("ramp-down")
        down_end = time.time() + rampdown
        while time.time() < down_end and not self._stop.is_set():
            time.sleep(0.2)

        self._stop.set()
        for t in vu_threads:
            t.join(timeout=self.client.timeout + 5)
        self.stats.finish()

    def run_smoke(self):
        """Smoke test: fire a small, fixed number of sessions concurrently, once,
        and stop. No ramping, no RPM targeting -- just a quick 'does this actually
        work end-to-end' check before committing to a real load run."""
        self.stats.begin()
        self.stats.set_phase("smoke")
        n_sessions = int(self.cfg.get("smoke_sessions", 3))
        threads = []
        for i in range(n_sessions):
            if self._stop.is_set():
                break
            t = threading.Thread(target=self._session, args=(f"smoke{i}",), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=self.client.timeout + 5)
        self._stop.set()
        self.stats.finish()

    def run_stepped(self):
        """Stepped RPM profile: hold each target RPM for a duration (characterization)."""
        self.stats.begin()
        steps = self.cfg.get("steps", [20, 40, 60])
        hold = float(self.cfg.get("step_hold_seconds", 60))
        pool = int(self.cfg.get("max_concurrency", 100))
        sem = threading.BoundedSemaphore(pool)
        uid = 0
        for step in steps:
            if self._stop.is_set():
                break
            self.stats.set_phase(f"{step} RPM")
            interval = 60.0 / max(step, 1)
            end = time.time() + hold
            while time.time() < end and not self._stop.is_set():
                sem.acquire()
                uid += 1

                def one(u):
                    try:
                        self._session(f"u{u}")
                    finally:
                        sem.release()
                threading.Thread(target=one, args=(uid,), daemon=True).start()
                time.sleep(interval)
        self._stop.set()
        time.sleep(1)
        self.stats.finish()
