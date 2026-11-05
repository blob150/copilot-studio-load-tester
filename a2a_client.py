"""
Copilot Studio A2A conversation client — alternative to D2EClient, same
TurnResult/stats contract so it drops into engine.py/stats.py/run_store.py
unchanged.

Wire format (empirically discovered against the PFECRM tenant's
`crc13_earlytest_FRyDFI` agent — see plan.md "2026-11-05: PFECRM
authenticated conversation" for the full investigation notes):

  POST {base_url}
    Authorization: Bearer <token>   (scope: .../copilotstudio-a2a/MCS.InvokeAsA2A)
    Content-Type: application/json
    Accept: application/json, text/event-stream
    Body (proto-JSON transcoded, NOT the generic A2A spec's lower-case
    method names -- `message/send` returns -32601 here):
      {"jsonrpc":"2.0","id":"<guid>","method":"SendStreamingMessage",
       "params":{"message":{"messageId":"<guid>","role":"ROLE_USER",
                 "parts":[{"text":"..."}],
                 "contextId":"<guid>"   # omit to start a new conversation
                 },
                 "configuration":{"acceptedOutputModes":["text/plain"]}}}

  Response is `text/event-stream` (SSE `data:` lines), one JSON-RPC envelope
  per event: a `task` snapshot, zero or more `artifactUpdate` chunks, a
  `statusUpdate`, finishing with `task.status.state == TASK_STATE_COMPLETED`.
  The non-streaming `SendMessage` method also works (blocking single
  `application/json` response with the same shapes embedded) but has no
  early partial output -- not used here since streaming is what the UI
  wants to characterize (closer to real end-user perceived latency).

Error shapes observed (see plan.md for the full matrix):
  - 401 (bad/missing token): plain text body + WWW-Authenticate header,
    rejected before reaching the agent -- no JSON-RPC envelope at all.
  - 200 + top-level {"error": {"code": -32601|-32602, "message": ...}}:
    unknown method / malformed params -- a real protocol-level failure.
  - 200 + a normally "completed" task but with NO text in any artifact:
    a SOFT failure (e.g. empty input text) -- NOT flagged by the server,
    must be detected by inspecting content, so `ok` here is also False in
    that case even though HTTP/JSON-RPC both say success.
  - A garbage/nonexistent contextId is silently accepted as a brand-new
    conversation (no validation) -- we detect and flag this as a context
    mismatch via `context_mismatch` on the result rather than silently
    letting engine.py believe conversation continuity was preserved.
"""
from __future__ import annotations

import json
import time
import uuid

import requests

from d2e_client import TurnResult  # reuse the exact stats/UI contract

DEFAULT_PROMPT = "Hello"  # used only for the synthetic "start" turn


class A2AClient:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ---- internal -----------------------------------------------------
    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    @staticmethod
    def _build_payload(text: str, context_id: str | None) -> dict:
        message: dict = {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        return {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "SendStreamingMessage",
            "params": {
                "message": message,
                "configuration": {"acceptedOutputModes": ["text/plain"]},
            },
        }

    @staticmethod
    def _iter_envelopes(body: str, content_type: str):
        """Yield each JSON-RPC envelope from either an SSE body (one per
        `data:` line) or a single application/json body."""
        ct = (content_type or "").lower()
        if "text/event-stream" in ct:
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload:
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            pass
            return
        try:
            yield json.loads(body)
        except json.JSONDecodeError:
            return

    @staticmethod
    def _extract(envelopes: list[dict]) -> tuple[str | None, list[str], str | None, str]:
        """Returns (context_id, bot_texts, jsonrpc_error, task_state)."""
        context_id = None
        texts: dict[str, str] = {}   # artifact_id -> accumulated text (last write wins)
        order: list[str] = []
        jsonrpc_error = None
        task_state = ""
        for env in envelopes:
            if "error" in env:
                jsonrpc_error = f"{env['error'].get('code')}: {env['error'].get('message')}"
                continue
            result = env.get("result") or {}
            task = result.get("task")
            artifact_update = result.get("artifactUpdate")
            message = result.get("message")  # immediate (non-task) reply, rare
            if task:
                context_id = task.get("contextId") or context_id
                status = task.get("status") or {}
                if status.get("state"):
                    task_state = status["state"]
                # Some tasks carry the final text in status.message instead of
                # a separate artifactUpdate stream.
                status_msg = status.get("message")
                if status_msg and status_msg.get("role") == "ROLE_AGENT":
                    aid = "status"
                    if aid not in texts:
                        order.append(aid)
                    texts[aid] = "".join(p.get("text", "") for p in status_msg.get("parts", []))
            if artifact_update:
                context_id = artifact_update.get("contextId") or context_id
                artifact = artifact_update.get("artifact") or {}
                aid = artifact.get("artifactId") or "unknown"
                chunk = "".join(p.get("text", "") for p in artifact.get("parts", []) if "text" in p)
                if artifact_update.get("append"):
                    texts[aid] = texts.get(aid, "") + chunk
                else:
                    texts[aid] = chunk
                if aid not in order:
                    order.append(aid)
            if message:
                context_id = message.get("contextId") or context_id
                aid = "message"
                if aid not in order:
                    order.append(aid)
                texts[aid] = "".join(p.get("text", "") for p in message.get("parts", []))
        bot_texts = [texts[a] for a in order if texts.get(a, "").strip()]
        return context_id, bot_texts, jsonrpc_error, task_state

    def _post(self, token: str, text: str, context_id: str | None,
              user_id: str | None) -> TurnResult:
        t0 = time.perf_counter()
        payload = self._build_payload(text, context_id)
        try:
            r = self.session.post(self.base_url, headers=self._headers(token),
                                  json=payload, timeout=self.timeout, stream=True)
            body = r.text  # fully read (requests buffers on .text access)
        except requests.RequestException as e:
            return TurnResult(False, 0, (time.perf_counter() - t0) * 1000,
                              None, error=f"request_exception: {e}", user_id=user_id)
        latency = (time.perf_counter() - t0) * 1000

        if r.status_code == 401:
            # Gateway-level auth rejection -- plain text + WWW-Authenticate,
            # never a JSON-RPC envelope. Fast-fail, not a throttle.
            return TurnResult(False, r.status_code, latency, None,
                              error=f"401 auth rejected: {r.headers.get('www-authenticate', '')[:200]}",
                              raw=body, user_id=user_id)
        if r.status_code == 429:
            return TurnResult(False, r.status_code, latency, None, throttled=True,
                              error=f"HTTP 429; Retry-After={r.headers.get('Retry-After')}",
                              raw=body, user_id=user_id)
        if r.status_code >= 400:
            return TurnResult(False, r.status_code, latency, None,
                              error=f"HTTP {r.status_code}", raw=body, user_id=user_id)

        envelopes = list(self._iter_envelopes(body, r.headers.get("Content-Type", "")))
        returned_context, bot_texts, jsonrpc_error, task_state = self._extract(envelopes)

        soft_throttle = bool(jsonrpc_error and (
            "quota" in jsonrpc_error.lower() or "throttl" in jsonrpc_error.lower()
            or "rate" in jsonrpc_error.lower()))

        # Soft failure: server said 200/completed but produced no agent text
        # at all (observed empirically with an empty input message part).
        empty_completion = (not jsonrpc_error and task_state in ("TASK_STATE_COMPLETED", "")
                            and not bot_texts)
        err = jsonrpc_error
        if empty_completion and err is None:
            err = "Task completed with no agent text (soft failure; see plan.md)"

        ok = jsonrpc_error is None and not empty_completion

        result = TurnResult(ok=ok, status=r.status_code, latency_ms=latency,
                            conversation_id=returned_context, bot_texts=bot_texts,
                            throttled=soft_throttle, error=err, raw=body[:4000],
                            user_id=user_id)
        # Context continuity is NOT server-validated (a garbage contextId is
        # silently accepted as a new conversation) -- flag it so callers can
        # tell the difference between "preserved" and "silently restarted".
        if context_id and returned_context and context_id != returned_context:
            result.context_mismatch = True  # dynamic attr; dataclass has no __slots__
        return result

    # ---- public API: mirrors D2EClient's shape for drop-in use ---------
    def start_conversation(self, token: str) -> TurnResult:
        """A2A has no separate 'start, no message' call like D2E's
        emitStartConversationEvent -- the first SendStreamingMessage both
        creates the context and gets a reply, so we send a neutral greeting."""
        return self._post(token, DEFAULT_PROMPT, context_id=None, user_id=None)

    def send_message(self, token: str, conversation_id: str, text: str,
                     user_id: str = "loadtest-user") -> TurnResult:
        return self._post(token, text, context_id=conversation_id, user_id=user_id)
