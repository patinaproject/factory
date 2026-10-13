from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

from tests.support import SCRIPTS


def load(name: str):
    loader = SourceFileLoader(name.replace("-", "_"), str(SCRIPTS / name))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


helper = load("wait-for-checks")
guard = load("guard-ci-wait")
PROMPT_CACHE_SECONDS = 300


def checks(*buckets: str) -> list[dict]:
    return [{"name": f"check-{i}", "bucket": bucket, "link": f"https://example.test/{i}"} for i, bucket in enumerate(buckets)]


class Clock:
    """A fake clock whose sleep advances it, so a wait runs instantly and its length is readable."""

    def __init__(self) -> None:
        self.now = 0.0
        self.fetches = 0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def fetch_always(self, result):
        def fetch():
            self.fetches += 1
            return result
        return fetch


class BudgetTests(unittest.TestCase):
    def test_the_budget_and_the_tool_timeout_stay_inside_the_prompt_cache(self) -> None:
        # The last poll starts at least POLL_SECONDS before the deadline and gh may take GH_SECONDS.
        worst_case = helper.BUDGET_SECONDS - helper.POLL_SECONDS + helper.GH_SECONDS
        self.assertLess(worst_case, guard.MAX_WAIT_SECONDS)
        self.assertLess(guard.MAX_WAIT_SECONDS, PROMPT_CACHE_SECONDS)
        self.assertEqual(guard.TOOL_TIMEOUT_MS, guard.MAX_WAIT_SECONDS * 1000)

    def test_a_wait_on_pending_checks_ends_inside_the_budget(self) -> None:
        clock = Clock()

        state, _ = helper.wait(clock.fetch_always(checks("pass", "pending")), clock=lambda: clock.now, sleep=clock.sleep)

        self.assertEqual(state, "pending")
        self.assertLessEqual(clock.now, helper.BUDGET_SECONDS)
        self.assertGreater(clock.fetches, 1)

    def test_checks_that_finish_end_the_wait_at_once(self) -> None:
        for buckets, expected in [(("pass", "skipping"), "pass"), (("pass", "pending", "fail"), "fail"), (("cancel",), "fail")]:
            clock = Clock()
            state, _ = helper.wait(clock.fetch_always(checks(*buckets)), clock=lambda: clock.now, sleep=clock.sleep)
            self.assertEqual((state, clock.now, clock.fetches), (expected, 0, 1), buckets)

    def test_no_checks_yet_is_pending(self) -> None:
        self.assertEqual(helper.verdict([]), "pending")


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir))

    def run_helper(self, stdout: str, stderr: str = "", code: int = 0) -> subprocess.CompletedProcess:
        gh = self.dir / "gh"
        gh.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\nsys.exit({code})\n"
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        env = {**os.environ, "GH_BIN": str(gh)}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "wait-for-checks"), "7", "--repo", "example-org/example-repo"],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_exit_codes_follow_the_checks(self) -> None:
        passed = self.run_helper(json.dumps(checks("pass")))
        failed = self.run_helper(json.dumps(checks("pass", "fail")), code=1)
        self.assertEqual((passed.returncode, failed.returncode), (0, 1))
        self.assertIn("fail\tcheck-1\thttps://example.test/1", failed.stdout)

    def test_a_gh_failure_is_exit_2_with_its_message(self) -> None:
        result = self.run_helper("", stderr="HTTP 401: Bad credentials", code=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Bad credentials", result.stderr)


class GuardTests(unittest.TestCase):
    def decide(self, command: str, tool: str = "Bash", **extra) -> tuple[int, str]:
        return guard.decide({"tool_name": tool, "tool_input": {"command": command, **extra}})

    def test_long_ci_waits_are_refused(self) -> None:
        for command in [
            "gh pr checks 7 --repo o/r --watch --fail-fast",
            "gh pr checks 7 -w",
            "GH_CONFIG_DIR=x perl watch.pl gh pr checks 7 --watch",
            "gh run watch 123",
            "for i in $(seq 1 110); do gh pr checks 7 | grep -q pending || break; sleep 30; done",
            "until gh pr checks 7 | grep -q pass; do sleep 20; done",
            "sleep 600",
            "sleep 10m",
        ]:
            code, text = self.decide(command)
            self.assertEqual(code, 2, command)
            self.assertIn("wait-for-checks", text)

    def test_short_waits_and_other_calls_pass_unchanged(self) -> None:
        for command in ["sleep 30", "sleep 4m", "gh pr checks 7 --repo o/r", "for f in a b; do echo $f; sleep 5; done", "git status"]:
            self.assertEqual(self.decide(command), (0, ""), command)
        self.assertEqual(self.decide("gh pr checks 7 --watch", tool="Read"), (0, ""))

    def test_the_helper_gets_a_timeout_that_outlasts_its_budget(self) -> None:
        code, text = self.decide("python3 /w/scripts/wait-for-checks 7 --repo o/r", timeout=120000, description="wait")
        update = json.loads(text)["hookSpecificOutput"]["updatedInput"]

        self.assertEqual(code, 0)
        self.assertEqual(update["timeout"], guard.TOOL_TIMEOUT_MS)
        self.assertGreater(update["timeout"] / 1000, helper.BUDGET_SECONDS)
        self.assertEqual(update["description"], "wait")

    def test_the_helper_cannot_smuggle_a_long_wait(self) -> None:
        self.assertEqual(self.decide("python3 wait-for-checks 7 --repo o/r; sleep 900")[0], 2)

    def test_the_hook_runs_as_a_command_on_stdin(self) -> None:
        for command, code in [("gh pr checks 7 --watch", 2), ("git status", 0)]:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "guard-ci-wait")], capture_output=True, text=True,
                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            )
            self.assertEqual(result.returncode, code, command)


if __name__ == "__main__":
    unittest.main()
