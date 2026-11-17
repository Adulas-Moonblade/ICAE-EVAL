import json
import sys
import tempfile
import unittest
from pathlib import Path

# The Oracle service is run as a script from user_agent/, not as a package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "user_agent"))
from main import _calc_repo_stats


class UserAgentStatsTests(unittest.TestCase):
    def test_fallback_rate_uses_only_model_fallback_flag(self):
        entries = [
            {"turn": 1, "parse_error": True},
            {"turn": 2, "fallback_triggered": True},
            {"turn": 3, "parse_error": True, "fallback_triggered": True},
            {"turn": 4, "parse_error": False, "fallback_triggered": False},
            {"turn": 5, "error": "max_interactions_reached"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "synthetic_task" / "log.jsonl"
            log_file.parent.mkdir()
            log_file.write_text("".join(json.dumps(entry) + "\n" for entry in entries),
                                encoding="utf-8")
            stats = _calc_repo_stats("a" * 32, log_file, 5, "test-model")

        self.assertEqual(stats["turns_used"], 4)
        self.assertEqual(stats["fallbacks"], 2)
        self.assertEqual(stats["fallback_rate"], 50.0)


if __name__ == "__main__":
    unittest.main()
