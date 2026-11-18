"""Course experiment Oracle relay: retain raw replies, expose only public data.

The relay is deliberately scoped to one group and append_id. It keeps the
Oracle's complete response on the host, including internal_log, while Codex
receives only the answer, fallback flag, and remaining budget.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


def _validate_risk_request(question: str, body: dict) -> tuple[list, list, str]:
    """Check C's auditable gap inventory without dictating its risk judgment."""
    gaps, candidates = body.get("gaps"), body.get("candidates")
    selection_reason = body.get("selection_reason")
    if not isinstance(gaps, list) or not gaps:
        raise ValueError("C group requires a nonempty gap inventory")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("C group requires candidate questions")
    if not isinstance(selection_reason, str) or not selection_reason.strip():
        raise ValueError("C group requires a selection_reason")
    gap_ids, open_ids = set(), set()
    for gap in gaps:
        if not isinstance(gap, dict):
            raise ValueError("each gap must be an object")
        gap_id = gap.get("id")
        if (not isinstance(gap_id, str) or not gap_id.strip() or
                gap_id in gap_ids or
                any(not isinstance(gap.get(key), str) or not gap[key].strip()
                    for key in ("topic", "uncertainty")) or
                gap.get("status") not in ("open", "resolved", "deferred")):
            raise ValueError("gaps need unique IDs, topic, uncertainty, and valid status")
        gap_ids.add(gap_id)
        if gap["status"] == "open":
            open_ids.add(gap_id)
    candidate_ids, candidate_questions = set(), set()
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("each candidate must be an object")
        gap_id, candidate_question = item.get("gap_id"), item.get("question")
        if (not isinstance(gap_id, str) or gap_id not in open_ids or
                gap_id in candidate_ids or
                not isinstance(candidate_question, str) or
                not candidate_question.strip() or
                candidate_question in candidate_questions or
                item.get("priority") not in ("high", "medium", "low") or
                not isinstance(item.get("reason"), str) or
                not item["reason"].strip()):
            raise ValueError("candidates need one open gap, question, priority, and reason")
        candidate_ids.add(gap_id)
        candidate_questions.add(candidate_question)
    if candidate_ids != open_ids:
        raise ValueError("every open gap must have exactly one candidate")
    if question not in candidate_questions:
        raise ValueError("selected question must match a candidate")
    return gaps, candidates, selection_reason


class OracleRelay:
    def __init__(self, *, append_id: str, alias: str, host: str, port: int,
                 log_path: Path, timeout: float = 180, risk_mode: bool = False):
        self.append_id, self.alias = append_id, alias
        self.url = f"http://{host}:{port}/"
        self.log_path = log_path
        self.timeout = timeout
        self.risk_mode = risk_mode
        self.events: list[dict] = []
        self.fatal: str | None = None
        self._lock = threading.Lock()
        self._question_lock = threading.Lock()
        self._active_condition = threading.Condition()
        self._active_requests = 0
        self._answers: dict[str, dict] = {}
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self):
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                with relay._active_condition:
                    relay._active_requests += 1
                try:
                    self._handle_question()
                finally:
                    with relay._active_condition:
                        relay._active_requests -= 1
                        relay._active_condition.notify_all()

            def _handle_question(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(min(length, 65536)))
                    question = body.get("question", "")
                    if not isinstance(question, str) or not question.strip():
                        raise ValueError("question must be a nonempty string")
                    if relay.risk_mode:
                        gaps, candidates, selection_reason = _validate_risk_request(
                            question, body)
                except (ValueError, json.JSONDecodeError) as exc:
                    self._reply(400, {"data": "", "status": {"ok": False, "error": str(exc)}})
                    return
                request = {"append_id": relay.append_id, "task_id": relay.alias,
                           "question": question}
                # Only one upstream question may be in flight. Replaying an exact
                # duplicate after a client timeout must not consume another turn.
                with relay._question_lock:
                    public = relay._answers.get(question)
                    if public is None:
                        if relay.fatal:
                            self._reply(503, {"data": "", "status": {"ok": False,
                                                                      "error": "Oracle unavailable"}})
                            return
                        started = time.time()
                        try:
                            response = requests.post(relay.url, json=request,
                                                     timeout=relay.timeout)
                            response.raise_for_status()
                            raw = response.json()
                        except Exception as exc:  # upstream may already have used a turn
                            with relay._lock:
                                relay.fatal = f"oracle_transport_error: {type(exc).__name__}: {exc}"
                            relay._record({"ts": started, "request": request,
                                           "error": relay.fatal})
                            self._reply(504, {"data": "", "status": {"ok": False,
                                                                      "error": "Oracle unavailable"}})
                            return
                        event = {"ts": started, "elapsed_s": round(time.time() - started, 3),
                                 "request": request, "response": raw}
                        if relay.risk_mode:
                            event["risk_gaps"] = gaps
                            event["risk_candidates"] = candidates
                            event["risk_selection_reason"] = selection_reason
                        relay._record(event)
                        status = raw.get("status") or {}
                        if not status.get("ok") and status.get("error") != "max_interactions_reached":
                            with relay._lock:
                                relay.fatal = f"oracle_error: {status.get('error', 'unknown')}"
                        internal = status.get("internal_log") or {}
                        fallback = bool(internal.get("fallback_triggered"))
                        public = {"data": raw.get("data", ""),
                                  "status": {"ok": bool(status.get("ok")),
                                             "remaining": status.get("remaining"),
                                             "fallback": fallback}}
                        if not status.get("ok"):
                            public["status"]["error"] = status.get("error")
                        if status.get("ok"):
                            relay._answers[question] = public
                self._reply(200, public)

            def _reply(self, code, body):
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    # The client may time out while the Oracle is still working.
                    # The upstream response has already been recorded.
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A Codex HTTP command can time out while its Oracle request is still
        # running. Drain those handler threads before exporting the service log;
        # otherwise the final answer and budget turn can be omitted from results.
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.server_port

    def _record(self, event: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.events.append(event)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def __exit__(self, *_exc):
        if self.server:
            self.server.shutdown()
        if self.thread:
            self.thread.join(timeout=5)
        # ThreadingHTTPServer may return from server_close while a handler is
        # still waiting for the upstream Oracle. Export must happen only after
        # every accepted request has been recorded, including late answers.
        with self._active_condition:
            drained = self._active_condition.wait_for(
                lambda: self._active_requests == 0, timeout=self.timeout + 30)
        if not drained:
            self.fatal = self.fatal or "oracle_inflight_drain_timeout"
        if self.server:
            self.server.server_close()


def confirmed_questions(events: list[dict]) -> tuple[list[dict], int, int]:
    """Return every successful Q&A, request count, and flagged fallback count."""
    questions, requested, fallbacks = [], 0, 0
    for event in events:
        raw = event.get("response") or {}
        status = raw.get("status") or {}
        if not status.get("ok"):
            continue
        requested += 1
        internal = status.get("internal_log") or {}
        if internal.get("fallback_triggered"):
            fallbacks += 1
        questions.append({"question": event["request"]["question"],
                          "answer": raw.get("data", ""), "source": "oracle"})
    return questions, requested, fallbacks


def service_questions(entries: list[dict]) -> tuple[list[dict], int, int]:
    """Forward every successful Oracle reply from the trusted service log."""
    questions, requested, fallbacks = [], 0, 0
    for entry in entries:
        if "answer" not in entry:  # rejected over-budget calls are not turns
            continue
        requested += 1
        if entry.get("fallback_triggered"):
            fallbacks += 1
        questions.append({"question": entry.get("question", ""),
                          "answer": entry.get("answer", ""), "source": "oracle"})
    return questions, requested, fallbacks
