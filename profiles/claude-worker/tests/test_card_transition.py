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

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]
assert argv[0] == "kanban", argv
argv = argv[1:]
board = None
if argv[0] == "--board":
    board, argv = argv[1], argv[2:]
state["boards"].append(board)
state["calls"].append(argv)
verb, task = argv[0], state["task"]
def save():
    json.dump(state, open(path, "w"))
failures = state["fail"].get(verb, 0)
if failures:
    state["fail"][verb] = failures - 1
    save()
    sys.exit(f"hermes: cannot {verb} {task['id']}")
status = task["status"]
if verb == "show":
    print(json.dumps({"task": task, "events": state["events"], "comments": []}))
elif verb == "assign":
    task["assignee"] = None if argv[2] == "none" else argv[2]
elif verb == "specify":
    task.update(status=state["specify_to"], title="Rewritten title", body="Rewritten body")
elif verb == "edit":
    task.update(title=argv[argv.index("--title") + 1], body=argv[argv.index("--body") + 1])
elif verb == "promote":
    task["status"] = "ready"
elif verb == "complete":
    if state["refuse_completion"]:
        state["events"].append({"kind": "pr_acceptance", "payload": state["receipt"]})
        save()
        sys.exit(f"cannot complete {task['id']} (unknown id or terminal state)")
    task["status"] = "done"
elif verb == "block":
    if status == "blocked":
        if "--kind" not in argv or task["block_kind"]:
            save()
            sys.exit(f"cannot block {task['id']}")
        task["block_kind"] = argv[argv.index("--kind") + 1]
    else:
        task["status"] = "blocked"
elif verb == "unblock":
    task["status"] = "ready"
elif verb == "request-review":
    task["status"] = "review"
elif verb == "reopen-review":
    task["status"] = "ready"
elif verb == "archive":
    task["status"] = "archived"
elif verb == "comment":
    state["comments"].append(argv[2])
save()
"""

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_STATE"]
state = json.load(open(path))
state["gh_calls"].append(sys.argv[1:])
json.dump(state, open(path, "w"))
if state["gh_fail"]:
    state["gh_fail"] -= 1
    json.dump(state, open(path, "w"))
    sys.exit("gh: HTTP 502")
if sys.argv[1:] == ["pr", "view", state["pr"]["url"], "--json", "headRefOid,statusCheckRollup"]:
    print(json.dumps({"headRefOid": state["pr"]["head"], "statusCheckRollup": state["pr"]["rollup"]}))
elif sys.argv[1:3] == ["pr", "view"] and "-R" in sys.argv:
    if not state["branch_has_pr"]:
        sys.exit('no pull requests found for branch "' + sys.argv[3] + '"')
    print(json.dumps({"state": "OPEN"}))
else:
    sys.exit("unexpected " + repr(sys.argv))
"""

URL = "https://github.com/example-org/app/pull/12"
REF = "example-org/app#12"
HEAD = "abc123"
TITLE = "PAT-1: Don't drop the user's work"
BODY = "linear_issue_id: 1\nbranch: pat-1-work\nIt's the user's body."
SUMMARY = ["--summary", f"Pull request {REF} merged.", "--metadata", json.dumps({"published_pr": URL})]
REAL_REASON = (
    f"Pull request {REF} merged, but required checks failed: Validate. Is the work done? If it is, mark the "
    "Linear issue Done, and the card is archived."
)
SUPERSEDED_REASON = (
    f"Pull request {REF} merged and its required checks passed on their newest runs. Hermes counted failed runs "
    "that newer passing runs replaced: Validate, so it cannot finish the card. Mark the Linear issue Done, and "
    "the card is archived."
)
NO_CHECKS_REASON = (
    f"Pull request {REF} merged, but its base branch has no required checks, so Hermes cannot finish the card. "
    "Is the work done? If it is, mark the Linear issue Done, and the card is archived."
)
REAL_MARKER = f"Merge refused by Hermes: required checks failed: Validate. {REF}"
SUPERSEDED_MARKER = f"Merge refused by Hermes: Hermes counted failed runs that newer passing runs replaced: Validate. {REF}"
NO_CHECKS_MARKER = f"Merge refused by Hermes: the base branch has no required checks. {REF}"
UNREAD = " The Linear issue could not be read, so the card was not archived."
COMPLETION_ERROR = "hermes kanban complete failed: cannot complete t_1 (unknown id or terminal state)"


def check_run(conclusion: str, started: str) -> dict:
    return {"__typename": "CheckRun", "name": "Validate", "workflowName": "Pull request", "status": "COMPLETED",
            "conclusion": conclusion, "startedAt": started}


FAILED_NEWEST = [check_run("SUCCESS", "2026-10-16T04:00:00Z"), check_run("FAILURE", "2026-10-16T05:00:00Z")]
PASSED_NEWEST = [check_run("FAILURE", "2026-10-16T04:00:00Z"), check_run("SUCCESS", "2026-10-16T05:00:00Z")]
FAILED_RECEIPT = {"ok": False, "classification": "failure", "pr_url": URL, "head_sha": HEAD,
                  "checks": [{"name": "Validate", "classification": "failure"},
                             {"name": "Lint", "classification": "success"}]}
NO_CHECKS_RECEIPT = {"ok": False, "classification": "missing", "pr_url": URL, "head_sha": HEAD, "checks": []}


class CardTransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "bin").mkdir()
        for name, script in (("hermes", FAKE_HERMES), ("gh", FAKE_GH)):
            fake = self.dir / "bin" / name
            fake.write_text(with_interpreter(script))
            os.chmod(fake, 0o755)
        self.state_path = self.dir / "state.json"
        self.state = {
            "task": {"id": "t_1", "status": "triage", "title": TITLE, "body": BODY, "assignee": "claude-worker",
                     "block_kind": None, "workspace_kind": "scratch", "workspace_path": None},
            "events": [], "comments": [], "calls": [], "boards": [], "gh_calls": [], "fail": {}, "gh_fail": 0,
            "specify_to": "todo", "refuse_completion": False, "receipt": FAILED_RECEIPT,
            "pr": {"url": URL, "head": HEAD, "rollup": FAILED_NEWEST}, "branch_has_pr": True,
        }

    def given(self, status: str, **changes) -> None:
        self.state["task"]["status"] = status
        self.state.update(changes)

    def transition(self, *argv: str) -> tuple[int, dict]:
        self.state_path.write_text(json.dumps(self.state))
        env = {**os.environ, "HERMES_BIN": str(self.dir / "bin" / "hermes"), "FAKE_STATE": str(self.state_path),
               "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}"}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "card_transition.py"), *argv],
                              capture_output=True, text=True, env=env)
        self.state = json.loads(self.state_path.read_text())
        self.assertEqual(proc.stderr, "")
        return proc.returncode, json.loads(proc.stdout)

    def calls(self) -> list:
        return self.state["calls"]

    def state_changes(self) -> list:
        return [call for call in self.calls() if call[0] != "show"]


class ResumeTest(CardTransitionTest):
    def test_the_card_leaves_triage_unassigned_and_is_assigned_after_its_text_is_restored(self):
        code, out = self.transition("resume", "t_1")
        self.assertEqual((code, out), (0, {"action": "resumed", "post": None, "assigned": True}))
        self.assertEqual(self.calls(), [
            ["show", "t_1", "--json"],
            ["assign", "t_1", "none"],
            ["specify", "t_1"],
            ["edit", "t_1", "--title", TITLE, "--body", BODY],
            ["assign", "t_1", "claude-worker"],
        ])
        self.assertEqual((self.state["task"]["title"], self.state["task"]["body"]), (TITLE, BODY))

    def test_a_failed_specify_reassigns_the_card_and_restores_nothing(self):
        self.state["fail"]["specify"] = 1
        code, out = self.transition("resume", "t_1")
        self.assertEqual(code, 1)
        self.assertEqual(out, {"error": "hermes kanban specify failed: hermes: cannot specify t_1; the card stays in triage"})
        self.assertEqual(self.state_changes(), [["assign", "t_1", "none"], ["specify", "t_1"], ["assign", "t_1", "claude-worker"]])

    def test_a_failed_edit_is_retried_before_the_card_is_assigned(self):
        self.state["fail"]["edit"] = 1
        code, out = self.transition("resume", "t_1")
        self.assertEqual(code, 0)
        edit = ["edit", "t_1", "--title", TITLE, "--body", BODY]
        self.assertEqual(self.state_changes(), [
            ["assign", "t_1", "none"], ["specify", "t_1"], edit, edit, ["assign", "t_1", "claude-worker"],
        ])

    def test_an_edit_that_keeps_failing_leaves_the_card_unassigned(self):
        self.state["fail"]["edit"] = 2
        code, out = self.transition("resume", "t_1")
        self.assertEqual(code, 1)
        self.assertIn("the card is unassigned", out["error"])
        edit = ["edit", "t_1", "--title", TITLE, "--body", BODY]
        self.assertEqual(self.state_changes(), [["assign", "t_1", "none"], ["specify", "t_1"], edit, edit])
        self.assertIsNone(self.state["task"]["assignee"])

    def test_a_card_that_already_left_triage_is_not_specified_again(self):
        self.given("ready")
        code, out = self.transition("resume", "t_1")
        self.assertEqual((code, out), (1, {"error": "card t_1 is ready, not triage"}))
        self.assertEqual(self.state_changes(), [])

    def test_a_board_reaches_every_command(self):
        self.transition("--board", "triage", "resume", "t_1")
        self.assertEqual(len(self.calls()), 5)
        self.assertEqual(set(self.state["boards"]), {"triage"})


class TriageReplyTest(CardTransitionTest):
    def test_a_parked_triage_card_resumes_without_the_assignment(self):
        code, out = self.transition("--board", "triage", "triage-reply", "t_1")
        self.assertEqual((code, out), (0, {"action": "resumed", "post": None, "assigned": False}))
        self.assertEqual(self.calls()[-1], ["edit", "t_1", "--title", TITLE, "--body", BODY])
        self.assertIsNone(self.state["task"]["assignee"])

    def test_a_blocked_triage_card_is_unassigned_then_unblocked(self):
        self.given("blocked")
        code, out = self.transition("--board", "triage", "triage-reply", "t_1")
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": False}))
        self.assertEqual(self.state_changes(), [["assign", "t_1", "none"], ["unblock", "t_1"]])
        self.assertEqual(self.state["gh_calls"], [])

    def test_any_other_status_is_left_alone(self):
        self.given("running")
        code, out = self.transition("--board", "triage", "triage-reply", "t_1")
        self.assertEqual((code, out), (0, {"action": "none", "post": None, "status": "running"}))
        self.assertEqual(self.state_changes(), [])


class UnblockTest(CardTransitionTest):
    ARGS = ("unblock", "t_1", "--repo", "example-org/app", "--branch", "pat-1-work", "--reason", "feedback on GitHub")

    def test_a_card_without_a_pull_request_is_only_unblocked(self):
        self.given("blocked", branch_has_pr=False)
        code, out = self.transition(*self.ARGS)
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": False}))
        self.assertEqual(self.state["gh_calls"], [["pr", "view", "pat-1-work", "-R", "example-org/app", "--json", "state"]])
        self.assertEqual(self.state_changes(), [["unblock", "t_1"]])

    def test_a_card_with_a_pull_request_is_unblocked_then_its_review_reopened(self):
        self.given("blocked")
        code, out = self.transition(*self.ARGS)
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": True}))
        self.assertEqual(self.state_changes(), [
            ["unblock", "t_1"], ["request-review", "t_1"], ["reopen-review", "t_1", "--reason", "feedback on GitHub"],
        ])
        self.assertEqual(self.state["task"]["status"], "ready")

    def test_a_failed_pull_request_lookup_changes_nothing(self):
        self.given("blocked", gh_fail=1)
        code, out = self.transition(*self.ARGS)
        self.assertEqual((code, out), (1, {"error": "gh pr view failed: gh: HTTP 502"}))
        self.assertEqual(self.state_changes(), [])


class ReopenAndStopTest(CardTransitionTest):
    def test_reopen_hands_a_review_card_back_with_the_reason(self):
        self.given("review")
        code, out = self.transition("reopen", "t_1", "--reason", "changes requested on GitHub; see the card comments")
        self.assertEqual((code, out), (0, {"action": "reopened", "post": None}))
        self.assertEqual(self.calls(), [["reopen-review", "t_1", "--reason", "changes requested on GitHub; see the card comments"]])

    def test_stop_reopens_a_review_card_then_blocks_it(self):
        self.given("review")
        code, out = self.transition("stop", "t_1")
        self.assertEqual((code, out), (0, {"action": "stopped", "post": None, "status": "blocked"}))
        self.assertEqual(self.state_changes(), [["reopen-review", "t_1", "--reason", "stopped"], ["block", "t_1", "stopped"]])

    def test_stop_blocks_a_ready_running_or_todo_card(self):
        for status in ("ready", "running", "todo"):
            with self.subTest(status):
                self.setUp()
                self.given(status)
                code, out = self.transition("stop", "t_1")
                self.assertEqual((code, out), (0, {"action": "stopped", "post": None, "status": "blocked"}))
                self.assertEqual(self.state_changes(), [["block", "t_1", "stopped"]])

    def test_stop_leaves_a_blocked_card_as_it_is(self):
        self.given("blocked")
        code, out = self.transition("stop", "t_1")
        self.assertEqual((code, out), (0, {"action": "stopped", "post": None, "status": "blocked"}))
        self.assertEqual(self.state_changes(), [])

    def test_stop_runs_on_the_named_board(self):
        self.given("ready")
        self.transition("--board", "triage", "stop", "t_1")
        self.assertEqual(self.state_changes(), [["block", "t_1", "stopped"]])
        self.assertEqual(set(self.state["boards"]), {"triage"})


class MergedTest(CardTransitionTest):
    def merged(self, linear_state: str = "started") -> tuple[int, dict]:
        return self.transition("merged", "t_1", "--pr", URL, "--linear-state", linear_state)

    def test_a_running_card_is_left_to_its_worker(self):
        for status in ("running", "done", "archived"):
            with self.subTest(status):
                self.setUp()
                self.given(status)
                self.assertEqual(self.merged(), (0, {"action": "none", "post": None, "status": status}))
                self.assertEqual(self.state_changes(), [])

    def test_a_review_blocked_or_ready_card_is_only_completed(self):
        for status in ("review", "blocked", "ready"):
            with self.subTest(status):
                self.setUp()
                self.given(status)
                self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
                self.assertEqual(self.state_changes(), [["complete", "t_1", *SUMMARY]])

    def test_a_todo_card_is_promoted_before_it_is_completed(self):
        self.given("todo")
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
        self.assertEqual(self.state_changes(), [
            ["promote", "t_1", "pull", "request", "merged"], ["complete", "t_1", *SUMMARY],
        ])

    def test_a_refused_promote_is_commented_and_stops(self):
        self.given("todo", fail={"promote": 1})
        code, out = self.merged()
        reason = f"Pull request {REF} merged, but Hermes refused to promote the card: hermes kanban promote failed: hermes: cannot promote t_1"
        self.assertEqual((code, out), (0, {"action": "commented", "post": None, "reason": reason}))
        self.assertEqual(self.state_changes(), [["promote", "t_1", "pull", "request", "merged"], ["comment", "t_1", reason]])

    def test_a_triage_card_is_unparked_restored_promoted_and_completed_without_an_assignment(self):
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
        self.assertEqual(self.state_changes(), [
            ["assign", "t_1", "none"],
            ["specify", "t_1"],
            ["edit", "t_1", "--title", TITLE, "--body", BODY],
            ["promote", "t_1", "pull", "request", "merged"],
            ["complete", "t_1", *SUMMARY],
        ])

    def test_a_triage_card_that_specify_left_ready_is_completed_without_a_promote(self):
        self.state["specify_to"] = "ready"
        self.merged()
        self.assertEqual([call[0] for call in self.state_changes()], ["assign", "specify", "edit", "complete"])

    def test_a_triage_card_whose_specify_fails_is_reassigned_and_commented(self):
        self.state["fail"]["specify"] = 1
        code, out = self.merged()
        reason = (f"Pull request {REF} merged, but the card could not leave triage: hermes kanban specify failed: "
                  "hermes: cannot specify t_1; the card stays in triage")
        self.assertEqual((code, out), (0, {"action": "commented", "post": None, "reason": reason}))
        self.assertEqual(self.state_changes(), [
            ["assign", "t_1", "none"], ["specify", "t_1"], ["assign", "t_1", "claude-worker"], ["comment", "t_1", reason],
        ])

    def test_after_specify_succeeds_a_triage_card_is_never_assigned(self):
        paths = {
            "completed": {},
            "promote refused": {"fail": {"promote": 1}},
            "completion refused": {"refuse_completion": True, "receipt": {**FAILED_RECEIPT, "head_sha": "other"}},
            "refused merge blocked": {"refuse_completion": True},
            "edit keeps failing": {"fail": {"edit": 2}},
        }
        for name, changes in paths.items():
            with self.subTest(name):
                self.setUp()
                self.given("triage", **changes)
                self.merged()
                after = self.state_changes()[self.state_changes().index(["specify", "t_1"]) + 1 :]
                self.assertEqual([call for call in after if call[0] == "assign" and call[2] != "none"], [])


class RefusedMergeTest(CardTransitionTest):
    """Each refused completion of a merged pull request, with the card ready unless the case says otherwise."""

    def refuse(self, status="ready", linear_state="started", receipt=FAILED_RECEIPT, rollup=FAILED_NEWEST, **changes):
        self.given(status, refuse_completion=True, receipt=receipt, **changes)
        self.state["pr"]["rollup"] = rollup
        return self.transition("merged", "t_1", "--pr", URL, "--linear-state", linear_state)

    def after_completion(self) -> list:
        changes = self.state_changes()
        return changes[changes.index(["complete", "t_1", *SUMMARY]) + 1 :]

    def test_each_case_comments_its_marker_and_holds_or_archives_the_card(self):
        cases = [
            ("real failure, ready", {}, {"action": "held", "post": None, "reason": REAL_REASON},
             [["comment", "t_1", REAL_MARKER], ["assign", "t_1", "none"], ["block", "t_1", REAL_REASON, "--kind", "needs_input"]]),
            ("superseded only, ready", {"rollup": PASSED_NEWEST}, {"action": "held", "post": None, "reason": SUPERSEDED_REASON},
             [["comment", "t_1", SUPERSEDED_MARKER], ["assign", "t_1", "none"],
              ["block", "t_1", SUPERSEDED_REASON, "--kind", "needs_input"]]),
            ("no required checks, ready", {"receipt": NO_CHECKS_RECEIPT}, {"action": "held", "post": None, "reason": NO_CHECKS_REASON},
             [["comment", "t_1", NO_CHECKS_MARKER], ["assign", "t_1", "none"],
              ["block", "t_1", NO_CHECKS_REASON, "--kind", "needs_input"]]),
            ("Linear completed", {"linear_state": "completed"}, {"action": "archived", "post": None},
             [["comment", "t_1", REAL_MARKER], ["archive", "t_1"]]),
            ("no required checks, Linear completed", {"receipt": NO_CHECKS_RECEIPT, "linear_state": "completed"},
             {"action": "archived", "post": None}, [["comment", "t_1", NO_CHECKS_MARKER], ["archive", "t_1"]]),
            ("Linear unreadable", {"linear_state": "unreadable"}, {"action": "held", "post": None, "reason": REAL_REASON},
             [["comment", "t_1", REAL_MARKER + UNREAD], ["assign", "t_1", "none"],
              ["block", "t_1", REAL_REASON, "--kind", "needs_input"]]),
            ("review card", {"status": "review"}, {"action": "held", "post": "elicitation", "reason": REAL_REASON},
             [["comment", "t_1", REAL_MARKER], ["assign", "t_1", "none"]]),
            ("blocked card with no block kind", {"status": "blocked"},
             {"action": "held", "post": "elicitation", "reason": REAL_REASON},
             [["comment", "t_1", REAL_MARKER], ["assign", "t_1", "none"], ["block", "t_1", REAL_REASON, "--kind", "needs_input"]]),
            ("ready card whose block fails", {"fail": {"block": 1}},
             {"action": "held", "post": "elicitation", "reason": REAL_REASON},
             [["comment", "t_1", REAL_MARKER], ["assign", "t_1", "none"], ["block", "t_1", REAL_REASON, "--kind", "needs_input"]]),
            ("event for an older head", {"receipt": {**FAILED_RECEIPT, "head_sha": "older"}},
             {"action": "commented", "post": None,
              "reason": f"Pull request {REF} merged, but Hermes refused to complete the card: {COMPLETION_ERROR}"},
             [["comment", "t_1", f"Pull request {REF} merged, but Hermes refused to complete the card: {COMPLETION_ERROR}"]]),
            ("event for another pull request", {"receipt": {**FAILED_RECEIPT, "pr_url": URL + "3"}},
             {"action": "commented", "post": None,
              "reason": f"Pull request {REF} merged, but Hermes refused to complete the card: {COMPLETION_ERROR}"},
             [["comment", "t_1", f"Pull request {REF} merged, but Hermes refused to complete the card: {COMPLETION_ERROR}"]]),
            ("only pending checks", {"receipt": {**FAILED_RECEIPT, "checks": [{"name": "Validate", "classification": "pending"}]}},
             {"action": "commented", "post": None,
              "reason": f"Pull request {REF} merged, but Hermes refused to complete the card: {COMPLETION_ERROR}"},
             [["comment", "t_1", f"Pull request {REF} merged, but Hermes refused to complete the card: {COMPLETION_ERROR}"]]),
            ("checks unreadable twice", {"gh_fail": 2},
             {"action": "commented", "post": None,
              "reason": f"Pull request {REF} merged, but its checks could not be read: gh pr view failed: gh: HTTP 502"},
             [["comment", "t_1", f"Pull request {REF} merged, but its checks could not be read: gh pr view failed: gh: HTTP 502"]]),
            ("checks read on the retry", {"gh_fail": 1}, {"action": "held", "post": None, "reason": REAL_REASON},
             [["comment", "t_1", REAL_MARKER], ["assign", "t_1", "none"], ["block", "t_1", REAL_REASON, "--kind", "needs_input"]]),
        ]
        for name, changes, output, after in cases:
            with self.subTest(name):
                self.setUp()
                self.assertEqual(self.refuse(**changes), (0, output))
                self.assertEqual(self.after_completion(), after)

    def test_a_blocked_card_that_already_has_a_kind_stays_blocked(self):
        self.state["task"]["block_kind"] = "needs_input"
        code, out = self.refuse(status="blocked")
        self.assertEqual((code, out), (0, {"action": "held", "post": "elicitation", "reason": REAL_REASON}))
        self.assertEqual(self.state["task"]["status"], "blocked")

    def test_no_comment_names_a_pull_request_by_url(self):
        runs = [
            dict(),
            dict(linear_state="unreadable"),
            dict(receipt={**FAILED_RECEIPT, "head_sha": "older"}),
            dict(gh_fail=2),
            dict(status="todo", fail={"promote": 1}),
            dict(status="triage", fail={"specify": 1}),
        ]
        for changes in runs:
            with self.subTest(changes):
                self.setUp()
                self.refuse(**changes)
                self.assertTrue(self.state["comments"])
                for comment in self.state["comments"]:
                    self.assertIn(REF, comment)
                    self.assertNotIn("https://github.com/", comment)

    def test_a_url_that_is_not_a_pull_request_is_refused_before_any_change(self):
        self.given("ready")
        code, out = self.transition("merged", "t_1", "--pr", "https://github.com/example-org/app/issues/12", "--linear-state", "started")
        self.assertEqual((code, out), (1, {"error": "https://github.com/example-org/app/issues/12 is not a GitHub pull request URL"}))
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
