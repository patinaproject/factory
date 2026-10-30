from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, SCRIPTS, FakeBroker, granted, with_interpreter

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]
if argv[:2] == ["config", "get"]:
    print(json.dumps({"claude_session": {"auth_token_env": "PROXY_VARIABLE"}}))
    sys.exit()
assert argv[0] == "kanban", argv
argv = argv[1:]
board = None
if argv[0] == "--board":
    board, argv = argv[1], argv[2:]
state["boards"].append(board)
state["calls"].append(argv)
verb, task = argv[0], state["task"]
if verb == "boards":
    print(json.dumps([{"slug": "default", "db_path": "/nowhere"}, {"slug": "triage", "db_path": state["db_path"]}]))
    sys.exit()
def save():
    json.dump(state, open(path, "w"))
failures = state["fail"].get(verb, 0)
if failures:
    state["fail"][verb] = failures - 1
    save()
    sys.exit(f"hermes: cannot {verb} {task['id']}")
if state.get("released_before") == verb:
    task["status"] = "ready"
status = task["status"]
def comments():
    at = state["comment_at"]
    return [{"author": "default", "body": body, "created_at": at[n] if n < len(at) else 1000 + n}
            for n, body in enumerate(state["comments"])]
if verb == "show":
    print(json.dumps({"task": task, "events": state["events"], "comments": comments()}))
elif verb == "assign":
    task["assignee"] = None if argv[2] == "none" else argv[2]
elif verb == "specify":
    state["specify_token"] = os.environ.get("PROXY_VARIABLE")
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
    if status == "scheduled":
        save()
        sys.exit(f"cannot block {task['id']}")
    if status == "blocked":
        if "--kind" not in argv or task["block_kind"]:
            save()
            sys.exit(f"cannot block {task['id']}")
        task["block_kind"] = argv[argv.index("--kind") + 1]
    else:
        task["status"] = "blocked"
elif verb == "schedule":
    if status not in ("todo", "ready", "running", "blocked"):
        save()
        sys.exit(f"cannot schedule {task['id']}")
    state["comments"].append("SCHEDULED: " + argv[2])
    state["events"].append({"kind": "scheduled", "payload": {"reason": argv[2]}, "created_at": 2000, "run_id": None})
    task["status"] = "scheduled"
elif verb == "unblock":
    if status not in ("blocked", "scheduled"):
        save()
        sys.exit(f"cannot unblock {task['id']} (not blocked/scheduled?)")
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
            "events": [], "comments": [], "comment_at": [], "calls": [], "boards": [], "gh_calls": [], "fail": {},
            "gh_fail": 0, "db_path": str(self.dir / "triage.db"),
            "specify_to": "todo", "refuse_completion": False, "receipt": FAILED_RECEIPT,
            "pr": {"url": URL, "head": HEAD, "rollup": FAILED_NEWEST}, "branch_has_pr": True,
        }
        self.home = self.dir / "hermes"
        (self.home / "plugins").mkdir(parents=True)
        (self.home / "plugins" / "linear-agent-session").symlink_to(REPO / "plugins" / "linear-agent-session")
        (self.home / "plugins" / "blocker-order").symlink_to(REPO / "plugins" / "blocker-order")
        self.start_broker(proxy_token=[granted("proxy-value", None)])

    def start_broker(self, **answers: list) -> FakeBroker:
        socket = self.home / "factory-uploads.sock"
        socket.unlink(missing_ok=True)
        self.broker = FakeBroker(socket, **answers)
        self.addCleanup(self.broker.close)
        return self.broker

    def given(self, status: str, **changes) -> None:
        self.state["task"]["status"] = status
        self.state.update(changes)

    def transition(self, *argv: str, reply: str = "") -> tuple[int, dict]:
        self.state_path.write_text(json.dumps(self.state))
        env = {**os.environ, "HERMES_BIN": str(self.dir / "bin" / "hermes"), "FAKE_STATE": str(self.state_path),
               "HERMES_HOME": str(self.home),
               "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}"}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "card_transition.py"), *argv],
                              capture_output=True, text=True, env=env, input=reply)
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

    def test_specify_alone_gets_the_proxy_token_from_the_broker(self):
        code, _ = self.transition("resume", "t_1")
        self.assertEqual((code, self.state["specify_token"], self.broker.requests), (0, "proxy-value", ["proxy_token"]))

    def test_a_broker_that_refuses_the_token_changes_nothing(self):
        self.broker.close()
        self.start_broker(proxy_token=[{"ok": False, "state": "refused", "message": "The gateway holds no proxy token."}])
        code, out = self.transition("resume", "t_1")
        self.assertEqual(code, 1)
        self.assertIn("specify needs the proxy token", out["error"])
        self.assertEqual(self.state_changes(), [])
        self.assertEqual(self.state["task"]["assignee"], "claude-worker")

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


def park(seen: int | None, created_at: int = 500) -> dict:
    reason = "Needs input: which screen?" + (f"\n\ncomments_seen: {seen}" if seen is not None else "")
    return {"kind": "scheduled", "payload": {"reason": reason}, "created_at": created_at, "run_id": 1}


REPLY = "It is the settings screen, see https://github.com/example-org/app/pull/12."
REPLY_COMMENT = f"Linear reply: It is the settings screen, see {REF}."


class TriageReplyTest(CardTransitionTest):
    def reply(self, text: str = REPLY) -> tuple[int, dict]:
        return self.transition("--board", "triage", "triage-reply", "t_1", reply=text)

    def test_the_reply_on_standard_input_becomes_one_prefixed_comment_in_every_status(self):
        for status in ("scheduled", "blocked", "triage", "running", "ready", "review"):
            with self.subTest(status):
                self.given(status, comments=["Asked a question."], events=[park(1)])
                self.reply()
                self.assertEqual(self.state["comments"], ["Asked a question.", REPLY_COMMENT])
                self.assertEqual(self.state["calls"][1], ["comment", "t_1", REPLY_COMMENT])

    def test_a_parked_triage_card_resumes_without_the_assignment(self):
        code, out = self.reply()
        self.assertEqual((code, out), (0, {"action": "resumed", "post": None, "assigned": False}))
        self.assertEqual(self.calls()[-1], ["edit", "t_1", "--title", TITLE, "--body", BODY])
        self.assertIsNone(self.state["task"]["assignee"])

    def test_a_blocked_triage_card_is_unassigned_then_unblocked(self):
        self.given("blocked")
        code, out = self.reply()
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": False}))
        self.assertEqual(self.state_changes(), [
            ["comment", "t_1", REPLY_COMMENT], ["assign", "t_1", "none"], ["unblock", "t_1"],
        ])
        self.assertEqual(self.state["gh_calls"], [])

    def test_a_scheduled_card_is_unassigned_before_it_is_unblocked_and_left_for_dispatch_triage(self):
        self.given("scheduled", comments=["Asked a question."], events=[park(1)])
        code, out = self.reply()
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": False}))
        self.assertEqual(self.state_changes(), [
            ["comment", "t_1", REPLY_COMMENT], ["assign", "t_1", "none"], ["unblock", "t_1"],
        ])
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("ready", None))

        code, out = self.reply()
        self.assertEqual((code, out), (0, {"action": "none", "post": None, "status": "ready"}))
        self.assertEqual(len(self.state_changes()), 3)

    def test_the_same_reply_again_writes_nothing_and_releases_nothing(self):
        self.given("scheduled", comments=["Asked a question.", REPLY_COMMENT, "Asked again."], events=[park(3)])
        code, out = self.reply(f"  {REPLY}\n")
        self.assertEqual((code, out), (0, {"action": "none", "post": None, "status": "scheduled"}))
        self.assertEqual(self.state_changes(), [])

    def test_a_rerun_after_a_crash_between_the_steps_finishes_the_move(self):
        self.given("scheduled", events=[park(0)])
        self.state["fail"]["unblock"] = 1
        self.assertEqual(self.reply()[0], 1)
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("scheduled", None))

        code, out = self.reply()
        self.assertEqual((code, out["action"]), (0, "unblocked"))
        self.assertEqual(self.state["comments"], [REPLY_COMMENT])
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("ready", None))

    def test_a_card_the_sweep_already_released_is_not_an_error(self):
        self.given("scheduled", events=[park(0)], released_before="unblock")
        code, out = self.reply()
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": False}))
        self.assertEqual(self.state["task"]["status"], "ready")

    def test_an_empty_reply_changes_nothing(self):
        self.given("scheduled", events=[park(0)])
        code, out = self.reply("  \n")
        self.assertEqual((code, out), (1, {"error": "triage-reply needs the reply's text on standard input"}))
        self.assertEqual(self.calls(), [])


class ReleaseRuleTest(CardTransitionTest):
    def reply(self, comments: list[str], events: list[dict], comment_at: list[int] | None = None) -> dict:
        self.given("scheduled", comments=comments, events=events, comment_at=comment_at or [])
        return self.transition("--board", "triage", "triage-reply", "t_1", reply="Again.")[1]

    def released(self, comments: list[str], events: list[dict], comment_at: list[int] | None = None) -> bool:
        """The rule as the replayed reply sees it: the newest comment is already this reply, so nothing is written."""
        out = self.reply([*comments, "Linear reply: Again."], events, comment_at)
        self.assertNotIn(["comment", "t_1", "Linear reply: Again."], self.calls())
        return out["action"] == "unblocked"

    def test_a_reply_past_the_count_releases_the_card(self):
        self.assertTrue(self.released(["Asked."], [park(1)]))

    def test_a_reply_that_landed_mid_round_releases_the_card_though_it_is_older_than_the_park(self):
        self.assertTrue(self.released(["Asked."], [park(1, created_at=5000)]))

    def test_a_reply_within_the_count_does_not(self):
        self.assertFalse(self.released(["Asked."], [park(2)]))

    def test_a_comment_that_is_not_a_reply_past_the_count_does_not(self):
        out = self.reply(["Linear reply: Again.", "SCHEDULED: Needs input: which screen?", "stopped"], [park(1)])
        self.assertEqual(out["action"], "none")

    def test_the_newest_park_sets_the_count(self):
        self.assertFalse(self.released(["Asked."], [park(1), park(2)]))

    def test_a_park_without_a_marker_is_released_only_by_a_reply_written_after_it(self):
        self.assertFalse(self.released([], [park(None, created_at=500)], comment_at=[400]))
        self.assertTrue(self.released([], [park(None, created_at=500)], comment_at=[600]))

    def test_a_card_with_no_park_event_is_released_by_any_reply(self):
        self.assertTrue(self.released([], []))


class ParkWaitingTest(CardTransitionTest):
    def board(self, block_kind: str) -> None:
        path = self.state["db_path"]
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT, status TEXT, block_kind TEXT)")
            db.execute("DELETE FROM tasks")
            db.execute("INSERT INTO tasks VALUES ('t_1', ?, ?)", (self.state["task"]["status"], block_kind))
        db.close()

    def park_waiting(self) -> tuple[int, dict]:
        return self.transition("--board", "triage", "park-waiting")

    def test_a_card_blocked_for_input_is_scheduled_with_a_count_that_leaves_out_the_schedule_comment(self):
        self.given("blocked", comments=["Asked.", "Linear reply: answered an earlier round."])
        self.board("needs_input")

        self.assertEqual(self.park_waiting(), (0, {"parked": ["t_1"]}))

        reason = "Needs input: waits for a reply, parked from blocked by park-waiting\n\ncomments_seen: 2"
        self.assertEqual(self.state_changes(), [["schedule", "t_1", reason]])
        self.assertEqual(self.state["task"]["status"], "scheduled")
        self.assertEqual(self.state["comments"][2], f"SCHEDULED: {reason}")

    def test_a_card_parked_in_triage_for_input_leaves_triage_then_is_scheduled(self):
        self.given("triage", specify_to="ready")
        self.board("needs_input")

        self.assertEqual(self.park_waiting(), (0, {"parked": ["t_1"]}))

        self.assertEqual([call[0] for call in self.state_changes()], ["assign", "specify", "edit", "schedule"])
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("scheduled", None))
        self.assertEqual(self.state["task"]["title"], TITLE)

    def test_a_capability_block_and_a_real_loop_are_left_alone(self):
        for status, kind in (("blocked", "capability"), ("triage", "transient")):
            with self.subTest(status):
                self.given(status, calls=[])
                self.board(kind)

                self.assertEqual(self.park_waiting(), (0, {"parked": []}))
                self.assertEqual(self.state_changes(), [])

    def test_a_second_run_changes_nothing(self):
        self.given("blocked")
        self.board("needs_input")
        self.park_waiting()
        self.state["calls"] = []

        self.assertEqual(self.park_waiting(), (0, {"parked": []}))
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

    def test_stop_leaves_a_scheduled_card_waiting_unassigned_and_a_later_reply_releases_it(self):
        self.given("scheduled", comments=["Asked."], events=[park(1)])
        code, out = self.transition("--board", "triage", "stop", "t_1")
        self.assertEqual((code, out), (0, {"action": "stopped", "post": None, "status": "scheduled"}))
        self.assertEqual(self.state_changes(), [["assign", "t_1", "none"], ["comment", "t_1", "stopped"]])
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("scheduled", None))

        code, out = self.transition("--board", "triage", "triage-reply", "t_1", reply="Carry on.")
        self.assertEqual((code, out["action"]), (0, "unblocked"))
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("ready", None))

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
