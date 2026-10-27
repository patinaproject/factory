from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
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
        (plugin / "broker_client.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "broker_client.py")
        self.script = self.worker / "scripts" / "refresh-gh-app-login"
        self.script.parent.mkdir(parents=True)
        shutil.copy(SCRIPTS / "refresh-gh-app-login", self.script)
        shutil.copy(SCRIPTS / "gh_login.py", self.script.parent)
        self.fake_gh = self.tmp / "fake-gh"
        self.fake_gh.write_text(with_interpreter(FAKE_GH))
        os.chmod(self.fake_gh, 0o755)
        self.gh_log = self.tmp / "gh.json"
        self.gh_config_dir = self.tmp / "gh"

    def broker(self, *replies: dict) -> FakeBroker:
        broker = FakeBroker(self.root / "factory-uploads.sock", github_token=list(replies))
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
        expiry = self.gh_config_dir / "token-expiry.json"
        self.assertEqual(json.loads(expiry.read_text()), {"expires_at": 1893456000.0})
        self.assertEqual(expiry.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.gh_config_dir.iterdir()), ["login.lock", "token-expiry.json"])

    def test_the_plist_runs_the_installed_script_every_15_minutes_with_its_directories(self) -> None:
        proc = subprocess.run([sys.executable, str(self.script), "--print-plist"], capture_output=True)

        job = plistlib.loads(proc.stdout)
        self.assertEqual(job["Label"], "ai.patinaproject.factory.gh-login")
        self.assertEqual(job["ProgramArguments"], ["/usr/bin/python3", str(self.script)])
        self.assertEqual((job["StartInterval"], job["RunAtLoad"]), (900, True))
        env = job["EnvironmentVariables"]
        self.assertEqual((env["GH_CONFIG_DIR"], env["HERMES_HOME"]), (str(self.worker / "gh"), str(self.worker)))
        self.assertEqual(job["StandardErrorPath"], str(self.worker / "logs" / "gh-login.log"))

    def test_a_failed_login_keeps_the_saved_expiry(self) -> None:
        self.broker(granted(TOKEN, 1893456000.0), granted(TOKEN, 1893459600.0))
        self.run_script()

        proc = self.run_script(FAKE_GH_EXIT="1")

        self.assertEqual(proc.returncode, 1)
        expiry = json.loads((self.gh_config_dir / "token-expiry.json").read_text())
        self.assertEqual(expiry, {"expires_at": 1893456000.0})

    def test_a_reply_without_an_expiry_fails_without_touching_the_login(self) -> None:
        self.broker(granted(TOKEN, None))

        proc = self.run_script()

        self.assert_failed(proc, "ask the broker: the reply holds no expires_at")

    def test_the_login_waits_for_the_lock_another_refresh_holds(self) -> None:
        broker = self.broker(granted(TOKEN, 1893456000.0))
        self.gh_config_dir.mkdir()
        with open(self.gh_config_dir / "login.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            env = {
                "PATH": os.environ["PATH"], "GH_BIN": str(self.fake_gh), "FAKE_GH_LOG": str(self.gh_log),
                "GH_CONFIG_DIR": str(self.gh_config_dir), "HERMES_HOME": str(self.worker),
            }
            proc = subprocess.Popen([sys.executable, str(self.script)], env=env, stdout=subprocess.PIPE, text=True)
            self.addCleanup(proc.stdout.close)
            time.sleep(1)
            self.assertIsNone(proc.poll())
            self.assertEqual(broker.requests, [])
        self.assertEqual((proc.wait(timeout=30), proc.stdout.read()), (0, ""))
        self.assertEqual(broker.requests, ["github_token"])

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
        self.assertFalse((self.gh_config_dir / "token-expiry.json").exists())


if __name__ == "__main__":
    unittest.main()
