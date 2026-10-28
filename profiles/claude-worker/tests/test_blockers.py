from __future__ import annotations

import sys
import unittest

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import blockers  # noqa: E402


def issue(identifier: str, *, state: str = "started", delegated: bool = True) -> dict:
    number = identifier.split("-")[1]
    return {
        "id": f"uuid-{number}", "identifier": identifier, "gitBranchName": f"abc-{number}-work",
        "stateType": state, "delegate": "app-user" if delegated else None, "delegatedToSelf": delegated,
    }


def card(task_id: str, identifier: str, *, status: str = "running", branch: str | None = None) -> dict:
    number = identifier.split("-")[1]
    return {"id": task_id, "title": f"{identifier}: work", "body": f"linear_issue_id: uuid-{number}\n",
            "branch_name": f"abc-{number}-work" if branch is None else branch, "status": status}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class WaitingOnTest(unittest.TestCase):
    def wait(self, issues, boards, ignored=()):
        """`boards` is the board the check reads each time; the last one repeats."""
        clock = Clock()
        reads = []

        def read_cards():
            reads.append(clock.now)
            return boards[min(len(reads), len(boards)) - 1]

        found = blockers.waiting_on(lambda: issues, read_cards, ignored, sleep=clock.sleep, clock=clock)
        return found, clock.sleeps, len(reads)

    def test_a_blocker_with_a_live_card_is_waited_for_at_once(self):
        found, sleeps, _ = self.wait([issue("ABC-1")], [[card("t_a", "ABC-1", status="todo")]])

        self.assertEqual((found, sleeps), (["t_a"], []))

    def test_a_finished_issue_a_done_card_and_a_card_on_no_blocker_are_not_waited_for(self):
        issues = [issue("ABC-1", state="completed"), issue("ABC-2", state="canceled"), issue("ABC-3"), issue("ABC-4"),
                  issue("ABC-5", state="duplicate")]
        board = [card("t_1", "ABC-1"), card("t_2", "ABC-2", status="review"), card("t_3", "ABC-3", status="done"),
                 card("t_4", "ABC-4", status="blocked"), card("t_5", "ABC-5"), card("t_9", "ABC-9")]

        found, sleeps, _ = self.wait(issues, [board])

        self.assertEqual((found, sleeps), (["t_4"], []))

    def test_an_issue_with_no_unfinished_blocker_never_reads_the_board(self):
        for issues in ([], [issue("ABC-1", state="completed")]):
            with self.subTest(issues=issues):
                self.assertEqual(self.wait(issues, [[card("t_1", "ABC-1")]]), ([], [], 0))

    def test_a_blocker_delegated_to_no_one_with_no_card_is_not_waited_for(self):
        found, sleeps, reads = self.wait([issue("ABC-1", delegated=False)], [[]])

        self.assertEqual((found, sleeps, reads), ([], [], 1))

    def test_a_card_for_a_blocker_delegated_to_someone_else_is_still_waited_for(self):
        found, _, _ = self.wait([issue("ABC-1", delegated=False)], [[card("t_a", "ABC-1", status="ready")]])

        self.assertEqual(found, ["t_a"])

    def test_a_delegated_blockers_card_that_appears_during_the_wait_is_waited_for(self):
        found, sleeps, reads = self.wait([issue("ABC-1")], [[], [], [card("t_a", "ABC-1", status="ready")]])

        self.assertEqual((found, sleeps, reads), (["t_a"], [45, 45], 3))

    def test_a_delegated_blocker_whose_card_never_appears_ends_the_wait_at_the_bound(self):
        found, sleeps, reads = self.wait([issue("ABC-1"), issue("ABC-2", delegated=False)], [[]])

        self.assertEqual(found, [])
        self.assertEqual(sum(sleeps), 300)
        self.assertEqual((sleeps, reads), ([45, 45, 45, 45, 45, 45, 30], 8))

    def test_the_bound_stays_well_inside_the_claim_lease(self):
        self.assertLessEqual(blockers.WAIT_SECONDS, 5 * 60)
        self.assertTrue(30 <= blockers.POLL_SECONDS <= 60)

    def test_a_card_that_already_closed_a_cycle_is_ignored_and_not_awaited(self):
        issues = [issue("ABC-1"), issue("ABC-2")]
        board = [card("t_1", "ABC-1", status="todo"), card("t_2", "ABC-2", status="todo")]

        self.assertEqual(self.wait(issues, [board], ignored=("t_1",))[0], ["t_2"])
        self.assertEqual(self.wait(issues, [board], ignored=("t_1", "t_2"))[:2], ([], []))

    def test_a_card_is_found_by_the_triage_lookup_keys_in_order(self):
        blocker = issue("ABC-1")
        by_title = {"id": "t_title", "title": "ABC-1: renamed", "body": "", "branch_name": "", "status": "todo"}
        by_body = {"id": "t_body", "title": "x", "body": "a\nlinear_issue_id: uuid-1\nb", "branch_name": "", "status": "todo"}
        by_takeover = card("t_takeover", "ABC-1", status="todo", branch="abc-1-work-takeover")
        by_branch = card("t_branch", "ABC-1", status="todo")
        boards = [
            ([by_title, by_body, by_takeover, by_branch], "t_branch"),
            ([by_title, by_body, by_takeover], "t_takeover"),
            ([by_title, by_body], "t_body"),
            ([by_title], "t_title"),
        ]
        for board, expected in boards:
            with self.subTest(expected=expected):
                self.assertEqual(self.wait([blocker], [board])[0], [expected])


if __name__ == "__main__":
    unittest.main()
