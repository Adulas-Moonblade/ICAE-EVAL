"""Exercise the Codex subprocess boundary without a model or Docker daemon."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.codex_runner import run_agent
from harness.runner_common import RateLimited


FAKE_CODEX = '''#!/usr/bin/env python3
import json
import os
import sys
import time

with open(os.environ["FAKE_RECORD"], "w") as f:
    json.dump({"argv": sys.argv[1:], "prompt": sys.stdin.read(),
               "cwd": os.getcwd()}, f)

def event(data):
    print(json.dumps(data), flush=True)

event({"type": "thread.started", "thread_id": "test-thread"})
event({"type": "turn.started"})
case = os.environ["FAKE_CASE"]
if case == "timeout":
    time.sleep(10)
elif case == "scope_breach":
    event({"type": "item.started", "item": {
        "type": "command_execution", "command": "cat /benchmark/realcode_repos/answer.py"}})
    time.sleep(10)
elif case == "docker_breach":
    event({"type": "item.started", "item": {
        "type": "command_execution", "command": "docker run -v /:/host image"}})
    time.sleep(10)
elif case == "allowed_docker":
    event({"type": "item.started", "item": {
        "type": "command_execution", "command": "docker exec task-id bash -lc 'ls /workspace'"}})
    event({"type": "turn.completed", "usage": {"input_tokens": 1}})
elif case == "rate_limit":
    event({"type": "turn.failed", "error": {"message": "429 rate limit"}})
    sys.exit(1)
elif case == "failed":
    event({"type": "error", "message": "gateway failed"})
    sys.exit(1)
elif case == "incomplete":
    sys.exit(0)
else:
    event({"type": "item.completed", "item": {
        "type": "agent_message", "text": "finished"}})
    event({"type": "turn.completed", "usage": {
        "input_tokens": 120, "cached_input_tokens": 30,
        "output_tokens": 25}})
'''


class CodexRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = self.root / "codex"
        self.cli.write_text(FAKE_CODEX)
        self.cli.chmod(0o755)
        self.record = self.root / "invocation.json"
        self.log = self.root / "trajectory" / "task.log"

    async def invoke(self, case, model=None, timeout=5):
        env = {"ICAE_CODEX_CLI": str(self.cli), "FAKE_RECORD": str(self.record),
               "FAKE_CASE": case}
        with patch.dict(os.environ, env):
            return await run_agent("implement task", self.root, self.log,
                                   {"CODEX_MODEL": model} if model else {},
                                   timeout=timeout)

    async def test_success_keeps_events_and_usage(self):
        result = await self.invoke("success", model="example-model")
        self.assertEqual(result.status, "success")
        self.assertEqual((result.input_tokens, result.output_tokens,
                          result.cache_read_tokens, result.num_turns), (120, 25, 30, 1))
        self.assertEqual(result.cost_usd, 0.0)
        call = json.loads(self.record.read_text())
        self.assertTrue(call["prompt"].startswith("implement task"))
        self.assertIn("Codex host command scope", call["prompt"])
        self.assertEqual(call["cwd"], str(self.root))
        self.assertIn("--json", call["argv"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--model") + 1], "example-model")
        events = [json.loads(line) for line in self.log.with_suffix(".jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["type"], "turn.completed")
        self.assertEqual(events[0]["prompt"], call["prompt"])

    async def test_scope_guard_stops_obvious_host_access(self):
        result = await self.invoke("scope_breach")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.detail, "scope_violation:benchmark_or_credentials")
        events = [json.loads(line) for line in self.log.with_suffix(".jsonl").read_text().splitlines()]
        self.assertEqual(events[-1], {"type": "scope.violation", "rule": "benchmark_or_credentials"})
        self.assertEqual((await self.invoke("docker_breach")).detail,
                         "scope_violation:docker_container_control")
        self.assertEqual((await self.invoke("allowed_docker")).status, "success")

    async def test_error_and_incomplete_run(self):
        for case in ("failed", "incomplete"):
            with self.subTest(case=case):
                result = await self.invoke(case)
                self.assertEqual(result.status, "error")
                self.assertTrue(result.is_error)
                self.assertTrue(result.detail)

    async def test_rate_limit_is_retryable(self):
        with self.assertRaises(RateLimited):
            await self.invoke("rate_limit")

    async def test_timeout_kills_cli(self):
        result = await self.invoke("timeout", timeout=0.2)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.detail, "timeout_overall")
        self.assertIn("timeout_overall", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
