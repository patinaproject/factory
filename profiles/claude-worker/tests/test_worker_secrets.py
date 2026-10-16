from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS

ROOT_HELPER = """#!/bin/sh
[ -n "$FAKE_HELPER_FAILS" ] && exit 1
[ -n "$FAKE_HELPER_EMPTY" ] && exit 0
cat <<'EOF'
LINEAR_WEBHOOK_SECRET=x
LINEAR_CLIENT_ID=id
GITHUB_WEBHOOK_SECRET=x
SLACK_BOT_TOKEN=x
CLAUDE_PROXY_TOKEN=t
OP_SERVICE_ACCOUNT_TOKEN=x
FACTORY_INFISICAL_CLIENT_SECRET=x
LINEAR_CLIENT_SECRET=s
FACTORY_MCP_NOTION_TOKEN=n
FACTORY_MCP_lower=x
FACTORY_MCPX_TOKEN=x
EOF
"""

CONFIG = """plugins:
  entries:
    linear-agent-session:
      settings:
        claude_session:
          auth_token_env: "CLAUDE_PROXY_TOKEN"
"""

FORBIDDEN = ("LINEAR_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET", "SLACK_", "OP_", "FACTORY_INFISICAL_")


class WorkerSecretsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        root = self.tmp / "root"
        helper = root / "bin" / "factory-secrets"
        helper.parent.mkdir(parents=True)
        helper.write_text(ROOT_HELPER)
        helper.chmod(0o700)
        self.worker = root / "profiles" / "claude-worker"
        self.script = self.worker / "scripts" / "worker-secrets"
        self.script.parent.mkdir(parents=True)
        shutil.copy(SCRIPTS / "worker-secrets", self.script)
        (self.worker / "config.yaml").write_text(CONFIG)
        self.env = {**os.environ}
        for name in ("FAKE_HELPER_FAILS", "FAKE_HELPER_EMPTY"):
            self.env.pop(name, None)

    def run_script(self, *args: str, **env: str) -> tuple:
        proc = subprocess.run(
            [sys.executable, str(self.script), *args], env={**self.env, **env}, capture_output=True, text=True
        )
        return proc.returncode, proc.stdout

    def test_prints_only_the_allowlisted_lines_in_the_helper_order(self) -> None:
        code, out = self.run_script()

        self.assertEqual((code, out), (0, "LINEAR_CLIENT_ID=id\nCLAUDE_PROXY_TOKEN=t\nLINEAR_CLIENT_SECRET=s\nFACTORY_MCP_NOTION_TOKEN=n\n"))
        for name in FORBIDDEN:
            self.assertNotIn(name, out)

    def test_without_auth_token_env_prints_only_the_linear_lines(self) -> None:
        for config in ("model: x\n", CONFIG.replace('"CLAUDE_PROXY_TOKEN"', '""')):
            with self.subTest(config=config):
                (self.worker / "config.yaml").write_text(config)

                self.assertEqual(self.run_script(), (0, "LINEAR_CLIENT_ID=id\nLINEAR_CLIENT_SECRET=s\nFACTORY_MCP_NOTION_TOKEN=n\n"))

    def test_refuses_any_argument(self) -> None:
        self.assertEqual(self.run_script("--help"), (2, ""))

    def test_refuses_a_terminal_stdout(self) -> None:
        primary, replica = pty.openpty()
        try:
            code = subprocess.run(
                [sys.executable, str(self.script)], env=self.env, stdout=replica, stderr=subprocess.PIPE
            ).returncode
            # macOS drops unread pty output once the replica closes, so read while it is open.
            written = os.read(primary, 4096) if select.select([primary], [], [], 0.5)[0] else b""
        finally:
            os.close(replica)
            os.close(primary)

        self.assertEqual((code, written), (2, b""))

    def test_prints_nothing_when_the_root_helper_fails_or_prints_nothing(self) -> None:
        self.assertEqual(self.run_script(FAKE_HELPER_FAILS="1"), (1, ""))
        self.assertEqual(self.run_script(FAKE_HELPER_EMPTY="1"), (1, ""))


if __name__ == "__main__":
    unittest.main()
