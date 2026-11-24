"""
Phase A ramp runner — walks the GENERATIVE request rate through the environment throttle
to find the onset point, using a thread pool on a single box.

It holds a series of target rates (RPM), each for a fixed duration, and records every
request (timestamp, step, status, latency, throttled, error) to a CSV. A per-step summary
prints the success rate and p50/p95 latency so you can see exactly where throttling begins.

This is characterization, not the 2,000-session event test. One identity is sufficient
here because the generative quota is enforced PER ENVIRONMENT, not per user.

Usage:
  python ramp.py --config config.json
  python ramp.py --token "<bearer>" --steps 10,30,50,70,100,150 --hold 90
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import queue
import random
import statistics
import sys
import threading
import time

from d2e_client import D2EClient
from token_provider import DeviceCodeProvider, RopcProvider, StaticTokenProvider

HERE = pathlib.Path(__file__).parent


def make_provider(cfg, token_override):
    if token_override:
        return StaticTokenProvider(token_override)
    mode = cfg.get("auth_mode", "devicecode").lower()
    if mode == "static":
        return StaticTokenProvider(cfg["static_token"])
    if mode == "ropc":
        a = cfg["accounts"][0]
        return RopcProvider(cfg["tenant_id"], cfg["client_id"], a["username"], a["password"])
    return DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"])


class Recorder:
    def __init__(self, path):
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(["ts", "step_rpm", "status", "latency_ms", "throttled", "ok", "error"])
        self._lock = threading.Lock()
        self.rows = []

    def record(self, step_rpm, r):
        with self._lock:
            self._w.writerow([f"{time.time():.3f}", step_rpm, r.status,
                              f"{r.latency_ms:.0f}", int(r.throttled), int(r.ok),
                              (r.error or "")[:120]])
            self._f.flush()
            self.rows.append((step_rpm, r))

    def close(self):
        self._f.close()


def worker(client, provider, conv_cache, prompts, step_rpm, rec, stop_evt, user_id):
    """Each worker: ensure a conversation, send a generative turn, repeat as scheduled."""
    token = provider.get_token()
    conv_id = conv_cache.get(user_id)
    if conv_id is None:
        start = client.start_conversation(token)
        conv_id = start.conversation_id
        conv_cache[user_id] = conv_id
        if conv_id is None:
            rec.record(step_rpm, start)
            return
    prompt = random.choice(prompts)
    r = client.send_message(token, conv_id, prompt, user_id=user_id)
    rec.record(step_rpm, r)


def run_step(client, provider, prompts, step_rpm, hold_s, rec, pool_size):
    """Fire `step_rpm` requests/min for hold_s seconds using a bounded thread pool."""
    interval = 60.0 / step_rpm
    deadline = time.time() + hold_s
    conv_cache = {}
    threads = []
    sem = threading.BoundedSemaphore(pool_size)
    n = 0
    stop_evt = threading.Event()

    def launch(uid):
        try:
            worker(client, provider, conv_cache, prompts, step_rpm, rec, stop_evt, uid)
        finally:
            sem.release()

    while time.time() < deadline:
        sem.acquire()
        n += 1
        t = threading.Thread(target=launch, args=(f"u{n % max(pool_size,1)}",), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(interval)
    for t in threads:
        t.join(timeout=client.timeout + 5)


def summarize(step_rpm, rows):
    sub = [r for s, r in rows if s == step_rpm]
    if not sub:
        return
    oks = [r for r in sub if r.ok]
    thr = [r for r in sub if r.throttled]
    lat = sorted(r.latency_ms for r in sub)
    p50 = statistics.median(lat) if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    print(f"  step {step_rpm:>4} RPM | sent={len(sub):>4} ok={len(oks):>4} "
          f"throttled={len(thr):>4} success={100*len(oks)/len(sub):5.1f}% "
          f"p50={p50:6.0f}ms p95={p95:6.0f}ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--token", default=None)
    ap.add_argument("--steps", default="10,30,50,70,100,150",
                    help="Comma list of target RPM steps")
    ap.add_argument("--hold", type=int, default=90, help="Seconds to hold each step")
    ap.add_argument("--pool", type=int, default=200, help="Max concurrent in-flight requests")
    args = ap.parse_args()

    cfg = {}
    if not args.token:
        p = pathlib.Path(args.config)
        if not p.exists():
            print(f"[!] {args.config} not found; pass --token or create config.json", file=sys.stderr)
            sys.exit(2)
        cfg = json.loads(p.read_text())
    else:
        cfg = {"host": "<ENVIRONMENT_HOST>.environment.api.powerplatform.com",
               "schema": "<AGENT_SCHEMA_NAME>",
               "prompts": ["What is the standard operating procedure for onboarding?"]}

    provider = make_provider(cfg, args.token)
    client = D2EClient(cfg["host"], cfg["schema"], cfg.get("api_version", "2022-03-01-preview"))
    prompts = cfg.get("prompts", ["Hello"])
    steps = [int(x) for x in args.steps.split(",")]

    out = HERE / f"ramp-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    rec = Recorder(out)
    print(f"[*] Priming token ...")
    provider.get_token()
    print(f"[*] Ramp: steps={steps} hold={args.hold}s pool={args.pool}")
    print(f"[*] Logging -> {out.name}\n")

    try:
        for step in steps:
            print(f"--- holding {step} RPM for {args.hold}s ---")
            run_step(client, provider, prompts, step, args.hold, rec, args.pool)
            summarize(step, rec.rows)
    finally:
        rec.close()

    print("\n=== Ramp complete ===")
    for step in steps:
        summarize(step, rec.rows)
    print(f"\nRaw per-request data: {out}")
    print("Throttle onset = first step where success% drops / throttled>0.")


if __name__ == "__main__":
    main()
