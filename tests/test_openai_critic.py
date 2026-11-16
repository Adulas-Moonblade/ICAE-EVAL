"""Verify OpenAI-compatible Critic wiring without making a model call."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from harness import agentic, config
from harness.critic_pool import CriticPool
from harness.orchestrator import build_parser


class OpenAICriticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = config.resolve_critic_model("Deepseek-V4.1-Flash")[0]

    def test_config_uses_credential_reference(self):
        self.assertEqual(self.entry["OPENAI_MODEL"], "deepseek-v4.1-flash")
        self.assertEqual(self.entry["OPENAI_CREDENTIALS_FROM_USER_MODEL"], "Qwen3.8-Flash")
        self.assertNotIn("OPENAI_API_KEY", self.entry)
        self.assertEqual(build_parser().parse_args([
            "run", "--model-name", "Codex-config"]).critic_model_name,
            "Deepseek-V4.1-Flash")
        self.assertEqual(CriticPool.from_entries([self.entry]).endpoints[0].label,
                         "deepseek-v4.1-flash#Qwen3.8-Flash")

    async def test_forced_tool_call_uses_shared_endpoint(self):
        fake_response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls", message=SimpleNamespace(tool_calls=[
                SimpleNamespace(function=SimpleNamespace(
                    name="submit_review", arguments=json.dumps({
                        "semantic_similarity": 0.8, "api_similarity": 0.7,
                        "design_quality": 0.9})))
            ]))])
        create = AsyncMock(return_value=fake_response)
        fake_client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        with patch.object(config, "load_user_models", return_value={
                "Qwen3.8-Flash": [{"api_type": "openai", "model_name": "qwen",
                                    "base_url": "https://example.test/v1",
                                    "api_key": "dummy-key"}]}), \
                patch.object(agentic, "AsyncOpenAI", return_value=fake_client) as client_factory:
            status, data, error = await agentic._call_openai_critic(
                self.entry, "review this", 5)
        self.assertEqual((status, error), ("ok", ""))
        self.assertEqual(data["semantic_similarity"], 0.8)
        self.assertEqual(client_factory.call_args.kwargs["api_key"], "dummy-key")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-v4.1-flash")
        self.assertEqual(kwargs["tool_choice"]["function"]["name"], "submit_review")
        self.assertEqual(kwargs["messages"][0]["content"], "review this")

    async def test_missing_shared_endpoint_fails_without_api_call(self):
        with patch.object(config, "load_user_models", return_value={}):
            status, data, error = await agentic._call_openai_critic(
                self.entry, "review this", 5)
        self.assertEqual(status, "dead")
        self.assertIsNone(data)
        self.assertIn("Qwen3.8-Flash", error)


if __name__ == "__main__":
    unittest.main()
