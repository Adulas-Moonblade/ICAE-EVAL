import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.docker_env import _codex_gateway_no_proxy


class DockerProxyTests(unittest.TestCase):
    def test_model_gateway_joins_existing_proxy_bypasses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                'model_provider = "test"\n'
                '[model_providers.test]\n'
                'base_url = "http://114.212.189.33:4000"\n',
                encoding="utf-8")
            docker_home = root / "docker"
            docker_home.mkdir()
            (docker_home / "config.json").write_text(json.dumps({
                "proxies": {"default": {"noProxy": "localhost,127.0.0.1"}}}),
                encoding="utf-8")
            with patch.dict(os.environ, {
                "DOCKER_CONFIG": str(docker_home),
                "NO_PROXY": "::1", "no_proxy": "localhost",
            }):
                bypass = _codex_gateway_no_proxy(codex_home, "127.0.0.1")

        self.assertEqual(bypass,
                         "localhost,127.0.0.1,::1,114.212.189.33")


if __name__ == "__main__":
    unittest.main()
