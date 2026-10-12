from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "linear-mcp-headers"

FAKE_PLUGIN = """\
import json, os
def client_from_env(environ):
    class Client:
        def _access_token(self):
            return "token-for-" + environ["LINEAR_CLIENT_ID"] + "-" + environ["LINEAR_CLIENT_SECRET"] + "@" + environ["HERMES_HOME"]
    return Client()
"""


class LinearMcpHeadersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        plugin = self.home / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "linear.py").write_text(FAKE_PLUGIN)

    def run_script(self, **extra: str) -> dict:
        env = {key: value for key, value in os.environ.items() if not key.startswith("LINEAR_")}
        env["HERMES_HOME"] = str(self.home)
        env.update(extra)
        result = subprocess.run([sys.executable, "-I", str(SCRIPT)], env=env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_reads_the_credentials_from_the_home_env_file(self) -> None:
        (self.home / ".env").write_text('# comment\nLINEAR_CLIENT_ID="id1"\nexport LINEAR_CLIENT_SECRET=secret1\n')
        self.assertEqual(self.run_script(), {"Authorization": f"Bearer token-for-id1-secret1@{self.home}"})

    def test_reads_missing_credentials_from_the_secret_helper(self) -> None:
        (self.home / ".env").write_text("HELPER_INPUT=from-env\n")
        helper = self.home / "bin" / "factory-secrets"
        helper.parent.mkdir()
        helper.write_text('#!/bin/sh\necho "LINEAR_CLIENT_ID=$HELPER_INPUT"\necho "LINEAR_CLIENT_SECRET=\'s2\'"\necho "OTHER=x"\n')
        helper.chmod(0o700)
        self.assertEqual(self.run_script(), {"Authorization": f"Bearer token-for-from-env-s2@{self.home}"})

    def test_sends_the_run_token_a_worker_session_already_holds(self) -> None:
        self.assertEqual(self.run_script(LINEAR_API_KEY="run-token"), {"Authorization": "Bearer run-token"})

    def test_prints_no_headers_without_the_factory_plugin(self) -> None:
        empty = Path(tempfile.mkdtemp())
        env = {key: value for key, value in os.environ.items() if not key.startswith("LINEAR_")}
        env["HERMES_HOME"] = str(empty)
        result = subprocess.run([sys.executable, "-I", str(SCRIPT)], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {})


if __name__ == "__main__":
    unittest.main()
