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
                    candidates = body.get("candidates")
                    if relay.risk_mode:
                        if not isinstance(candidates, list) or not candidates:
                            raise ValueError("C group requires candidate risk scores with every question")
                        ranks = []
                        for item in candidates:
                            if (not isinstance(item, dict) or
                                    not isinstance(item.get("interpretations"), list) or
                                    len(item["interpretations"]) < 2 or
                                    not all(isinstance(x, str) and x.strip()
                                            for x in item["interpretations"])):
                                raise ValueError("candidate must include at least two plausible interpretations")
                            impact, uncertainty = item.get("impact"), item.get("uncertainty")
                            if (type(impact) is not int or type(uncertainty) is not int or
                                    not 1 <= impact <= 5 or not 1 <= uncertainty <= 3 or
                                    type(item.get("score")) is not int or
                                    item.get("score") != impact * uncertainty or
                                    not isinstance(item.get("question"), str)):
                                raise ValueError("candidate score must equal impact x uncertainty")
                            ranks.append((item["score"], impact, item["question"]))
                        best = max((score, impact) for score, impact, _ in ranks)
                        if question not in [q for score, impact, q in ranks
                                            if (score, impact) == best]:
                            raise ValueError("question must have the highest risk (then impact)")
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
                            event["risk_candidates"] = candidates
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
