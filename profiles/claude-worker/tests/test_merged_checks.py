from __future__ import annotations

import sys
import unittest

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
from merged_checks import sort_checks  # noqa: E402


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
    def sort(self, rollup, names) -> dict:
        return sort_checks(rollup, names)

    def test_a_failed_run_replaced_by_a_newer_pass_is_superseded(self):
        rollup = [
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:09:58Z"),
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"),
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:10:16Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"]), {"real": [], "superseded": ["Validate"]})

    def test_a_newest_run_that_failed_is_real(self):
        rollup = [
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:09:58Z"),
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:25:56Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"])["real"], ["Validate"])

    def test_a_tie_keeps_the_run_that_did_not_pass(self):
        rollup = [
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"),
            run("Validate", "Pull request", "FAILURE", "2026-10-16T04:25:56Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"])["real"], ["Validate"])

    def test_a_run_that_has_not_started_is_the_newest(self):
        pending = {**run("Validate", "Pull request", "", "0001-01-01T00:00:00Z"), "status": "QUEUED", "conclusion": None}
        rollup = [run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"), pending]
        self.assertEqual(self.sort(rollup, ["Validate"])["real"], ["Validate"])

    def test_one_workflow_that_still_fails_makes_a_shared_name_real(self):
        rollup = [
            run("Validate", "Pull request", "SUCCESS", "2026-10-16T04:25:56Z"),
            run("Validate", "Release", "FAILURE", "2026-10-16T01:00:00Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Validate"])["real"], ["Validate"])

    def test_a_status_context_is_judged_by_its_state(self):
        failing = {"__typename": "StatusContext", "context": "Vercel", "state": "FAILURE", "startedAt": "2026-10-16T01:00:00Z"}
        passing = {**failing, "state": "SUCCESS", "startedAt": "2026-10-16T02:00:00Z"}
        self.assertEqual(self.sort([failing, passing], ["Vercel"])["superseded"], ["Vercel"])

    def test_a_failed_run_replaced_by_a_newer_skip_is_superseded(self):
        rollup = [
            run("Deploy", "Pull request", "FAILURE", "2026-10-16T04:09:58Z"),
            run("Deploy", "Pull request", "SKIPPED", "2026-10-16T04:25:56Z"),
        ]
        self.assertEqual(self.sort(rollup, ["Deploy"])["superseded"], ["Deploy"])

    def test_a_check_the_rollup_does_not_show_is_real(self):
        self.assertEqual(self.sort([], ["Gone"]), {"real": ["Gone"], "superseded": []})

    def test_mixed_names_are_sorted_each_on_their_own_newest_run(self):
        rollup = [
            run("A", "W", "FAILURE", "2026-10-16T01:00:00Z"),
            run("A", "W", "SUCCESS", "2026-10-16T02:00:00Z"),
            run("B", "W", "SUCCESS", "2026-10-16T01:00:00Z"),
            run("B", "W", "FAILURE", "2026-10-16T02:00:00Z"),
        ]
        out = self.sort(rollup, ["A", "B"])
        self.assertEqual((out["real"], out["superseded"]), (["B"], ["A"]))

    def test_a_repeated_name_is_reported_once(self):
        rollup = [run("A", "W", "SUCCESS", "2026-10-16T01:00:00Z")]
        self.assertEqual(self.sort(rollup, ["A", "A", "A"])["superseded"], ["A"])


if __name__ == "__main__":
    unittest.main()
