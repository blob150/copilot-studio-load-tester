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

    def _session(self, user_id: str):
        with self._active_lock:
            self._active += 1
            self.stats.set_active(self._active)
        try:
            token = self._get_token(user_id)
            start = self.client.start_conversation(token)
            start.user_id = user_id
            self.stats.record("start", start)
            if not start.conversation_id or self._stop.is_set():
                return
            conv = start.conversation_id
            prompts = self.cfg.get("prompts") or ["Hello"]
            turns = int(self.cfg.get("turns_per_session", 4))
            tmin, tmax = self.cfg.get("think_time_seconds", [5, 15])
            sequential = (self.cfg.get("prompt_order") == "sequential")
            for turn_idx in range(turns):
                if self._stop.is_set():
                    break
                token = self._get_token(user_id)
                # Sequential: turn N uses prompts[N] in order, scripting a real
                # multi-turn conversation (e.g. a follow-up question that only
                # makes sense after the first) -- wraps around if there are
                # more turns than prompt lines. Random (default): independently
                # sample a prompt each turn, as before.
                prompt_text = prompts[turn_idx % len(prompts)] if sequential \
                    else random.choice(prompts)
                r = self.client.send_message(token, conv, prompt_text,
                                             user_id=user_id)
                r.user_id = user_id
                r.prompt = prompt_text
                self.stats.record("turn", r)
                if self.cfg.get("stop_on_throttle") and r.throttled:
                    self._stop.set()
                    break
                # think time
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
        # Each "virtual user" is a long-lived loop launching sessions back-to-back.
        def vu_loop(vu_index):
            launched = 0
            while not self._stop.is_set():
                # respect ramp schedule: a VU only starts once its slot opens
                self._session(f"vu{vu_index}")
                launched += 1
                if max_sessions_per_vu and launched >= max_sessions_per_vu:
                    break
                if time.time() - t0 > (rampup + hold + rampdown):
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

        # Ramp-down
        self.stats.set_phase("ramp-down")
        self._stop_after = time.time() + rampdown
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
