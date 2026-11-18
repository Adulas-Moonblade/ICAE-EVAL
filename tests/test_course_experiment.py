import asyncio
import contextlib
import io
import json
import shutil
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

import requests

from harness import config as C
from harness.course_experiment import (GROUPS, RISK_STRATEGY_VERSION, _attempt, _coding_prd, _overall,
                                       _phase, _requirements_prompt, _selected_groups,
                                       build_parser, run)
from harness.course_oracle import (OracleRelay, _validate_risk_request,
                                   confirmed_questions, service_questions)
from harness.runner_common import AgentResult


class CourseDataTests(unittest.TestCase):
    def test_cli_rejects_missing_or_repeated_repo(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--repo", "realcode@005", "--repo", "realcode@006"])
        self.assertEqual(build_parser().parse_args(["--repo", "realcode@005"])
                         .user_model_name, "Deepseek-V4.1-Flash")

    def test_all_eight_groups_have_separate_strategy_and_budget(self):
        self.assertEqual([g[0] for g in GROUPS],
                         ["A", "A′3", "A′8", "B3", "B8", "C3", "C8", "D"])
        self.assertEqual([g[2] for g in GROUPS], [0, 3, 8, 3, 8, 3, 8, 0])

    def test_course_group_subset_preserves_requested_order(self):
        self.assertEqual([g[0] for g in _selected_groups("B3,B8,C3,C8")],
                         ["B3", "B8", "C3", "C8"])
        with self.assertRaises(SystemExit):
            _selected_groups("B3,unknown")
        with self.assertRaises(SystemExit):
            _selected_groups("B3,B3")

    def test_oracle_prompt_waits_for_each_answer_longer_than_relay_timeout(self):
        prompt = _requirements_prompt("free", 2, "http://127.0.0.1:123/", 180)
        self.assertIn("at least 210 seconds", prompt)
        self.assertIn("Wait for its full answer", prompt)
        self.assertIn("public_test_cases/", prompt)
        self.assertIn("whole question is specific and fully covered", prompt)
        self.assertIn("no partial answer", prompt)
        self.assertIn("still consumes one question", prompt)
        self.assertIn("independent behaviors or ambiguities", prompt)
        self.assertIn("one coherent output-contract topic", prompt)
        self.assertIn("hidden evaluation cases", prompt)
        self.assertNotIn("partial answers", prompt)
        self.assertNotIn("more than three technical points", prompt)

    def test_risk_prompt_uses_gap_inventory_without_numeric_ranking(self):
        prompt = _requirements_prompt("risk", 3, "http://127.0.0.1:123/", 180)
        self.assertEqual(RISK_STRATEGY_VERSION, "gap_v2_1")
        self.assertIn("systematically identify", prompt)
        self.assertIn("every open gap", prompt)
        self.assertIn("selection_reason", prompt)
        self.assertIn("focused fallback", prompt)
        self.assertIn("scenario-specific gaps", prompt)
        self.assertIn("zero, negative, and noninteger inputs", prompt)
        self.assertIn("only one or two questions remain", prompt)
        self.assertIn("unasked core state transitions", prompt)
        self.assertNotIn("impact*uncertainty", prompt)
        self.assertNotIn("highest score", prompt)
        free_prompt = _requirements_prompt("free", 3, "http://127.0.0.1:123/", 180)
        self.assertNotIn("selection_reason", free_prompt)
        self.assertNotIn("scenario-specific gaps", free_prompt)

    def test_risk_request_requires_full_inventory_but_allows_reasoned_override(self):
        gaps = [
            {"id": "G1", "topic": "add", "uncertainty": "replace or accumulate",
             "status": "open"},
            {"id": "G2", "topic": "dump", "uncertainty": "output order",
             "status": "open"},
        ]
        candidates = [
            {"gap_id": "G1", "question": "add?", "priority": "high",
             "reason": "affects state"},
            {"gap_id": "G2", "question": "dump?", "priority": "medium",
             "reason": "affects output"},
        ]
        body = {"gaps": gaps, "candidates": candidates,
                "selection_reason": "dump blocks the current output design"}
        self.assertEqual(_validate_risk_request("dump?", body),
                         (gaps, candidates, body["selection_reason"]))
        with self.assertRaisesRegex(ValueError, "every open gap"):
            _validate_risk_request("add?", {**body, "candidates": candidates[:1]})
        with self.assertRaisesRegex(ValueError, "selection_reason"):
            _validate_risk_request("add?", {**body, "selection_reason": ""})
        with self.assertRaisesRegex(ValueError, "selected question"):
            _validate_risk_request("other?", body)
        updated = [{**gaps[0], "status": "resolved"}, gaps[1]]
        self.assertEqual(_validate_risk_request("dump?", {
            **body, "gaps": updated, "candidates": candidates[1:]} )[0], updated)

    def test_coding_prd_contains_oracle_replies(self):
        q = [{"question": "format?", "answer": "JSON", "source": "oracle"}]
        self.assertEqual(_coding_prd("original", q, "direct"), "original")
        self.assertEqual(_coding_prd("detailed", q, "detailed"), "detailed")
        self.assertEqual(json.loads(_coding_prd("original", q, "free").split(
            "## Oracle replies\n\n", 1)[1])["questions"], q)
        self.assertEqual(_overall({"hidden": {"passed": 1, "total": 2},
                                   "enhanced": {"passed": 1, "total": 2}}), 0.5)
        self.assertEqual(_overall({"public_visible": {"passed": 6, "total": 6},
                                   "hidden": {"passed": 6, "total": 6},
                                   "enhanced": {"passed": 19, "total": 39}}), 0.5556)

    def test_oracle_service_entries_forward_fallback_and_parse_error(self):
        entries = [{"question": "a", "answer": "yes", "fallback_triggered": False},
                   {"question": "b", "answer": "fallback", "fallback_triggered": True},
                   {"question": "d", "answer": "malformed original", "parse_error": True},
                   {"question": "c", "error": "max_interactions_reached"}]
        self.assertEqual(service_questions(entries),
                         ([{"question": "a", "answer": "yes", "source": "oracle"},
                           {"question": "b", "answer": "fallback", "source": "oracle"},
                           {"question": "d", "answer": "malformed original",
                            "source": "oracle"}], 3, 1))

    def test_relay_entries_keep_fallback_answer(self):
        events = [
            {"request": {"question": "unknown?"}, "response": {
                "data": "not specified", "status": {"ok": True,
                    "internal_log": {"fallback_triggered": True}}}},
            {"request": {"question": "malformed?"}, "response": {
                "data": "raw reply", "status": {"ok": True, "parse_error": True,
                    "internal_log": {}}}},
        ]
        self.assertEqual(confirmed_questions(events),
                         ([{"question": "unknown?", "answer": "not specified",
                            "source": "oracle"},
                           {"question": "malformed?", "answer": "raw reply",
                            "source": "oracle"}], 2, 1))

    def test_relay_hides_internal_log_and_preserves_raw_response(self):
        class Oracle(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.received = payload
                response = {"data": "answer", "status": {"ok": True, "remaining": 2,
                            "parse_error": True,
                            "internal_log": {"triggers_hit": ["secret"]}}}
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Oracle)
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "oracle_raw.jsonl"
                with OracleRelay(append_id="a" * 32, alias="realcode@005",
                                 host="127.0.0.1", port=upstream.server_port,
                                 log_path=log) as relay:
                    visible = requests.post(f"http://127.0.0.1:{relay.port}/",
                                            json={"question": "what?"}, timeout=3).json()
                self.assertNotIn("internal_log", json.dumps(visible))
                self.assertEqual(visible["data"], "answer")
                self.assertFalse(visible["status"]["fallback"])
                self.assertEqual(upstream.received["task_id"], "realcode@005")
                self.assertIn("secret", log.read_text())
                with OracleRelay(append_id="b" * 32, alias="realcode@005",
                                 host="127.0.0.1", port=upstream.server_port,
                                 log_path=Path(tmp) / "risk.jsonl", risk_mode=True) as relay:
                    gaps = [
                        {"id": "G1", "topic": "low", "uncertainty": "which output",
                         "status": "open"},
                        {"id": "G2", "topic": "high", "uncertainty": "which state",
                         "status": "open"},
                    ]
                    candidates = [
                        {"gap_id": "G1", "question": "low", "priority": "low",
                         "reason": "local output"},
                        {"gap_id": "G2", "question": "high", "priority": "high",
                         "reason": "shared state"}]
                    bad = requests.post(f"http://127.0.0.1:{relay.port}/",
                                        json={"question": "low", "candidates": candidates},
                                        timeout=3)
                    self.assertEqual(bad.status_code, 400)
                    good = requests.post(f"http://127.0.0.1:{relay.port}/",
                                         json={"question": "low", "gaps": gaps,
                                               "candidates": candidates,
                                               "selection_reason": "better answerability"},
                                         timeout=3)
                    self.assertEqual(good.status_code, 200)
                self.assertEqual(upstream.received["question"], "low")
                recorded = json.loads((Path(tmp) / "risk.jsonl").read_text().splitlines()[0])
                self.assertEqual(recorded["risk_gaps"], gaps)
                self.assertEqual(recorded["risk_candidates"], candidates)
                self.assertEqual(recorded["risk_selection_reason"], "better answerability")
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)

    def test_relay_drains_request_after_client_timeout(self):
        received = Event()

        class SlowOracle(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                received.set()
                time.sleep(0.2)
                raw = json.dumps({"data": "late answer", "status": {
                    "ok": True, "remaining": 0, "internal_log": {}}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowOracle)
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "oracle_raw.jsonl"
                with OracleRelay(append_id="c" * 32, alias="realcode@005",
                                 host="127.0.0.1", port=upstream.server_port,
                                 log_path=log) as relay:
                    with self.assertRaises(requests.exceptions.ReadTimeout):
                        requests.post(f"http://127.0.0.1:{relay.port}/",
                                      json={"question": "late?"}, timeout=0.05)
                    self.assertTrue(received.wait(timeout=1))
                self.assertEqual(json.loads(log.read_text().splitlines()[0])
                                 ["response"]["data"], "late answer")
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)

    def test_relay_exit_waits_for_late_oracle_answer(self):
        received = Event()
        release = Event()

        class SlowOracle(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                received.set()
                release.wait(timeout=3)
                raw = json.dumps({"data": "late", "status": {"ok": True,
                    "remaining": 0, "internal_log": {}}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowOracle)
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "raw.jsonl"
                with OracleRelay(append_id="e" * 32, alias="realcode@005",
                                 host="127.0.0.1", port=upstream.server_port,
                                 log_path=log, timeout=3) as relay:
                    with self.assertRaises(requests.exceptions.ReadTimeout):
                        requests.post(f"http://127.0.0.1:{relay.port}/",
                                      json={"question": "late?"}, timeout=0.05)
                    self.assertTrue(received.wait(timeout=1))
                    Thread(target=lambda: (time.sleep(0.8), release.set()),
                           daemon=True).start()
                self.assertEqual(len(relay.events), 1)
                self.assertEqual(json.loads(log.read_text().splitlines()[0])
                                 ["response"]["data"], "late")
        finally:
            release.set()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)

    def test_relay_replays_duplicate_question_without_second_oracle_turn(self):
        class Oracle(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.server.calls += 1
                time.sleep(0.1)
                raw = json.dumps({"data": "answer", "status": {
                    "ok": True, "remaining": 2, "internal_log": {}}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Oracle)
        upstream.calls = 0
        thread = Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with OracleRelay(append_id="d" * 32, alias="realcode@005",
                                 host="127.0.0.1", port=upstream.server_port,
                                 log_path=Path(tmp) / "raw.jsonl") as relay:
                    def ask():
                        return requests.post(f"http://127.0.0.1:{relay.port}/",
                                             json={"question": "same?"}, timeout=3).json()

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        replies = list(pool.map(lambda _: ask(), range(2)))
                self.assertEqual([r["data"] for r in replies], ["answer", "answer"])
                self.assertEqual(upstream.calls, 1)
                self.assertEqual(len(relay.events), 1)
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)


class CourseRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_oracle_failure_still_exports_private_service_log(self):
        class Relay:
            fatal = None
            port = 12345

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

        async def fail_phase(*_args, **_kwargs):
            raise RuntimeError("oracle upstream unavailable")

        async def immediate(func, *args, **kwargs):
            return func(*args, **kwargs)

        args = SimpleNamespace(user_host="127.0.0.1", user_init_port=50001,
                               user_query_port=50002, user_eval_port=50003,
                               user_model_name="Qwen3.8-Flash", model_name="Codex-config",
                               oracle_timeout=180, eval=False)
        entry = {"question": "known?", "answer": "raw answer",
                 "parse_error": True, "raw_response": "raw answer"}
        append_id = "a" * 32
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / append_id).mkdir()
            with patch.object(C, "RESULTS", results), \
                 patch("harness.course_experiment.OracleRelay", Relay), \
                 patch("harness.course_experiment._phase", side_effect=fail_phase), \
                 patch("harness.course_experiment.asyncio.to_thread", side_effect=immediate), \
                 patch("harness.course_experiment.ua.mint_append_id", return_value=append_id), \
                 patch("harness.course_experiment.ua.init_settings"), \
                 patch("harness.course_experiment.ua.update_repo_status"), \
                 patch("harness.course_experiment.ua.fetch_interaction_log",
                       return_value=[entry]):
                row = await _attempt("realcode@005", "B3", "free", 3,
                                     "fuzzy", "detailed", Path("image.tar"),
                                     args, {}, "batch")
            saved = [json.loads(line) for line in (
                results / append_id / "oracle_service.jsonl").read_text().splitlines()]
        self.assertEqual(row["status"], "infrastructure_error")
        self.assertEqual(saved, [entry])

    async def test_requirements_phase_receives_public_scaffold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "workspace"
            scaffold = root / "data" / "sample" / "rcb_tests" / "public_test_cases"
            scaffold.mkdir(parents=True)
            (scaffold / "feature.json").write_text('{"cases": []}')
            seen = []

            class Container:
                container_id = "container"
                evaluation_image = "image"

                def teardown(self):
                    seen.append("teardown")

            def fake_provision(_alias, code_path, prd, _tar_path, **kwargs):
                self.assertEqual(kwargs["scaffold_src"], scaffold)
                code_path.mkdir()
                (code_path / "start.md").write_text(prd)
                shutil.copytree(scaffold, code_path / "rcb_tests" / "public_test_cases")
                return Container()

            async def fake_agent(_prompt, code_path, *_args, **_kwargs):
                self.assertEqual((code_path / "start.md").read_text(), "PRD")
                self.assertTrue((code_path / "rcb_tests" / "public_test_cases" /
                                 "feature.json").is_file())
                return AgentResult(status="success")

            async def immediate(func, *args, **kwargs):
                return func(*args, **kwargs)

            args = SimpleNamespace(proxy=None, proxy_fallback_file=None,
                                   requirements_timeout=60, coding_timeout=60)
            with patch.object(C, "REPOS_DIR", root / "data"), \
                 patch.object(C, "RESULTS", root / "results"), \
                 patch.object(C, "key_for_alias", return_value="sample"), \
                 patch.object(C, "code_path", return_value=workdir), \
                 patch("harness.course_experiment.docker_env.provision",
                       side_effect=fake_provision), \
                 patch("harness.course_experiment.asyncio.to_thread", side_effect=immediate), \
                 patch("harness.course_experiment.run_agent", side_effect=fake_agent):
                result, *_ = await _phase("prompt", alias="realcode@005",
                                          append_id="a" * 32, phase="requirements",
                                          prd="PRD", tar_path=root / "image.tar",
                                          args=args, model={})
            self.assertEqual(result.status, "success")
            self.assertIn("teardown", seen)

    async def test_b_group_coding_receives_all_service_answers(self):
        class Relay:
            port = 12345
            fatal = None
            events = [
                {"request": {"question": "confirmed?"}, "response": {
                    "data": "yes", "status": {"ok": True, "internal_log": {}}}},
                {"request": {"question": "known and unknown?"}, "response": {
                    "data": "Known rule; other behavior unspecified.", "status": {
                    "ok": True, "internal_log": {"triggers_hit": ["C004"],
                                                  "fallback_triggered": False}}}},
                {"request": {"question": "fallback?"}, "response": {
                    "data": "do not use", "status": {"ok": True,
                    "internal_log": {"fallback_triggered": True}}}}]

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

        entries = [{"question": "confirmed?", "answer": "yes",
                    "fallback_triggered": False},
                   {"question": "known and unknown?",
                    "answer": "Known rule; other behavior unspecified.",
                    "triggers_hit": ["C004"], "fallback_triggered": False},
                   {"question": "fallback?", "answer": "do not use",
                    "fallback_triggered": True}]
        args = SimpleNamespace(user_host="127.0.0.1", user_init_port=50001,
                               user_query_port=50002, user_eval_port=50003,
                               user_model_name="Qwen3.8-Flash", model_name="Codex-config",
                               oracle_timeout=180, eval=False)
        phase_inputs = []

        async def fake_phase(prompt, **kwargs):
            phase_inputs.append((kwargs["phase"], kwargs["prd"], prompt))
            return AgentResult(status="success", input_tokens=10,
                               output_tokens=5), 2.0, Path("unused.log"), "base-image"

        async def immediate(func, *args, **kwargs):
            return func(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            (results / ("a" * 32)).mkdir()
            with patch.object(C, "RESULTS", results), \
                 patch("harness.course_experiment.OracleRelay", Relay), \
                 patch("harness.course_experiment._phase", side_effect=fake_phase), \
                 patch("harness.course_experiment.asyncio.to_thread", side_effect=immediate), \
                 patch("harness.course_experiment._json_message",
                       return_value={"questions": []}), \
                 patch("harness.course_experiment.ua.mint_append_id", return_value="a" * 32), \
                 patch("harness.course_experiment.ua.init_settings"), \
                 patch("harness.course_experiment.ua.update_repo_status"), \
                 patch("harness.course_experiment.ua.fetch_interaction_log",
                       return_value=entries), \
                 patch("harness.course_experiment.ua.fetch_interaction_stats",
                       return_value={"ok": True}):
                row = await _attempt("realcode@005", "B8", "free", 8,
                                     "fuzzy", "detailed", Path("image.tar"),
                                     args, {}, "batch")
        self.assertEqual(row["status"], "success", row)
        self.assertEqual((row["actual_questions"], row["fallback_count"],
                          row["confirmed_questions"]), (3, 1, 2))
        self.assertEqual(row["forwarded_questions"], 3)
        self.assertEqual([p[0] for p in phase_inputs], ["requirements", "coding"])
        self.assertIn('"answer": "yes"', phase_inputs[1][1])
        self.assertIn("Known rule; other behavior unspecified.", phase_inputs[1][1])
        self.assertIn("do not use", phase_inputs[1][1])
        self.assertTrue(row["oracle_log_mismatch"])  # audit hint, not a run failure
        self.assertEqual(row["requirements_output_authority"], "audit_summary_only")
        self.assertEqual(row["total_codex_tokens"], 30)

    async def test_b_group_continues_when_audit_summary_is_invalid(self):
        class Relay:
            port = 12345
            fatal = None
            events = [{"request": {"question": "known?"}, "response": {
                "data": "known answer", "status": {"ok": True, "internal_log": {}}}}]

            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

        args = SimpleNamespace(user_host="127.0.0.1", user_init_port=50001,
                               user_query_port=50002, user_eval_port=50003,
                               user_model_name="Qwen3.8-Flash", model_name="Codex-config",
                               oracle_timeout=180, eval=False)

        async def immediate(func, *args, **kwargs):
            return func(*args, **kwargs)

        for bad_summary in (ValueError("invalid JSON"), {"other": []}):
            with self.subTest(bad_summary=bad_summary), tempfile.TemporaryDirectory() as tmp:
                append_id = "a" * 32
                results = Path(tmp)
                (results / append_id).mkdir()
                phase_inputs = []

                async def fake_phase(prompt, **kwargs):
                    phase_inputs.append((kwargs["phase"], kwargs["prd"]))
                    return AgentResult(status="success", input_tokens=10,
                                       output_tokens=5), 2.0, Path("unused.log"), "base-image"

                with patch.object(C, "RESULTS", results), \
                     patch("harness.course_experiment.OracleRelay", Relay), \
                     patch("harness.course_experiment._phase", side_effect=fake_phase), \
                     patch("harness.course_experiment.asyncio.to_thread", side_effect=immediate), \
                     patch("harness.course_experiment._json_message", side_effect=(
                         bad_summary if isinstance(bad_summary, Exception) else None),
                         return_value=(bad_summary if isinstance(bad_summary, dict) else None)), \
                     patch("harness.course_experiment.ua.mint_append_id", return_value=append_id), \
                     patch("harness.course_experiment.ua.init_settings"), \
                     patch("harness.course_experiment.ua.update_repo_status"), \
                     patch("harness.course_experiment.ua.fetch_interaction_log",
                           return_value=[{"question": "known?", "answer": "known answer"}]), \
                     patch("harness.course_experiment.ua.fetch_interaction_stats",
                           return_value={"ok": True}):
                    row = await _attempt("realcode@005", "B3", "free", 3,
                                         "fuzzy", "detailed", Path("image.tar"),
                                         args, {}, "batch")
                self.assertEqual(row["status"], "success", row)
                self.assertEqual([phase for phase, _ in phase_inputs],
                                 ["requirements", "coding"])
                self.assertIn("known answer", phase_inputs[1][1])
                self.assertIn("requirements_summary_error", row)
                self.assertTrue(row["oracle_log_mismatch"])
                self.assertFalse((results / append_id / "requirements_output.json").exists())

    async def test_self_group_still_requires_valid_summary(self):
        args = SimpleNamespace(user_host="127.0.0.1", user_init_port=50001,
                               user_model_name="Qwen3.8-Flash", model_name="Codex-config",
                               eval=False)
        phases = []

        async def fake_phase(prompt, **kwargs):
            phases.append(kwargs["phase"])
            return AgentResult(status="success", input_tokens=1,
                               output_tokens=1), 1.0, Path("unused.log"), "base-image"

        async def immediate(func, *args, **kwargs):
            return func(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            append_id = "a" * 32
            (Path(tmp) / append_id).mkdir()
            with patch.object(C, "RESULTS", Path(tmp)), \
                 patch("harness.course_experiment._phase", side_effect=fake_phase), \
                 patch("harness.course_experiment.asyncio.to_thread", side_effect=immediate), \
                 patch("harness.course_experiment._json_message",
                       return_value={"other": []}), \
                 patch("harness.course_experiment.ua.mint_append_id", return_value=append_id), \
                 patch("harness.course_experiment.ua.init_settings"), \
                 patch("harness.course_experiment.ua.update_repo_status"):
                row = await _attempt("realcode@005", "A′3", "self", 3,
                                     "fuzzy", "detailed", Path("image.tar"),
                                     args, {}, "batch")
        self.assertEqual(row["status"], "generation_error")
        self.assertEqual(phases, ["requirements"])

    async def test_requires_one_repo_and_runs_eight_groups(self):
        args = SimpleNamespace(repo="realcode@005,realcode@006", model_name="Codex-config",
                               user_model_name="Qwen3.8-Flash", max_retries=0,
                               groups=None)
        with self.assertRaises(SystemExit):
            await run(args)
        args.repo = "realcode@005"
        calls = []

        async def fake_attempt(alias, group, strategy, budget, *_rest):
            calls.append((alias, group, strategy, budget))
            return {"group": group, "status": "success", "append_id": group}

        with tempfile.TemporaryDirectory() as tmp:
            image_tar = Path(tmp) / "image.tar"
            image_tar.touch()
            with patch.object(C, "RESULTS", Path(tmp)), \
                 patch.object(C, "resolve_docker_tar", return_value=image_tar), \
                 patch("harness.course_experiment._attempt", side_effect=fake_attempt):
                result = await run(args)
        self.assertEqual(len(calls), 8)
        self.assertEqual(list(result["groups"]), [g[0] for g in GROUPS])
        self.assertTrue(all(c[0] == "realcode@005" for c in calls))
        self.assertIsNotNone(result["input_fingerprints"]["oracle_prompt"])


if __name__ == "__main__":
    unittest.main()
