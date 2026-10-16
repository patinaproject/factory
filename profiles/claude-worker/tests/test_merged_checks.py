from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, with_interpreter

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_GH_STATE"]))
if state.get("fails"):
    sys.exit("gh: HTTP 502")
assert sys.argv[1:3] == ["pr", "view"], sys.argv
print(json.dumps(state["pr"]))
"""


def run(name: str, workflow: str, conclusion: str, started: str) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": workflow,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "startedAt": started,
    }


class MergedChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "bin").mkdir()
        gh = self.dir / "bin" / "gh"
        gh.write_text(with_interpreter(FAKE_GH))
        os.chmod(gh, 0o755)
        self.state = self.dir / "state.json"

    def sort(self, rollup, names, fails=False) -> tuple[int, dict]:
        self.state.write_text(json.dumps({"fails": fails, "pr": {"headRefOid": "abc", "statusCheckRollup": rollup}}))
        env = {**os.environ, "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "FAKE_GH_STATE": str(self.state)}
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "merged_checks.py"), "https://github.com/o/r/pull/1", *names],
            capture_output=True, text=True, env=env,
        )
        return proc.returncode, json.loads(proc.stdout)

    def test_a_failed_run_replaced_by_a_newer_pass_is_superseded(self):
        rollup = [
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:09:58Z"),
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"),
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:10:16Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"]), (0, {"head_sha": "abc", "real": [], "superseded": ["Validate"]}))

    def test_a_newest_run_that_failed_is_real(self):
        rollup = [
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:09:58Z"),
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:25:56Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"])[1]["real"], ["Validate"])

    def test_a_tie_keeps_the_run_that_did_not_pass(self):
        rollup = [
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"),
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:25:56Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"])[1]["real"], ["Validate"])

    def test_a_run_that_has_not_started_is_the_newest(self):
        pending = {**run("Validate", "Pull request", "", "0001-01-01T00:00:00Z"), "status": "QUEUED", "conclusion": None}
        rollup = [run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"), pending]
        self.assertEqual(self.sort(rollup, ["Validate"])[1]["real"], ["Validate"])

    def test_one_workflow_that_still_fails_makes_a_shared_name_real(self):
        rollup = [
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"),
            run("Validate", "Release", "FAILURE", "2026-10-16T01:00:00Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"])[1]["real"], ["Validate"])

    def test_a_status_context_is_judged_by_its_state(self):
        failing = {"__typename": "StatusContext", "context": "Vercel", "state": "FAILURE", "startedAt": "2026-10-16T01:00:00Z"}
        passing = {**failing, "state": "SUCCESS", "startedAt": "2026-10-16T02:00:00Z"}
        self.assertEqual(self.sort([failing, passing], ["Vercel"])[1]["superseded"], ["Vercel"])

    def test_a_failed_run_replaced_by_a_newer_skip_is_superseded(self):
        rollup = [
            run("Deploy", "Pull request", "FAILURE", "2026-10-16T04:09:58Z"),
            run("Deploy", "Pull request", "SKIPPED", "2026-10-16T04:25:56Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Deploy"])[1]["superseded"], ["Deploy"])

    def test_a_check_the_rollup_does_not_show_is_real(self):
        self.assertEqual(self.sort([], ["Gone"])[1], {"head_sha": "abc", "real": ["Gone"], "superseded": []})

    def test_mixed_names_are_sorted_each_on_their_own_newest_run(self):
        rollup = [
            run("A", "W", "FAILURE", "2026-10-16T01:00:00Z"),
            run("A", "W", "SUCCESS", "2026-10-16T02:00:00Z"),
            run("B", "W", "SUCCESS", "2026-10-16T01:00:00Z"),
            run("B", "W", "FAILURE", "2026-10-16T02:00:00Z"),
        ]
        out = self.sort(rollup, ["A", "B"])[1]
        self.assertEqual((out["real"], out["superseded"]), (["B"], ["A"]))

    def test_a_repeated_name_is_reported_once(self):
        rollup = [run("A", "W", "SUCCESS", "2026-10-16T01:00:00Z")]
        self.assertEqual(self.sort(rollup, ["A", "A", "A"])[1]["superseded"], ["A"])

    def test_a_gh_failure_prints_an_error_and_exits_1(self):
        code, out = self.sort([], ["A"], fails=True)
        self.assertEqual(code, 1)
        self.assertIn("gh pr view failed: gh: HTTP 502", out["error"])


if __name__ == "__main__":
    unittest.main()
