from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import SCRIPTS

FAKE = f"""#!{sys.executable}
import json, os, subprocess, sys, time
log = os.environ["FAKE_LOG"]
record = {{"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid()}}
if os.environ.get("FAKE_HOLD"):
    record["descendant"] = subprocess.Popen(["sleep", "600"], start_new_session=True).pid
json.dump(record, open(log + ".tmp", "w"))
os.rename(log + ".tmp", log)
if os.environ.get("FAKE_HOLD"):
    time.sleep(600)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


class NestedClaudeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.claude = self.tmp / "claude"
        self.claude.write_text(FAKE)
        os.chmod(self.claude, 0o700)
        self.settings = self.tmp / "settings.json"
        self.settings.write_text("{}")
        self.mcp = self.tmp / "mcp.json"
        self.mcp.write_text("{}")
        self.slots = self.tmp / "slots"
        self.slots.mkdir()
        self.log = self.tmp / "call.json"

    def command(self, *caller: str, lifetime: float = 30, base_url: str | None = None) -> list[str]:
        return [
            sys.executable, str(SCRIPTS / "nested-claude"),
            "--claude", str(self.claude), "--settings", str(self.settings), "--mcp-config", str(self.mcp),
            "--slots", str(self.slots), "--lifetime", str(lifetime),
            *(["--base-url", base_url] if base_url else []), "--", *caller,
        ]

    def env(self, **extra: str) -> dict:
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("CLAUDE_SESSION_NESTED", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                         "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL")
        }
        return {**env, "FAKE_LOG": str(self.log), **extra}

    def run_wrapper(self, *caller: str, env: dict | None = None, **options) -> subprocess.CompletedProcess:
        return subprocess.run(self.command(*caller, **options), env=env or self.env(), capture_output=True, text=True, timeout=60)

    def call(self) -> dict:
        return json.loads(self.log.read_text())

    def assert_refused(self, proc: subprocess.CompletedProcess, reason: str) -> None:
        self.assertEqual((proc.returncode, proc.stderr), (2, f"nested-claude: refused: {reason}\n"))
        self.assertFalse(self.log.exists())

    def test_caller_options_come_before_the_run_flags(self) -> None:
        proc = self.run_wrapper("-p", "--model", "m", "say hi")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.call()["argv"], [
            "-p", "--model", "m", "say hi", "--settings", str(self.settings), "--mcp-config", str(self.mcp),
        ])

    def test_a_caller_double_dash_keeps_its_arguments_after_the_run_flags(self) -> None:
        self.run_wrapper("-p", "--", "--not-an-option")

        self.assertEqual(self.call()["argv"], [
            "-p", "--settings", str(self.settings), "--mcp-config", str(self.mcp), "--", "--not-an-option",
        ])

    def test_the_child_is_marked_nested_and_gets_nothing_else(self) -> None:
        env = self.env(EXAMPLE_KEEP="kept")

        self.run_wrapper("-p", "hi", env=env)

        self.assertEqual(self.call()["env"], {**env, "CLAUDE_SESSION_NESTED": "1"})

    def test_the_child_exit_code_is_the_wrapper_exit_code(self) -> None:
        self.assertEqual(self.run_wrapper("-p", "hi", env=self.env(FAKE_EXIT="7")).returncode, 7)

    def test_a_matching_endpoint_is_accepted(self) -> None:
        proc = self.run_wrapper("-p", "hi", env=self.env(ANTHROPIC_BASE_URL="http://127.0.0.1:9999"), base_url="http://127.0.0.1:9999")

        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_refusals(self) -> None:
        cases = (
            ("recursion", ("-p", "hi"), {"CLAUDE_SESSION_NESTED": "1"}, {},
             "CLAUDE_SESSION_NESTED is set, so this claude is already nested and cannot start another"),
            ("api key", ("-p", "hi"), {"ANTHROPIC_API_KEY": "synthetic-key"}, {},
             "ANTHROPIC_API_KEY in the environment would conflict with the run's apiKeyHelper; unset it"),
            ("auth token", ("-p", "hi"), {"ANTHROPIC_AUTH_TOKEN": "synthetic-key", "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-oauth"}, {},
             "ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN in the environment would conflict with the run's apiKeyHelper; unset it"),
            ("endpoint", ("-p", "hi"), {"ANTHROPIC_BASE_URL": "http://elsewhere.invalid"}, {"base_url": "http://127.0.0.1:9999"},
             "ANTHROPIC_BASE_URL differs from the run's endpoint; unset it or leave the run's value"),
            ("settings flag", ("-p", "hi", "--settings", "/tmp/x.json"), {}, {},
             "--settings is set by the run; pass settings another way or not at all"),
            ("settings value", ("--settings={}", "-p", "hi"), {}, {},
             "--settings is set by the run; pass settings another way or not at all"),
        )
        for name, caller, env, options, reason in cases:
            with self.subTest(name):
                proc = self.run_wrapper(*caller, env=self.env(**env), **options)

                self.assert_refused(proc, reason)
                self.assertNotIn("synthetic", proc.stderr)

    def test_a_run_that_ended_refuses_its_wrapper(self) -> None:
        self.settings.unlink()

        self.assert_refused(
            self.run_wrapper("-p", "hi"),
            "the run's nested settings file is gone, so the run that set up this wrapper has ended",
        )

    def test_all_slots_held_refuses_a_fifth_call(self) -> None:
        held = []
        for index in range(4):
            handle = open(self.slots / f"{index}.lock", "a")
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(handle)
        self.addCleanup(lambda: [handle.close() for handle in held])

        self.assert_refused(self.run_wrapper("-p", "hi"), "all 4 nested claude slots are in use; wait for one to finish")

        held.pop().close()
        self.assertEqual(self.run_wrapper("-p", "hi").returncode, 0)

    def wait_for_call(self, proc: subprocess.Popen) -> dict:
        deadline = time.monotonic() + 30
        while not self.log.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                self.fail(proc.communicate())
            time.sleep(0.05)
        return self.call()

    def assert_gone(self, *pids: int) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(alive(pid) for pid in pids):
                return
            time.sleep(0.1)
        for pid in pids:
            with_ignored(os.kill, pid, signal.SIGKILL)
        self.fail(f"still running: {[pid for pid in pids if alive(pid)]}")

    def test_the_lifetime_limit_ends_the_child_and_its_descendants(self) -> None:
        proc = subprocess.Popen(
            self.command("-p", "hi", lifetime=2), env=self.env(FAKE_HOLD="1"), stderr=subprocess.PIPE, text=True,
        )
        call = self.wait_for_call(proc)

        _, stderr = proc.communicate(timeout=30)

        self.assertEqual((proc.returncode, stderr), (124, "nested-claude: ended claude after 2 s (lifetime limit)\n"))
        self.assert_gone(call["pid"], call["descendant"])

    def test_a_stop_signal_ends_the_child_and_its_descendants(self) -> None:
        proc = subprocess.Popen(self.command("-p", "hi"), env=self.env(FAKE_HOLD="1"))
        call = self.wait_for_call(proc)

        proc.send_signal(signal.SIGTERM)

        self.assertEqual(proc.wait(timeout=30), 128 + signal.SIGTERM)
        self.assert_gone(call["pid"], call["descendant"])


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def with_ignored(call, *args) -> None:
    try:
        call(*args)
    except ProcessLookupError:
        pass


if __name__ == "__main__":
    unittest.main()
