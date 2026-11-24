"""
Copilot Studio Direct-to-Engine (D2E) conversation client.

Endpoint shape (per Microsoft 365 Agents SDK CopilotStudio.Client):
  START:    POST {base}/bots/{schema}/conversations?api-version=2022-03-01-preview
  CONTINUE: POST {base}/bots/{schema}/conversations/{conversationId}?api-version=...
  where base = https://{host}/copilotstudio/dataverse-backed/authenticated

Auth: Authorization: Bearer <Entra token, scope api.powerplatform.com/.default>

The response is a Bot Framework activity stream. The real wire format (SSE
'text/event-stream' vs application/json) and the THROTTLE response shape are things
we capture empirically — so this client:
  * requests SSE but tolerates JSON,
  * returns the raw body + parsed activities + status + latency,
  * flags throttling (HTTP 429 or error activities) explicitly.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

API_VERSION = "2022-03-01-preview"


@dataclass
class TurnResult:
    ok: bool
    status: int
    latency_ms: float
    conversation_id: str | None
    bot_texts: list[str] = field(default_factory=list)
    throttled: bool = False
    error: str | None = None
    raw: str = ""
    tool_calls: int = 0
    tools: dict = field(default_factory=dict)
    knowledge_hits: int = 0
    user_id: str | None = None   # set by engine.py: which virtual user/session this was
    prompt: str | None = None    # set by engine.py: the prompt text sent (turns only)
    account: str | None = None   # set by engine.py: the REAL pooled-account username
                                  # that backed this session (e.g. "alice@contoso.com"),
                                  # distinct from user_id above which is just the
                                  # synthetic virtual-user label ("vu3"/"smoke0") --
                                  # None for single-account auth modes (static/
                                  # devicecode/confidential), where there's no
                                  # per-session account to report.
    tool_error: bool = False     # True if a downstream tool/connector failed internally
                                 # even though the overall HTTP response was 200 OK
    tool_errors: list = field(default_factory=list)  # [{tool, path, detail}, ...]
    harness_unsupported: bool = False  # True if the agent rejected this D2E call as
                                       # unsupported for its harness (e.g. GitHub
                                       # Copilot harness agents reject this endpoint
                                       # outright with HTTP 200 + a canned refusal --
                                       # NOT a real conversation turn, flagged as a
                                       # failure here even though status==200)


class D2EClient:
    def __init__(self, host: str, schema: str, api_version: str = API_VERSION,
                 timeout: float = 120.0):
        self.base = (
            f"https://{host}/copilotstudio/dataverse-backed/authenticated/bots/{schema}"
        )
        self.api_version = api_version
        self.timeout = timeout
        self.session = requests.Session()

    # ---- internal ---------------------------------------------------------
    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Ask for streaming; server may still answer JSON — we handle both.
            "Accept": "text/event-stream, application/json",
        }

    @staticmethod
    def _parse_activities(body: str, content_type: str) -> list[dict]:
        """Parse either SSE ('data: {...}' lines) or a JSON body into activities."""
        activities: list[dict] = []
        ct = (content_type or "").lower()
        if "text/event-stream" in ct or body.lstrip().startswith("event:") \
                or "\ndata:" in body or body.lstrip().startswith("data:"):
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        activities.append(json.loads(payload))
                    except json.JSONDecodeError:
                        pass
            if activities:
                return activities
        # Fall back to JSON (single activity, {"activities":[...]}, or a list)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return activities
        if isinstance(data, dict) and "activities" in data:
            return data["activities"]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return activities

    @staticmethod
    def _extract(activities: list[dict]) -> tuple[str | None, list[str], str | None]:
        conv_id, texts, err = None, [], None
        for a in activities:
            conv = a.get("conversation") or {}
            if conv.get("id"):
                conv_id = conv["id"]
            if a.get("type") == "message" and a.get("text"):
                # bot turns: role 'bot' or simply not the user; capture all text
                if (a.get("from") or {}).get("role") != "user":
                    texts.append(a["text"])
            if a.get("type") == "error" or a.get("code") or a.get("error"):
                err = json.dumps(a)[:500]
        return conv_id, texts, err

    @staticmethod
    def _find_embedded_errors(obj, path: str = "", _depth: int = 0) -> list[dict]:
        """Recursively scan a tool's observation payload for error-shaped fields
        (e.g. 'search_errors', 'errors', 'exception', 'fault') with actual content.
        Generic by design: different tools/connectors (Azure AI Search, Power
        Automate flows, custom connectors) use different field names, so we match on
        any key containing 'error'/'fault'/'exception' rather than hardcoding one
        tool's schema. Returns [{'path': 'observation.search_result.search_errors',
        'detail': <the non-empty value>}, ...]."""
        found: list[dict] = []
        if _depth > 6:   # guard against pathological/cyclic structures
            return found
        if isinstance(obj, dict):
            for k, v in obj.items():
                key_l = k.lower()
                here = f"{path}.{k}" if path else k
                if "error" in key_l or "fault" in key_l or "exception" in key_l:
                    if isinstance(v, list) and v:
                        found.append({"path": here, "detail": v[:5]})
                    elif isinstance(v, dict) and v:
                        found.append({"path": here, "detail": v})
                    elif isinstance(v, str) and v.strip():
                        found.append({"path": here, "detail": v})
                found.extend(D2EClient._find_embedded_errors(v, here, _depth + 1))
        elif isinstance(obj, list):
            for i, item in enumerate(obj[:20]):
                found.extend(D2EClient._find_embedded_errors(item, f"{path}[{i}]", _depth + 1))
        return found

    @staticmethod
    def _extract_tools(activities: list[dict]) -> tuple[int, dict[str, int], int, list[dict]]:
        """Live counting of tool/knowledge-source invocations from plan events, plus
        detection of EMBEDDED tool failures (a downstream service like Azure AI Search
        erroring while the overall HTTP request still returns 200).

        Each real invocation is marked by a 'DynamicPlanStepTriggered' activity whose
        nested value carries:
          - taskDialogId: the tool identifier, e.g. "P:UniversalSearchTool"
          - type: a classification of the step, e.g. "KnowledgeSource" for knowledge
            retrieval (knowledge grounding IS a tool call, tagged with this type)

        Its completion is 'DynamicPlanStepFinished', whose value.observation carries
        the tool's raw result -- this is where a downstream failure (e.g. Azure AI
        Search timing out) shows up even though the agent gracefully degrades and the
        HTTP response is a clean 200. We scan that payload for error-shaped fields.

        We count every trigger event (not distinct names), so an agent calling the
        same tool multiple times isn't undercounted. Returns:
          (total_invocations, {tool_name: invocation_count}, knowledge_hit_count,
           tool_errors) where tool_errors is a list of
          {"tool": name, "path": "...", "detail": ...} for any embedded failure found.
        """
        tool_hist: dict[str, int] = {}
        knowledge = 0
        total = 0
        tool_errors: list[dict] = []
        for a in activities:
            name = a.get("name")
            val = a.get("value")
            if not isinstance(val, dict):
                continue
            if name == "DynamicPlanStepTriggered":
                tid = val.get("taskDialogId") or "unknown"
                label = tid[2:] if isinstance(tid, str) and tid.startswith("P:") else str(tid)
                tool_hist[label] = tool_hist.get(label, 0) + 1
                total += 1
                if val.get("type") == "KnowledgeSource":
                    knowledge += 1
            elif name == "DynamicPlanStepFinished":
                tid = val.get("taskDialogId") or "unknown"
                label = tid[2:] if isinstance(tid, str) and tid.startswith("P:") else str(tid)
                observation = val.get("observation")
                if observation is not None:
                    for err in D2EClient._find_embedded_errors(observation, "observation"):
                        tool_errors.append({"tool": label, **err})
        return total, tool_hist, knowledge, tool_errors[:10]

    def _post(self, url: str, token: str, body: dict | None) -> TurnResult:
        t0 = time.perf_counter()
        try:
            r = self.session.post(url, headers=self._headers(token),
                                  json=body if body is not None else {},
                                  timeout=self.timeout)
        except requests.RequestException as e:
            return TurnResult(False, 0, (time.perf_counter() - t0) * 1000,
                              None, error=f"request_exception: {e}")
        latency = (time.perf_counter() - t0) * 1000
        raw = r.text
        throttled = r.status_code == 429
        if throttled:
            return TurnResult(False, r.status_code, latency, None,
                              throttled=True,
                              error=f"HTTP 429; Retry-After={r.headers.get('Retry-After')}",
                              raw=raw)
        if r.status_code >= 400:
            return TurnResult(False, r.status_code, latency, None,
                              error=f"HTTP {r.status_code}", raw=raw)
        acts = self._parse_activities(raw, r.headers.get("Content-Type", ""))
        conv_id, texts, err = self._extract(acts)
        tool_calls, tools, knowledge, tool_errors = self._extract_tools(acts)
        # Some throttle/quota failures come back as 200 + an error activity.
        soft_throttle = bool(err and ("quota" in err.lower() or "throttl" in err.lower()
                                      or "rate" in err.lower()))
        # Some harnesses (observed: GitHub Copilot harness agents) reject this D2E
        # endpoint outright with a canned refusal message, still wrapped as a normal
        # HTTP 200 + message activity + a "turn.complete" event -- no real agent
        # reasoning ever happens. Detect it so it isn't silently counted as success.
        harness_unsupported = any(
            "doesn't support agents built with the" in t and "harness" in t
            for t in texts)
        ok = err is None and not harness_unsupported
        if harness_unsupported and err is None:
            err = "Harness rejected this endpoint: " + texts[0] if texts else \
                "Harness rejected this endpoint (unsupported)."
        return TurnResult(ok=ok, status=r.status_code, latency_ms=latency,
                          conversation_id=conv_id, bot_texts=texts,
                          throttled=soft_throttle, error=err, raw=raw,
                          tool_calls=tool_calls, tools=tools, knowledge_hits=knowledge,
                          tool_error=bool(tool_errors), tool_errors=tool_errors,
                          harness_unsupported=harness_unsupported)

    # ---- public API -------------------------------------------------------
    def start_conversation(self, token: str) -> TurnResult:
        url = f"{self.base}/conversations?api-version={self.api_version}"
        return self._post(url, token, {"emitStartConversationEvent": True})

    def send_message(self, token: str, conversation_id: str, text: str,
                     user_id: str = "loadtest-user") -> TurnResult:
        url = f"{self.base}/conversations/{conversation_id}?api-version={self.api_version}"
        # The continue-conversation endpoint requires the activity WRAPPED in an
        # "activity" property (empirically verified; bare activity returns 400).
        body = {
            "activity": {
                "type": "message",
                "text": text,
                "from": {"id": user_id, "role": "user"},
            }
        }
        return self._post(url, token, body)

    def send_activity(self, token: str, conversation_id: str, activity: dict) -> TurnResult:
        """Send an arbitrary Bot Framework activity instead of a plain text
        message -- e.g. a "value"-type activity submitting an Adaptive Card
        action (used by preflight.py to auto-submit a connector consent
        card's "Allow" action, the same JSON the card's button sends when
        clicked interactively)."""
        url = f"{self.base}/conversations/{conversation_id}?api-version={self.api_version}"
        return self._post(url, token, {"activity": activity})
