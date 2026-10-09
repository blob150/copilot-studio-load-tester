"""Thread-safe live statistics aggregation for the load engine."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0.0
    k = int(round((pct / 100.0) * (len(sorted_vals) - 1)))
    return sorted_vals[k]


class StatsAggregator:
    """Collects per-request outcomes and exposes live aggregate snapshots."""

    def __init__(self, bucket_seconds: int = 2, on_record=None):
        self._lock = threading.Lock()
        self.start_ts = None
        self.bucket_seconds = bucket_seconds
        self.on_record = on_record   # optional callback(kind, result) for persistence

        self.total = 0
        self.ok = 0
        self.errors = 0
        self.throttled = 0
        self.tool_calls = 0
        self.knowledge_hits = 0
        self.tool_names = defaultdict(int)
        self.conversation_ids = []
        self.by_status = defaultdict(int)
        self.by_kind = defaultdict(lambda: {"count": 0, "ok": 0, "latencies": []})
        self.latencies = []          # all latencies (ms)
        self._time_buckets = defaultdict(lambda: {"count": 0, "ok": 0,
                                                   "throttled": 0, "errors": 0,
                                                   "latencies": []})
        self.recent = deque(maxlen=50)   # recent events for the live feed
        self._seen_convs = set()
        self.active_sessions = 0
        self.phase = "idle"
        self.running = False

    # ---- lifecycle --------------------------------------------------------
    def begin(self):
        with self._lock:
            self.start_ts = time.time()
            self.running = True
            self.phase = "starting"

    def finish(self):
        with self._lock:
            self.running = False
            self.phase = "complete"

    def set_phase(self, phase: str):
        with self._lock:
            self.phase = phase

    def set_active(self, n: int):
        with self._lock:
            self.active_sessions = n

    def inc_active(self, delta: int):
        with self._lock:
            self.active_sessions = max(0, self.active_sessions + delta)

    # ---- recording --------------------------------------------------------
    def record(self, kind: str, result):
        """kind: 'start' or 'turn'. result: d2e_client.TurnResult."""
        now = time.time()
        if self.on_record:
            try:
                self.on_record(kind, result)
            except Exception:
                pass
        with self._lock:
            self.total += 1
            self.by_status[str(result.status)] += 1
            if result.throttled:
                self.throttled += 1
            if result.ok:
                self.ok += 1
            else:
                self.errors += 1

            self.tool_calls += getattr(result, "tool_calls", 0)
            self.knowledge_hits += getattr(result, "knowledge_hits", 0)
            for tn, cnt in (getattr(result, "tools", None) or {}).items():
                self.tool_names[tn] += cnt
            if result.conversation_id and result.conversation_id not in self._seen_convs:
                self._seen_convs.add(result.conversation_id)
                self.conversation_ids.append(result.conversation_id)

            k = self.by_kind[kind]
            k["count"] += 1
            if result.ok:
                k["ok"] += 1
            k["latencies"].append(result.latency_ms)

            self.latencies.append(result.latency_ms)

            rel = int((now - (self.start_ts or now)) // self.bucket_seconds) * self.bucket_seconds
            b = self._time_buckets[rel]
            b["count"] += 1
            b["latencies"].append(result.latency_ms)
            if result.throttled:
                b["throttled"] += 1
            if result.ok:
                b["ok"] += 1
            else:
                b["errors"] += 1

            self.recent.appendleft({
                "t": round(now - (self.start_ts or now), 1),
                "kind": kind,
                "status": result.status,
                "latency_ms": round(result.latency_ms),
                "throttled": result.throttled,
                "ok": result.ok,
                "error": (result.error or "")[:160],
            })

    # ---- snapshot ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            elapsed = (time.time() - self.start_ts) if self.start_ts else 0
            lat_sorted = sorted(self.latencies)
            # achieved RPM over the last 60s of buckets
            recent_cut = elapsed - 60
            recent_count = sum(
                b["count"] for rel, b in self._time_buckets.items() if rel >= recent_cut
            )
            window = min(60, max(elapsed, 1e-9))
            achieved_rpm = recent_count / window * 60.0

            series = []
            for rel in sorted(self._time_buckets.keys()):
                b = self._time_buckets[rel]
                ls = sorted(b["latencies"])
                series.append({
                    "t": rel,
                    "count": b["count"],
                    "rpm": b["count"] / self.bucket_seconds * 60.0,
                    "ok": b["ok"],
                    "errors": b["errors"],
                    "throttled": b["throttled"],
                    "p50": round(_percentile(ls, 50)),
                    "p95": round(_percentile(ls, 95)),
                })

            kinds = {}
            for name, k in self.by_kind.items():
                ls = sorted(k["latencies"])
                kinds[name] = {
                    "count": k["count"], "ok": k["ok"],
                    "p50": round(_percentile(ls, 50)),
                    "p95": round(_percentile(ls, 95)),
                }

            return {
                "running": self.running,
                "phase": self.phase,
                "elapsed": round(elapsed, 1),
                "active_sessions": self.active_sessions,
                "total": self.total,
                "ok": self.ok,
                "errors": self.errors,
                "throttled": self.throttled,
                "success_rate": round(100.0 * self.ok / self.total, 1) if self.total else 0.0,
                "achieved_rpm": round(achieved_rpm, 1),
                "p50": round(_percentile(lat_sorted, 50)),
                "p95": round(_percentile(lat_sorted, 95)),
                "p99": round(_percentile(lat_sorted, 99)),
                "by_status": dict(self.by_status),
                "by_kind": kinds,
                "tool_calls": self.tool_calls,
                "knowledge_hits": self.knowledge_hits,
                "tool_names": dict(self.tool_names),
                "conversation_count": len(self.conversation_ids),
                "conversation_ids": list(self.conversation_ids),
                "series": series,
                "recent": list(self.recent),
            }
