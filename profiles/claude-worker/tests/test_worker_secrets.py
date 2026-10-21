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

from tests.support import REPO, SCRIPTS, FakeBroker, granted

CONFIG = """plugins:
  entries:
    linear-agent-session:
      settings:
        claude_session:
          auth_token_env: "CLAUDE_PROXY_TOKEN"
"""
REFUSAL = {"ok": False, "state": "unavailable", "message": "The gateway holds no proxy token."}


def run_on_a_terminal(command: list, env: dict) -> tuple:
    primary, replica = pty.openpty()
    try:
        code = subprocess.run(command, env=env, stdout=replica, stderr=subprocess.PIPE).returncode
        # macOS drops unread pty output once the replica closes, so read while it is open.
        written = os.read(primary, 4096) if select.select([primary], [], [], 0.5)[0] else b""
    finally:
        os.close(replica)
        os.close(primary)
    return code, written


class HelperTestCase(unittest.TestCase):
    script_name = ""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.worker = self.root / "profiles" / "claude-worker"
        plugin = self.worker / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "linear.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "linear.py")
        self.script = self.worker / "scripts" / self.script_name
        self.script.parent.mkdir(parents=True)
        shutil.copy(SCRIPTS / self.script_name, self.script)
        (self.worker / "config.yaml").write_text(CONFIG)
        self.env = {**os.environ, "HERMES_HOME": str(self.worker)}

    def broker(self, *replies: dict) -> FakeBroker:
        broker = FakeBroker(self.root, proxy_token=list(replies))
        self.addCleanup(broker.close)
        return broker

    def run_script(self, *args: str) -> tuple:
        proc = subprocess.run([sys.executable, str(self.script), *args], env=self.env, capture_output=True, text=True)
        return proc.returncode, proc.stdout


class WorkerSecretsTest(HelperTestCase):
    script_name = "worker-secrets"

    def test_prints_only_the_proxy_token_from_the_gateways_broker(self) -> None:
        broker = self.broker(granted("t", None))

        self.assertEqual(self.run_script(), (0, "CLAUDE_PROXY_TOKEN=t\n"))
        self.assertEqual(broker.requests, ["proxy_token"])

    def test_without_auth_token_env_prints_nothing_and_asks_nothing(self) -> None:
        broker = self.broker(granted("t", None))
        for config in ("model: x\n", CONFIG.replace('"CLAUDE_PROXY_TOKEN"', '""')):
            with self.subTest(config=config):
                (self.worker / "config.yaml").write_text(config)

                self.assertEqual(self.run_script(), (0, ""))
        self.assertEqual(broker.requests, [])

    def test_prints_nothing_when_the_broker_refuses_or_is_down(self) -> None:
        broker = self.broker(REFUSAL)
        self.assertEqual(self.run_script(), (1, ""))
        broker.close()
        self.assertEqual(self.run_script(), (1, ""))

    def test_refuses_any_argument(self) -> None:
        self.assertEqual(self.run_script("--help"), (2, ""))

    def test_refuses_a_terminal_stdout(self) -> None:
        self.broker(granted("t", None))

        self.assertEqual(run_on_a_terminal([sys.executable, str(self.script)], self.env), (2, b""))


class ProxyTokenTest(HelperTestCase):
    script_name = "proxy-token"

    def test_prints_the_bare_token_for_claude_code(self) -> None:
        broker = self.broker(granted("t", None))

        self.assertEqual(self.run_script(), (0, "t\n"))
        self.assertEqual(broker.requests, ["proxy_token"])

    def test_asks_the_broker_on_every_run_and_keeps_nothing(self) -> None:
        self.broker(granted("first", None), granted("second", None))

        self.assertEqual([self.run_script(), self.run_script()], [(0, "first\n"), (0, "second\n")])
        self.assertEqual(sorted(path.name for path in self.worker.iterdir()), ["config.yaml", "plugins", "scripts"])

    def test_prints_nothing_when_the_broker_refuses_or_is_down(self) -> None:
        broker = self.broker(REFUSAL)
        self.assertEqual(self.run_script(), (1, ""))
        broker.close()
        self.assertEqual(self.run_script(), (1, ""))

    def test_refuses_any_argument_or_a_terminal(self) -> None:
        self.broker(granted("t", None))

        self.assertEqual(self.run_script("--print"), (2, ""))
        self.assertEqual(run_on_a_terminal([sys.executable, str(self.script)], self.env), (2, b""))


class SentryHeadersTest(HelperTestCase):
    script_name = "sentry-headers"

    def sentry_broker(self, *replies: dict) -> FakeBroker:
        broker = FakeBroker(self.root, sentry_token=list(replies))
        self.addCleanup(broker.close)
        return broker

    def test_prints_the_sentry_bearer_header_for_claude_code(self) -> None:
        broker = self.sentry_broker(granted("st", None))

        self.assertEqual(self.run_script(), (0, '{"Authorization": "Sentry-Bearer st"}\n'))
        self.assertEqual(broker.requests, ["sentry_token"])

    def test_prints_nothing_when_the_broker_refuses_or_is_down(self) -> None:
        broker = self.sentry_broker({"ok": False, "state": "unavailable", "message": "The gateway holds no Sentry token."})
        self.assertEqual(self.run_script(), (1, ""))
        broker.close()
        self.assertEqual(self.run_script(), (1, ""))

    def test_refuses_any_argument_or_a_terminal(self) -> None:
        self.sentry_broker(granted("st", None))

        self.assertEqual(self.run_script("--print"), (2, ""))
        self.assertEqual(run_on_a_terminal([sys.executable, str(self.script)], self.env), (2, b""))


if __name__ == "__main__":
    unittest.main()
