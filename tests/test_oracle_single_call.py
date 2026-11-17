import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "user_agent"))
import main as oracle_service
import user_agent as oracle_core


class OracleSingleCallTests(unittest.TestCase):
    def test_one_model_call_returns_reply_without_protocol_validation(self):
        raw = json.dumps({"reply": "Known rule", "_internal_log": {
            "fallback_triggered": True}})
        with patch.object(oracle_core, "load_record", return_value={"oracle_data": {}}), \
             patch.object(oracle_core, "_call_with_rr", return_value=raw) as model:
            session = oracle_core.UserAgentSession("sample")
            result = session.ask("Known?", "model")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result["reply"], "Known rule")
        self.assertEqual(result["_raw_response"], raw)
        self.assertEqual(session.messages, [
            {"role": "user", "content": "Known?"},
            {"role": "assistant", "content": raw},
        ])

    def test_malformed_reply_is_returned_directly_and_kept_in_history(self):
        raw = "not valid JSON"
        with patch.object(oracle_core, "load_record", return_value={"oracle_data": {}}), \
             patch.object(oracle_core, "_call_with_rr", return_value=raw) as model:
            session = oracle_core.UserAgentSession("sample")
            result = session.ask("Question?", "model")
        self.assertEqual(model.call_count, 1)
        self.assertTrue(result["_parse_error"])
        self.assertEqual(result["reply"], raw)
        self.assertEqual(result["_raw_response"], raw)
        self.assertEqual(session.messages[-1]["content"], raw)

    def test_parser_keeps_original_lenient_json_extraction(self):
        result = oracle_core.parse_reply('```json\n{"reply": "yes"}\n```')
        self.assertEqual(result["reply"], "yes")
        self.assertEqual(result["_internal_log"], {})
        self.assertFalse(result.get("_parse_error", False))
        self.assertIn("_internal_log", oracle_service._safe_reply_fields(result)[2])
        self.assertFalse(oracle_core.parse_reply(
            '{"reply": "yes", "_parse_error": true}')["_parse_error"])


class OracleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_error_consumes_one_turn_and_raw_text_is_logged(self):
        append_id = "f" * 32
        task_id = "sample"
        raw = "not valid JSON"
        request = SimpleNamespace(json=AsyncMock(return_value={
            "append_id": append_id, "task_id": task_id, "question": "Question?"}))

        async def immediate(func, *args, **kwargs):
            return func(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(oracle_service, "LOG_DIR", Path(tmp)), \
             patch.object(oracle_service, "_save_session"), \
             patch.object(oracle_service.asyncio, "to_thread", side_effect=immediate), \
             patch.object(oracle_core, "load_record", return_value={"oracle_data": {}}), \
             patch.object(oracle_core, "_call_with_rr", return_value=raw) as model:
            session = oracle_core.UserAgentSession(task_id)
            record = {"sess": session, "count": 0, "lock": asyncio.Lock()}
            with patch.dict(oracle_service.valid_append_ids, {append_id: {
                     "max_interactions": 2, "model": "model"}}), \
                 patch.dict(oracle_service.sessions, {(append_id, task_id): record}):
                response = await oracle_service.chat_endpoint(request)
            log_path = Path(tmp) / task_id / f"{append_id}.jsonl"
            entries = [json.loads(line) for line in log_path.read_text().splitlines()]
            stats = oracle_service._calc_repo_stats(append_id, log_path, 2, "model")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(response["data"], raw)
        self.assertTrue(response["status"]["ok"])
        self.assertTrue(response["status"]["parse_error"])
        self.assertEqual(response["status"]["remaining"], 1)
        self.assertEqual(record["count"], 1)
        self.assertEqual(entries[0]["answer"], raw)
        self.assertEqual(entries[0]["raw_response"], raw)
        self.assertTrue(entries[0]["parse_error"])
        self.assertEqual(entries[0]["schema_errors"], [])
        self.assertFalse(entries[0]["fallback_triggered"])
        self.assertEqual(stats["fallbacks"], 0)
        self.assertEqual(stats["fallback_rate"], 0.0)

    async def test_bad_field_types_do_not_lose_turn_or_service_log(self):
        append_id = "e" * 32
        task_id = "sample"
        raws = [
            json.dumps({"reply": "known answer", "_internal_log": None}),
            json.dumps({"reply": ["wrong type"], "_internal_log": {
                "triggers_hit": ["C001", {"bad": True}],
                "fallback_triggered": "true", "api_alignment_triggered": 1,
                "cheating_attempt_detected": None, "score_adjustment": "1"}}),
        ]
        request = SimpleNamespace(json=AsyncMock(return_value={
            "append_id": append_id, "task_id": task_id, "question": "Question?"}))

        async def immediate(func, *args, **kwargs):
            return func(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(oracle_service, "LOG_DIR", Path(tmp)), \
             patch.object(oracle_service, "_save_session"), \
             patch.object(oracle_service.asyncio, "to_thread", side_effect=immediate), \
             patch.object(oracle_core, "load_record", return_value={"oracle_data": {}}), \
             patch.object(oracle_core, "_call_with_rr", side_effect=raws) as model:
            session = oracle_core.UserAgentSession(task_id)
            record = {"sess": session, "count": 0, "lock": asyncio.Lock()}
            with patch.dict(oracle_service.valid_append_ids, {append_id: {
                     "max_interactions": 2, "model": "model"}}), \
                 patch.dict(oracle_service.sessions, {(append_id, task_id): record}):
                responses = [await oracle_service.chat_endpoint(request) for _ in raws]
            log_path = Path(tmp) / task_id / f"{append_id}.jsonl"
            entries = [json.loads(line) for line in log_path.read_text().splitlines()]
            stats = oracle_service._calc_repo_stats(append_id, log_path, 2, "model")
        self.assertEqual(model.call_count, 2)
        self.assertEqual(record["count"], 2)
        self.assertEqual([r["data"] for r in responses], ["known answer", ""])
        self.assertTrue(all(r["status"]["ok"] for r in responses))
        self.assertEqual([e["raw_response"] for e in entries], raws)
        self.assertEqual(entries[0]["schema_errors"], ["_internal_log"])
        self.assertEqual(entries[1]["schema_errors"], [
            "reply", "_internal_log.triggers_hit", "_internal_log.fallback_triggered",
            "_internal_log.api_alignment_triggered",
            "_internal_log.cheating_attempt_detected",
            "_internal_log.score_adjustment"])
        self.assertEqual(entries[1]["triggers_hit"], ["C001"])
        self.assertFalse(entries[1]["fallback_triggered"])
        self.assertFalse(any(e["parse_error"] for e in entries))
        self.assertEqual(stats["turns_used"], 2)
        self.assertEqual(stats["fallbacks"], 0)


if __name__ == "__main__":
    unittest.main()
