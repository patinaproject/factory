from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, SCRIPTS, FakeBroker, granted, with_interpreter

TOKEN = "ghs_FAKEinstallationtoken"
REFUSAL = {"ok": False, "state": "unavailable", "message": "The gateway holds no GitHub App private key."}

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "w") as log:
    json.dump({
        "argv": sys.argv[1:],
        "gh_config_dir": os.environ.get("GH_CONFIG_DIR"),
        "gh_token": os.environ.get("GH_TOKEN"),
        "stdin": sys.stdin.read(),
    }, log)
sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
"""


class RefreshGhAppLoginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.worker = self.root / "profiles" / "claude-worker"
        plugin = self.worker / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "linear.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "linear.py")
        self.script = self.worker / "scripts" / "refresh-gh-app-login"
        self.script.parent.mkdir(parents=True)
        shutil.copy(SCRIPTS / "refresh-gh-app-login", self.script)
        self.fake_gh = self.tmp / "fake-gh"
        self.fake_gh.write_text(with_interpreter(FAKE_GH))
        os.chmod(self.fake_gh, 0o755)
        self.gh_log = self.tmp / "gh.json"
        self.gh_config_dir = self.tmp / "gh"

    def broker(self, *replies: dict) -> FakeBroker:
        broker = FakeBroker(self.root, github_token=list(replies))
        self.addCleanup(broker.close)
        return broker

    def run_script(self, **env_changes) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ["PATH"],
            "GH_BIN": str(self.fake_gh),
            "FAKE_GH_LOG": str(self.gh_log),
            "GH_CONFIG_DIR": str(self.gh_config_dir),
            "GH_TOKEN": "ambient-token",
            "HERMES_HOME": str(self.worker),
        }
        env.update(env_changes)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run([sys.executable, str(self.script)], capture_output=True, text=True, env=env)

    def assert_failed(self, proc: subprocess.CompletedProcess, line: str) -> None:
        self.assertEqual((proc.returncode, proc.stdout), (1, f"refresh-gh-app-login: {line}\n"))
        self.assertFalse(self.gh_log.exists())

    def test_signs_gh_in_with_the_token_the_broker_mints(self) -> None:
        broker = self.broker(granted(TOKEN, 1893456000.0))

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(broker.requests, ["github_token"])
        self.assertEqual(json.loads(self.gh_log.read_text()), {
            "argv": ["auth", "login", "--with-token", "--hostname", "github.com", "--insecure-storage"],
            "gh_config_dir": str(self.gh_config_dir),
            "gh_token": None,
            "stdin": TOKEN,
        })

    def test_a_broker_refusal_fails_without_touching_the_login(self) -> None:
        self.broker(REFUSAL)

        proc = self.run_script()

        self.assert_failed(
            proc,
            "ask the broker: the gateway's broker refused github_token: The gateway holds no GitHub App private key.",
        )

    def test_an_unreachable_broker_fails_without_touching_the_login(self) -> None:
        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stdout.startswith("refresh-gh-app-login: ask the broker: the gateway's broker at "), proc.stdout)
        self.assertFalse(self.gh_log.exists())

    def test_unset_gh_config_dir_is_refused_before_asking(self) -> None:
        broker = self.broker(granted(TOKEN))

        proc = self.run_script(GH_CONFIG_DIR=None)

        self.assert_failed(proc, "check environment: GH_CONFIG_DIR is not set; set it in the worker profile's .env")
        self.assertEqual(broker.requests, [])

    def test_gh_failure_is_reported_without_the_token(self) -> None:
        self.broker(granted(TOKEN))

        proc = self.run_script(FAKE_GH_EXIT="1")

        self.assertEqual((proc.returncode, proc.stdout), (1, "refresh-gh-app-login: gh auth login: exited 1\n"))
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
