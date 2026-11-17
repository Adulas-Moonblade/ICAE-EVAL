import contextlib
import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.course_report import PAIR_FIELDS, RUN_FIELDS, _oracle_log, _run_row, main, paired


class CourseReportTests(unittest.TestCase):
    def test_overall_uses_framework_native_and_enhanced_without_public(self):
        objective = {"public_visible": {"passed": 6, "total": 6},
                     "hidden": {"passed": 6, "total": 6},
                     "enhanced": {"passed": 19, "total": 39}}
        batch = {"batch_id": "batch", "repo": "realcode@005",
                 "model": {"CODEX_MODEL": "gpt-6-sol"},
                 "input_fingerprints": {"data": "abc"},
                 "codex_cli_config_sha256": "123"}
        row = {"strategy": "direct", "append_id": None, "status": "success",
               "requirements": {"status": "pre_satisfied", "seconds": 0,
                                "total_codex_tokens": 0},
               "coding": {"status": "pre_satisfied", "seconds": 1,
                          "total_codex_tokens": 10},
               "total_codex_tokens": 10, "total_time": 1,
               "actual_questions": 0, "fallback_count": 0,
               "objective": objective, "overall_pass_rate": 0.5556}
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_row(batch, "A", row, Path(tmp) / "summary.json",
                              Path(tmp), True)
        self.assertEqual(result["overall_passed"], 25)
        self.assertEqual(result["overall_total"], 45)
        self.assertAlmostEqual(result["overall_pass_rate"], 25 / 45)
        self.assertEqual(result["public_rate"], 1.0)
        self.assertEqual(result["hidden_rate"], 1.0)
        self.assertEqual(result["enhanced_rate"], 19 / 39)
        self.assertNotIn("overall_pass_rate_mismatch", result["issues"])

    def test_oracle_rates_use_framework_stats_and_check_log_counts(self):
        batch = {"batch_id": "batch", "repo": "realcode@005",
                 "model": {"CODEX_MODEL": "gpt-6-sol"},
                 "input_fingerprints": {"data": "abc"},
                 "codex_cli_config_sha256": "123"}
        row = {"strategy": "free", "append_id": None, "status": "success",
               "question_budget": 9, "actual_questions": 3, "fallback_count": 1,
               "interaction": {"ok": True, "constraint_coverage": 50.0,
                               "fallback_rate": 33.3, "budget_usage_rate": 33.3},
               "objective": {"hidden": {"passed": 1, "total": 1},
                             "enhanced": {"passed": 1, "total": 1}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            result = _run_row(batch, "B3", row, path / "summary.json", path, True)
            missing = _run_row(batch, "B3", {**row, "interaction": {"ok": False}},
                               path / "summary.json", path, True)
            inconsistent = _run_row(batch, "B3", {**row, "interaction": {
                **row["interaction"], "fallback_rate": 30.0,
                "budget_usage_rate": 40.0}}, path / "summary.json", path, True)
        self.assertEqual(result["fallback_rate_pct"], 33.3)
        self.assertEqual(result["budget_usage_pct"], 33.3)
        self.assertNotIn("fallback_rate_mismatch", result["issues"])
        self.assertNotIn("budget_usage_rate_mismatch", result["issues"])
        self.assertIsNone(missing["fallback_rate_pct"])
        self.assertIsNone(missing["budget_usage_pct"])
        self.assertIn("fallback_rate_mismatch", inconsistent["issues"])
        self.assertIn("budget_usage_rate_mismatch", inconsistent["issues"])

    def test_no_evaluation_is_missing_score_while_generation_failure_is_zero(self):
        batch = {"batch_id": "batch", "repo": "realcode@005", "model_name": "test",
                 "model": {"CODEX_MODEL": "gpt-6-sol"}, "oracle_model": "oracle",
                 "input_fingerprints": {"data": "abc"}}
        base = {"strategy": "direct", "append_id": "a" * 32,
                "requirements": {"status": "pre_satisfied", "seconds": 0,
                                 "total_codex_tokens": 0},
                "coding": {"status": "pre_satisfied", "seconds": 1,
                           "total_codex_tokens": 10},
                "total_codex_tokens": 10, "total_time": 1,
                "actual_questions": 0, "fallback_count": 0}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            no_eval = _run_row(batch, "A", {**base, "status": "success"},
                               path / "summary.json", path, True)
            self.assertIsNone(no_eval["overall_pass_rate"])
            self.assertFalse(no_eval["quality_ready"])
            failed = _run_row(batch, "A", {**base, "status": "generation_failed",
                        "objective": {"error": "no rcb_tests/test.sh in generated code"}},
                        path / "summary.json", path, True)
            self.assertEqual(failed["overall_pass_rate"], 0.0)
            self.assertTrue(failed["quality_ready"])

    def test_paired_deltas_are_right_minus_left(self):
        a = {"batch_id": "b", "repo": "realcode@005", "group": "A",
             "model_name": "m", "codex_model": "gpt-6-sol", "reasoning_effort": "medium",
             "oracle_model": "oracle",
             "quality_ready": True, "study_ready": True, "overall_pass_rate": 0.2,
             "total_codex_tokens": 100, "total_time_s": 10, "actual_questions": 0}
        b = {**a, "group": "B3", "overall_pass_rate": 0.5,
             "total_codex_tokens": 130, "total_time_s": 15, "actual_questions": 2}
        result = next(item for item in paired([a, b]) if item["right_group"] == "B3")
        self.assertEqual(result["delta_overall_pass_rate"], 0.3)
        self.assertEqual(result["delta_total_codex_tokens"], 30)
        self.assertEqual(result["delta_actual_questions"], 2)

    def test_late_relay_reply_is_flagged_against_service_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            append_id = "a" * 32
            directory = root / append_id
            directory.mkdir()
            (directory / "oracle_service.jsonl").write_text(json.dumps({
                "answer": "yes", "question": "first", "fallback_triggered": False,
                "triggers_hit": ["C001"]}) + "\n")
            reply = {"response": {"status": {"ok": True}}}
            (directory / "oracle_raw.jsonl").write_text(
                json.dumps(reply) + "\n" + json.dumps(reply) + "\n")
            issues = []
            _oracle_log(root, append_id, {"actual_questions": 1,
                        "confirmed_questions": 1, "fallback_count": 0,
                        "oracle_response_length": 3}, issues)
            self.assertIn("oracle_relay_service_count_mismatch", issues)

    def test_single_batch_export_copies_only_evidence_and_records_missing_retry_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "results"
            run_dir = root / "course_runs" / "batch-one"
            run_dir.mkdir(parents=True)
            other_dir = root / "course_runs" / "batch-two"
            other_dir.mkdir()
            (other_dir / "summary.json").write_text(json.dumps({
                "batch_id": "batch-two", "repo": "other", "groups": {"A": {}}}))
            failed_id, final_id = "a" * 32, "b" * 32
            batch = {"batch_id": "batch-one", "repo": "realcode@005",
                     "model_name": "test", "model": {"CODEX_MODEL": "gpt-6-sol"},
                     "oracle_model": "oracle", "input_fingerprints": {"data": "abc"},
                     "codex_cli_config_sha256": "123",
                     "groups": {"B3": {
                         "append_id": final_id, "strategy": "free", "status": "success",
                         "question_budget": 3, "actual_questions": 1,
                         "confirmed_questions": 1, "fallback_count": 0,
                         "oracle_response_length": 3,
                         "interaction": {"ok": True, "constraint_coverage": 50,
                                         "fallback_rate": 0},
                         "requirements": {"status": "success", "input_tokens": 2,
                                          "output_tokens": 3, "total_codex_tokens": 5,
                                          "seconds": 2},
                         "coding": {"status": "success", "input_tokens": 4,
                                    "output_tokens": 5, "total_codex_tokens": 9,
                                    "seconds": 3},
                         "total_codex_tokens": 14, "total_time": 5,
                         "objective": {tier: {"passed": 1, "total": 1} for tier in
                                       ("public_visible", "hidden", "enhanced")},
                         "attempts": [{"append_id": failed_id,
                                       "status": "infrastructure_error"},
                                      {"append_id": final_id, "status": "success"}],
                     }}}
            (run_dir / "summary.json").write_text(json.dumps(batch))
            failed = root / failed_id
            (failed / "trajectory").mkdir(parents=True)
            (failed / "trajectory" / "realcode_005.requirements.jsonl").write_text("{}\n")
            final = root / final_id
            (final / "trajectory").mkdir(parents=True)
            (final / "realcode@005").mkdir()
            eval_dir = final / "_eval" / "realcode@005"
            eval_dir.mkdir(parents=True)
            (final / "settings.json").write_text("{}\n")
            (final / "requirements_output.json").write_text('{"questions": []}\n')
            (final / "realcode@005" / "start.md").write_text("coding instructions\n")
            (final / "secret.txt").write_text("should stay in the source\n")
            for phase, inputs, outputs in (("requirements", 2, 3), ("coding", 4, 5)):
                (final / "trajectory" / f"realcode_005.{phase}.jsonl").write_text(
                    json.dumps({"type": "turn.completed", "usage": {
                        "input_tokens": inputs, "output_tokens": outputs}}) + "\n")
            (final / "oracle_service.jsonl").write_text(json.dumps({
                "answer": "yes", "triggers_hit": ["C001"]}) + "\n")
            (final / "oracle_raw.jsonl").write_text(json.dumps({
                "response": {"status": {"ok": True}}}) + "\n")
            (eval_dir / "objective.json").write_text(json.dumps(batch["groups"]["B3"]["objective"]))
            for tier in ("public_test_cases", "test_cases", "enhanced_test_cases"):
                (eval_dir / f".build_log_{tier}.txt").write_text("test output\n")
            outcap = eval_dir / "outcap_test_cases"
            outcap.mkdir()
            (outcap / "case.txt").write_text("large output\n")
            args = ["course_report", "--run-dir", str(run_dir)]
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                main()
            exported = root / "course_exports" / "batch-one"
            with (exported / "runs.csv").open(encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, list(RUN_FIELDS))
                rows = list(reader)
            self.assertEqual([r["group"] for r in rows], ["B3"])
            self.assertEqual(len(RUN_FIELDS), 23)
            self.assertEqual(len(PAIR_FIELDS), 11)
            self.assertNotIn("codex_model", rows[0])
            self.assertNotIn("pair_summary.csv", {p.name for p in exported.iterdir()})
            self.assertTrue((exported / "README.md").is_file())
            self.assertEqual(json.loads((exported / "summary.json").read_text())["model"],
                             batch["model"])
            manifest = json.loads((exported / "manifest.json").read_text())
            copied = {item["archive_path"]: item for item in manifest["files"]}
            self.assertIn("summary.json", copied)
            self.assertTrue(any("attempt-01-" in name and name.endswith("requirements.jsonl")
                                for name in copied))
            self.assertTrue(any(name.endswith("oracle_raw.jsonl") for name in copied))
            self.assertTrue(any(name.endswith("objective.json") for name in copied))
            self.assertTrue(any(name.endswith("start.md") for name in copied))
            self.assertFalse(any("secret.txt" in name or "outcap_" in name for name in copied))
            self.assertEqual(len(manifest["missing"]), 1)
            self.assertTrue(manifest["missing"][0]["archive_path"].endswith("settings.json"))
            for relative, item in copied.items():
                self.assertEqual(hashlib.sha256((exported / relative).read_bytes()).hexdigest(),
                                 item["sha256"])
            with patch.object(sys, "argv", args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main()
            self.assertEqual((exported / "summary.json").read_text(),
                             (run_dir / "summary.json").read_text())

    def test_cross_batch_export_removes_obsolete_pair_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs_dir = root / "course_runs" / "batch-one"
            runs_dir.mkdir(parents=True)
            (runs_dir / "summary.json").write_text(json.dumps({
                "batch_id": "batch-one", "repo": "realcode@005",
                "model": {"CODEX_MODEL": "gpt-6-sol"},
                "groups": {"A": {"append_id": None, "strategy": "direct",
                                 "status": "generation_error"}}}))
            output = root / "course_report"
            output.mkdir()
            (output / "pair_summary.csv").write_text("old data\n")
            args = ["course_report", "--course-runs", str(root / "course_runs"),
                    "--results-root", str(root), "--output-dir", str(output)]
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertFalse((output / "pair_summary.csv").exists())
            with (output / "runs.csv").open(encoding="utf-8-sig") as stream:
                self.assertEqual(next(csv.reader(stream)), list(RUN_FIELDS))


if __name__ == "__main__":
    unittest.main()
