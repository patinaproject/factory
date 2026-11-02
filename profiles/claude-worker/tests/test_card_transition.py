from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import REPO, SCRIPTS, FakeBroker, granted, with_interpreter
from tests.test_kanban_board import COMMENTS_AND_EVENTS, TASKS
from tests.test_dispatch_triage import BRIEF_SETTINGS, NO_BRIEF_PARK, IMPLEMENTATION_BODY, QUEUED_NOTE, TRIAGE_BODY, BriefHarness, task

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sqlite3, sys
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
    print(json.dumps([{"slug": "default", "db_path": state["default_db"]}, {"slug": "triage", "db_path": state["db_path"]}]))
    sys.exit()
def contract():
    db = sqlite3.connect(state["default_db"])
    row = db.execute("SELECT completion_contract FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    db.close()
    return row[0]
def save():
    json.dump(state, open(path, "w"))
    if board is None and os.path.exists(state["default_db"]):
        db = sqlite3.connect(state["default_db"])
        db.execute("UPDATE tasks SET status = ? WHERE id = ?", (task["status"], task["id"]))
        db.commit()
        db.close()
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
    if status not in ("running", "ready", "blocked", "review") or (state["acceptance_refuses"] and contract() != "local-only"):
        save()
        sys.exit(f"cannot complete {task['id']} (unknown id or terminal state)")
    task["status"] = "done"
    state["events"].append({"kind": "completed", "payload": None})
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
pr = state["pr"]
if sys.argv[1:] == ["pr", "view", pr["requested"], "--json", "state,url,headRefOid,baseRefName,statusCheckRollup"]:
    print(json.dumps({"state": pr["state"], "url": pr["url"], "headRefOid": pr["head"], "baseRefName": "main",
                      "statusCheckRollup": pr["rollup"]}))
elif sys.argv[1:] == ["api", "repos/example-org/app/rules/branches/main?per_page=100"]:
    if pr["required"] is None:
        sys.exit("gh: HTTP 403")
    print(json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [
        {"context": name} for name in pr["required"]]}}]))
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
REPOSITORY_CONTRACT = "example-org/app"
LOCAL_ONLY = "local-only"
FAILED_NOTE = f"Merged past required checks: {REF}: Lint, Validate failed or had no run on the merged head."


def check_run(conclusion: str, started: str) -> dict:
    return {"__typename": "CheckRun", "name": "Validate", "workflowName": "Pull request", "status": "COMPLETED",
            "conclusion": conclusion, "startedAt": started}


FAILED_NEWEST = [check_run("SUCCESS", "2026-10-16T04:00:00Z"), check_run("FAILURE", "2026-10-16T05:00:00Z")]
PASSED_NEWEST = [check_run("FAILURE", "2026-10-16T04:00:00Z"), check_run("SUCCESS", "2026-10-16T05:00:00Z")]


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
            "gh_fail": 0, "db_path": str(self.dir / "triage.db"), "default_db": str(self.dir / "default.db"),
            "specify_to": "todo", "acceptance_refuses": True,
            "pr": {"requested": URL, "url": URL, "state": "MERGED", "head": HEAD, "rollup": PASSED_NEWEST,
                   "required": ["Validate"]},
            "branch_has_pr": True,
        }
        with closing(sqlite3.connect(self.state["default_db"])) as db:
            db.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, completion_contract TEXT, "
                "last_failure_error TEXT, consecutive_failures INTEGER DEFAULT 0)"
            )
            db.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER)")
            db.execute("INSERT INTO tasks (id, status, completion_contract) VALUES ('t_1', 'triage', ?)", (REPOSITORY_CONTRACT,))
            db.commit()
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

    def contract(self, value: str | None = None) -> str:
        with closing(sqlite3.connect(self.state["default_db"])) as db:
            if value is not None:
                db.execute("UPDATE tasks SET completion_contract = ? WHERE id = 't_1'", (value,))
                db.commit()
            return db.execute("SELECT completion_contract FROM tasks WHERE id = 't_1'").fetchone()[0]

    def transition(self, *argv: str, reply: str = "") -> tuple[int, dict]:
        self.state_path.write_text(json.dumps(self.state))
        with closing(sqlite3.connect(self.state["default_db"])) as db:
            db.execute("UPDATE tasks SET status = ? WHERE id = 't_1'", (self.state["task"]["status"],))
            db.commit()
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

    def test_a_comment_not_from_the_assignee_past_the_count_does_not(self):
        sys.path.insert(0, str(SCRIPTS))
        import reply_inbox

        marked = f"{reply_inbox.NOT_ASSIGNEE_MARKER} Linear reply: rename the button."
        out = self.reply(["Linear reply: Again.", marked], [park(1)])
        self.assertEqual(out["action"], "none")

    def test_the_newest_park_sets_the_count(self):
        self.assertFalse(self.released(["Asked."], [park(1), park(2)]))

    def test_a_park_without_a_marker_is_released_only_by_a_reply_written_after_it(self):
        self.assertFalse(self.released([], [park(None, created_at=500)], comment_at=[400]))
        self.assertTrue(self.released([], [park(None, created_at=500)], comment_at=[600]))

    def test_a_card_with_no_park_event_is_released_by_any_reply(self):
        self.assertTrue(self.released([], []))

    def test_a_reply_never_releases_a_park_that_waits_for_the_gh_login(self):
        login = {"kind": "scheduled", "created_at": 500, "run_id": 1,
                 "payload": {"reason": "Waiting for the worker's gh login: the login is stale"}}
        self.assertFalse(self.released(["Asked."], [login]))
        self.assertFalse(self.released(["Asked."], [park(0), login]))
        self.assertEqual(self.state_changes(), [])

    def test_a_reply_never_releases_a_park_that_waits_for_a_factory_deploy(self):
        deploy = {"kind": "scheduled", "created_at": 500, "run_id": 1,
                  "payload": {"reason": "Waiting for a factory deploy: the profile scripts differ"}}
        self.assertFalse(self.released(["Asked."], [deploy]))
        self.assertFalse(self.released(["Asked."], [park(0), deploy]))
        self.assertEqual(self.state_changes(), [])

    def test_a_question_parked_after_a_login_park_is_released_by_a_reply_again(self):
        login = {"kind": "scheduled", "created_at": 100, "run_id": 1,
                 "payload": {"reason": "Waiting for the worker's gh login: the login is stale"}}
        self.assertTrue(self.released(["Asked."], [login, park(1)]))


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

    def test_a_comment_reopens_the_review_with_no_reason_and_posts_it_plain(self):
        self.given("blocked")
        code, out = self.transition("unblock", "t_1", "--repo", "example-org/app", "--branch", "pat-1-work",
                                    "--comment", "Resumed for a Linear reply")
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": True}))
        self.assertEqual(self.state_changes(), [
            ["unblock", "t_1"], ["request-review", "t_1"], ["reopen-review", "t_1"],
            ["comment", "t_1", "Resumed for a Linear reply"],
        ])

    def test_a_comment_on_a_card_without_a_pull_request_posts_nothing(self):
        self.given("blocked", branch_has_pr=False)
        code, out = self.transition("unblock", "t_1", "--repo", "example-org/app", "--branch", "pat-1-work",
                                    "--comment", "Resumed for a Linear reply")
        self.assertEqual((code, out), (0, {"action": "unblocked", "post": None, "pull_request": False}))
        self.assertEqual(self.state_changes(), [["unblock", "t_1"]])

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
        self.assertEqual(self.state_changes(), [["reopen-review", "t_1", "--reason", "changes requested on GitHub; see the card comments"]])

    def test_reopen_with_a_comment_hands_the_card_back_with_no_reason(self):
        self.given("review")
        code, out = self.transition("reopen", "t_1", "--comment", "Resumed for a Linear reply; see the card comments")
        self.assertEqual((code, out), (0, {"action": "reopened", "post": None}))
        self.assertEqual(self.state_changes(), [
            ["reopen-review", "t_1"], ["comment", "t_1", "Resumed for a Linear reply; see the card comments"],
        ])

    QUOTA_ERROR = "pid 1 exited rate-limited (quota wall) — requeued without counting a failure"

    def failure_state(self, error: str | None, *runs: tuple[str, int], failures: int = 2) -> None:
        with closing(sqlite3.connect(self.state["default_db"])) as db:
            db.execute("UPDATE tasks SET last_failure_error = ?, consecutive_failures = ? WHERE id = 't_1'", (error, failures))
            db.executemany("INSERT INTO task_runs (task_id, outcome, ended_at) VALUES ('t_1', ?, ?)", runs)
            db.commit()

    def failure_row(self) -> tuple:
        with closing(sqlite3.connect(self.state["default_db"])) as db:
            return db.execute("SELECT last_failure_error, consecutive_failures FROM tasks WHERE id = 't_1'").fetchone()

    def held_as_blocker_auth(self) -> bool:
        error, _ = self.failure_row()
        with closing(sqlite3.connect(self.state["default_db"])) as db:
            newest = db.execute(
                "SELECT outcome FROM task_runs WHERE task_id = 't_1' AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
            ).fetchone()
        outcome = newest[0] if newest else None
        return bool(error and outcome not in ("crashed", "rate_limited") and re.search(r"quota|rate.?limit|auth", error))

    def test_reopen_clears_an_old_quota_error_after_a_review_run_and_keeps_the_failure_count(self):
        for ended in ("review_requested", "completed"):
            with self.subTest(ended):
                self.setUp()
                self.given("review")
                self.failure_state(self.QUOTA_ERROR, ("rate_limited", 100), (ended, 200))
                self.assertTrue(self.held_as_blocker_auth())
                code, out = self.transition("reopen", "t_1", "--reason", "changes requested")
                self.assertEqual((code, out), (0, {"action": "reopened", "post": None}))
                self.assertEqual(self.state["task"]["status"], "ready")
                self.assertEqual(self.failure_row(), (None, 2))
                self.assertFalse(self.held_as_blocker_auth())

    def test_reopen_keeps_the_error_when_the_newest_ended_run_is_not_a_review(self):
        for outcome in ("rate_limited", "spawn_failed", "crashed", "timed_out", "gave_up", "blocked"):
            with self.subTest(outcome):
                self.setUp()
                self.given("review")
                self.failure_state(self.QUOTA_ERROR, ("review_requested", 100), (outcome, 200))
                code, _ = self.transition("reopen", "t_1", "--reason", "changes requested")
                self.assertEqual(code, 0)
                self.assertEqual(self.failure_row(), (self.QUOTA_ERROR, 2))

    def test_reopen_keeps_the_error_of_a_card_with_no_ended_run(self):
        self.given("review")
        self.failure_state(self.QUOTA_ERROR)
        self.transition("reopen", "t_1", "--reason", "changes requested")
        self.assertEqual(self.failure_row(), (self.QUOTA_ERROR, 2))

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
    """A merged pull request's card, on a board whose acceptance refuses every contract but `local-only`."""

    def merged(self, url: str = URL) -> tuple[int, dict]:
        return self.transition("merged", "t_1", "--pr", url)

    def failing(self, status: str, **changes) -> None:
        self.given(status, **changes)
        self.state["pr"].update(rollup=FAILED_NEWEST, required=["Validate", "Lint"])

    def test_a_merged_card_ends_done_from_each_status_whatever_its_checks_say(self):
        steps = {
            "review": [], "blocked": [], "ready": [],
            "todo": [["promote", "t_1", "pull", "request", "merged"]],
            "triage": [["assign", "t_1", "none"], ["specify", "t_1"], ["edit", "t_1", "--title", TITLE, "--body", BODY],
                       ["promote", "t_1", "pull", "request", "merged"]],
            "scheduled": [["assign", "t_1", "none"], ["unblock", "t_1"]],
        }
        for status, before in steps.items():
            with self.subTest(status):
                self.setUp()
                self.failing(status)
                self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
                self.assertEqual(self.state_changes(), [["comment", "t_1", FAILED_NOTE], *before, ["complete", "t_1", *SUMMARY]])
                self.assertEqual(self.state["task"]["status"], "done")
                self.assertEqual([event["kind"] for event in self.state["events"]], ["completed"])
                self.assertEqual(self.contract(), LOCAL_ONLY)

    def test_a_pull_request_github_does_not_report_merged_changes_nothing(self):
        for state in ("OPEN", "CLOSED"):
            with self.subTest(state):
                self.setUp()
                self.given("review")
                self.state["pr"]["state"] = state
                self.assertEqual(self.merged(), (0, {"action": "not_merged", "post": None, "state": state}))
                self.assertEqual(self.state_changes(), [])
                self.assertEqual(self.contract(), REPOSITORY_CONTRACT)

    def test_a_contract_that_names_another_pull_request_changes_nothing(self):
        for contract, shown in (("https://github.com/example-org/app/pull/13", "example-org/app#13"),
                                ("example-org/other", "example-org/other")):
            with self.subTest(contract):
                self.setUp()
                self.given("review")
                self.contract(contract)
                self.assertEqual(self.merged(), (0, {"action": "other_pull", "post": None, "contract": shown}))
                self.assertEqual(self.state_changes(), [])
                self.assertEqual(self.contract(), contract)

    def test_a_contract_hermes_bound_to_this_pull_request_is_written_whatever_its_case(self):
        for contract in (URL, "https://github.com/Example-Org/App/pull/12", "https://github.com/example-org/app/pull/12/"):
            with self.subTest(contract):
                self.setUp()
                self.given("review")
                self.contract(contract)
                self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
                self.assertEqual(self.contract(), LOCAL_ONLY)

    def test_the_webhook_url_is_read_and_github_names_the_pull_request(self):
        self.given("review")
        self.state["pr"]["requested"] = "https://github.com/Example-Org/App/pull/12"
        self.assertEqual(self.merged("https://github.com/Example-Org/App/pull/12"), (0, {"action": "completed", "post": "response"}))
        self.assertEqual(self.state_changes(), [["complete", "t_1", *SUMMARY]])

    def test_a_running_card_gets_the_write_and_is_left_to_its_worker(self):
        self.failing("running")
        self.assertEqual(self.merged(), (0, {"action": "worker_completes", "post": None}))
        self.assertEqual(self.state_changes(), [["comment", "t_1", FAILED_NOTE]])
        self.assertEqual(self.contract(), LOCAL_ONLY)
        self.state["task"]["status"] = "review"
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
        self.assertEqual(self.state["comments"], [FAILED_NOTE])

    def test_a_done_or_archived_card_gets_no_write_and_no_second_comment(self):
        self.failing("review")
        self.merged()
        for status in ("done", "archived"):
            with self.subTest(status):
                self.state.update(calls=[], gh_calls=[])
                self.state["task"]["status"] = status
                self.assertEqual(self.merged(), (0, {"action": "none", "post": None, "status": status}))
                self.assertEqual(self.state_changes(), [])
                self.assertEqual(self.state["gh_calls"], [])
                self.assertEqual(self.state["comments"], [FAILED_NOTE])

    def test_a_done_card_before_the_first_run_keeps_its_contract(self):
        self.given("done")
        self.assertEqual(self.merged(), (0, {"action": "none", "post": None, "status": "done"}))
        self.assertEqual(self.contract(), REPOSITORY_CONTRACT)

    def test_a_failed_complete_leaves_the_card_local_only_and_the_next_run_completes_it_without_a_second_comment(self):
        self.failing("review", fail={"complete": 2})
        error = (f"Pull request {REF} merged and the card's completion_contract is local-only, but hermes kanban "
                 "complete failed: hermes: cannot complete t_1; the next finish-merged run completes it")
        self.assertEqual(self.merged(), (1, {"error": error}))
        self.assertEqual((self.state["task"]["status"], self.contract()), ("review", LOCAL_ONLY))
        self.assertEqual(self.merged(), (1, {"error": error}))
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
        self.assertEqual(self.state["comments"], [FAILED_NOTE])
        self.assertEqual(self.state["task"]["status"], "done")

    def test_a_merge_whose_required_checks_passed_on_their_newest_runs_posts_no_comment(self):
        self.given("ready")
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
        self.assertEqual(self.state_changes(), [["complete", "t_1", *SUMMARY]])

    def test_unreadable_required_checks_are_commented_and_the_card_still_finishes(self):
        self.given("ready")
        self.state["pr"]["required"] = None
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))
        self.assertEqual(self.state["comments"], [
            f"Merged past required checks: {REF}: its required checks could not be read: gh api "
            "repos/example-org/app/rules/branches/main?per_page=100 failed: gh: HTTP 403",
        ])

    def test_a_pull_request_github_cannot_read_twice_changes_nothing(self):
        self.given("review", gh_fail=2)
        code, out = self.merged()
        self.assertEqual((code, out), (1, {"error": f"pull request {REF} could not be read: gh pr view failed: gh: HTTP 502"}))
        self.assertEqual(self.state_changes(), [])
        self.assertEqual(self.contract(), REPOSITORY_CONTRACT)

    def test_a_pull_request_read_on_the_retry_finishes_the_card(self):
        self.given("review", gh_fail=1)
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))

    def test_a_refused_promote_is_reported_and_the_next_run_finishes_the_card(self):
        self.given("todo", fail={"promote": 1})
        code, out = self.merged()
        self.assertEqual((code, out), (1, {"error": (
            f"Pull request {REF} merged and the card's completion_contract is local-only, but hermes kanban promote "
            "failed: hermes: cannot promote t_1; the next finish-merged run completes it")}))
        self.assertEqual(self.merged(), (0, {"action": "completed", "post": "response"}))

    def test_a_triage_card_whose_specify_fails_is_reassigned_and_reported(self):
        self.state["fail"]["specify"] = 1
        code, out = self.merged()
        self.assertEqual((code, out), (1, {"error": (
            f"Pull request {REF} merged and the card's completion_contract is local-only, but hermes kanban specify "
            "failed: hermes: cannot specify t_1; the card stays in triage; the next finish-merged run completes it")}))
        self.assertEqual(self.state_changes(), [["assign", "t_1", "none"], ["specify", "t_1"], ["assign", "t_1", "claude-worker"]])

    def test_a_triage_card_that_specify_left_ready_is_completed_without_a_promote(self):
        self.state["specify_to"] = "ready"
        self.merged()
        self.assertEqual([call[0] for call in self.state_changes()], ["assign", "specify", "edit", "complete"])

    def test_after_specify_succeeds_a_triage_card_is_never_assigned(self):
        for name, changes in {"completed": {}, "promote refused": {"fail": {"promote": 1}},
                              "completion refused": {"fail": {"complete": 1}}, "edit keeps failing": {"fail": {"edit": 2}}}.items():
            with self.subTest(name):
                self.setUp()
                self.failing("triage", **changes)
                self.merged()
                after = self.state_changes()[self.state_changes().index(["specify", "t_1"]) + 1 :]
                self.assertEqual([call for call in after if call[0] == "assign" and call[2] != "none"], [])

    def test_no_comment_names_a_pull_request_by_url(self):
        for changes in ({}, {"required": None}):
            with self.subTest(changes):
                self.setUp()
                self.failing("review")
                self.state["pr"].update(changes)
                self.merged()
                self.assertTrue(self.state["comments"])
                for comment in self.state["comments"]:
                    self.assertIn(REF, comment)
                    self.assertNotIn("https://github.com/", comment)

    def test_a_url_that_is_not_a_pull_request_is_refused_before_any_change(self):
        self.given("ready")
        code, out = self.merged("https://github.com/example-org/app/issues/12")
        self.assertEqual((code, out), (1, {"error": "https://github.com/example-org/app/issues/12 is not a GitHub pull request URL"}))
        self.assertEqual(self.calls(), [])


class BriefWaitTest(BriefHarness):
    """`sweep_brief` against fake boards and the real Linear client on a fake Linear."""

    def sweep_brief(self, triage_tasks: list | None = None) -> dict:
        """One sweep; a second call keeps the boards the first one left."""
        if triage_tasks is not None:
            self.state_path.write_text(json.dumps({
                "default_tasks": [self.impl], "tasks": triage_tasks, "settings": BRIEF_SETTINGS,
                "db_path": str(self.board), "default_db": str(self.default_board),
            }))
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path),
               "HERMES_HOME": str(self.home)}
        proc = subprocess.run(
            [sys.executable, "-c", "import json, card_transition; print(json.dumps(card_transition.sweep_brief()))"],
            cwd=SCRIPTS, env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def hermes_calls(self) -> list:
        log = Path(f"{self.state_path}.log")
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call[2:] for call in calls if call[:2] == ["kanban", "--board"] and call[3] not in ("show", "list")]

    def test_a_card_without_a_triage_card_queues_one_and_posts_one_note_once(self) -> None:
        self.linear({"documents": []})

        first = self.sweep_brief([])
        second = self.sweep_brief()

        self.assertEqual(first, {"released": [], "queued": {"t_impl": "t_triage_1"}, "failed": {}})
        self.assertEqual(second, {"released": [], "queued": {}, "failed": {}})
        self.assertEqual(self.hermes_calls(), [
            ["triage", "create", "ABC-123: Fix checkout", "--priority", "2", "--workspace", "worktree:/repos/app",
             "--idempotency-key", "linear-triage:issue-uuid", "--body", TRIAGE_BODY, "--json"],
            ["default", "comment", "t_impl", "Queued for triage as t_triage_1"],
        ])
        self.assertEqual(self.activities(), [
            {"agentSessionId": "session-1", "content": {"type": "thought", "body": QUEUED_NOTE}},
        ])
        self.assertEqual(self.statuses()["t_impl"], ("scheduled", "claude-worker"))
        self.assertEqual(self.statuses()["t_triage_1"], ("ready", None))

    def test_a_done_triage_card_is_archived_before_the_new_one_is_created(self) -> None:
        self.linear({"documents": []})
        done = {**task("t_old", "done"), "body": TRIAGE_BODY, "idempotency_key": "linear-triage:issue-uuid",
                "workspace_kind": "scratch"}

        swept = self.sweep_brief([done])

        self.assertEqual(swept["queued"], {"t_impl": "t_triage_2"})
        self.assertEqual([call[:3] for call in self.hermes_calls()], [
            ["triage", "archive", "t_old"], ["triage", "create", "ABC-123: Fix checkout"],
            ["default", "comment", "t_impl"],
        ])
        self.assertEqual(self.statuses()["t_old"], ("archived", None))
        self.assertEqual(len(self.activities()), 1)

    def test_a_brief_on_a_ready_issue_releases_the_card(self) -> None:
        swept = self.sweep_brief([])

        self.assertEqual(swept, {"released": ["t_impl"], "queued": {}, "failed": {}})
        self.assertEqual(self.hermes_calls(), [["default", "unblock", "t_impl"]])
        self.assertEqual(self.statuses()["t_impl"], ("ready", "claude-worker"))
        self.assertEqual(self.activities(), [])

    def test_an_issue_without_ready_for_agent_releases_the_card(self) -> None:
        self.linear({"labels": ["Needs info"], "documents": []})

        swept = self.sweep_brief([])

        self.assertEqual(swept["released"], ["t_impl"])
        self.assertEqual(self.statuses()["t_impl"], ("ready", "claude-worker"))
        self.assertEqual(self.activities(), [])

    def test_a_wait_whose_triage_card_ended_without_a_brief_queues_and_posts_nothing(self) -> None:
        self.linear({"documents": []})
        self.park(self.default_board, "t_queued", NO_BRIEF_PARK, comments=("Queued for triage as t_old",))
        queued = {**task("t_queued", "scheduled", "claude-worker"), "body": IMPLEMENTATION_BODY}
        self.impl = {**self.impl, "assignee": None}
        done = {**task("t_old", "done"), "body": TRIAGE_BODY, "workspace_kind": "scratch"}
        self.state_path.write_text(json.dumps({
            "default_tasks": [self.impl, queued], "tasks": [done], "settings": BRIEF_SETTINGS,
            "db_path": str(self.board), "default_db": str(self.default_board),
        }))

        swept = self.sweep_brief()

        self.assertEqual(swept, {"released": [], "queued": {}, "failed": {}})
        self.assertEqual((self.hermes_calls(), self.activities()), ([], []))
        self.assertEqual(self.statuses()["t_queued"], ("scheduled", "claude-worker"))
        self.assertEqual(self.statuses()["t_old"], ("done", None))

    def test_a_failed_linear_read_leaves_the_card_parked_and_is_reported(self) -> None:
        self.linear({"documents_error": "Entity not found"})

        swept = self.sweep_brief([])

        self.assertEqual(swept, {"released": [], "queued": {},
                                 "failed": {"t_impl": "reading Linear issue ABC-123 failed: Entity not found"}})
        self.assertEqual((self.hermes_calls(), self.activities()), ([], []))
        self.assertEqual(self.statuses()["t_impl"], ("scheduled", "claude-worker"))


FAKE_ISSUE_GH = """#!/usr/bin/env python3
import json, os, sys, time
path = os.environ["FAKE_GH_ISSUES"]
with open(path + ".log", "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "config": os.environ.get("GH_CONFIG_DIR")}) + "\\n")
answer = json.load(open(path))[sys.argv[3]]
time.sleep(answer.get("sleep", 0))
if "error" in answer:
    sys.stderr.write(answer["error"] + "\\n")
    sys.exit(1)
print(json.dumps(answer))
"""
ISSUE = "https://github.com/example-org/app/issues/7"
ISSUE_PARKED = f"Waiting for an issue: {ISSUE}"
OPEN = {"state": "OPEN", "stateReason": None}
NOT_FOUND = {"error": "GraphQL: Could not resolve to an issue or pull request with the number of 7. (repository.issue)"}


class IssueWaitTest(BriefHarness):
    """`sweep_issue` against fake boards, a fake gh, and the real Linear client on a fake Linear."""

    def setUp(self) -> None:
        super().setUp()
        self.default_board.unlink()
        with closing(sqlite3.connect(self.default_board)) as db:
            db.execute(TASKS)
            db.executescript(COMMENTS_AND_EVENTS)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(with_interpreter(FAKE_ISSUE_GH))
        os.chmod(bin_dir / "gh", 0o755)
        self.bin = bin_dir
        self.issues = self.tmp / "issues.json"
        self.issues.write_text(json.dumps({ISSUE: OPEN}))
        self.login(30 * 60)
        self.tasks: dict[str, list] = {"default": [], "triage": []}

    def login(self, seconds_left: float | None) -> None:
        gh = self.home / "gh"
        gh.mkdir(exist_ok=True)
        (gh / "token-expiry.json").unlink(missing_ok=True)
        if seconds_left is not None:
            (gh / "token-expiry.json").write_text(json.dumps({"expires_at": time.time() + seconds_left}))

    def wait(self, board: str, task_id: str, reason: str = ISSUE_PARKED, assignee: str | None = "claude-worker",
             comments: tuple = ()) -> None:
        self.park(self.default_board if board == "default" else self.board, task_id, reason, assignee, comments)
        self.tasks[board].append({**task(task_id, "scheduled", assignee), "body": IMPLEMENTATION_BODY})

    def sweep(self, seconds: float | None = None) -> dict:
        self.state_path.write_text(json.dumps({
            "default_tasks": self.tasks["default"], "tasks": self.tasks["triage"], "settings": BRIEF_SETTINGS,
            "db_path": str(self.board), "default_db": str(self.default_board),
        }))
        limit = "" if seconds is None else f"card_transition.ISSUE_READ_SECONDS = {seconds}; "
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path),
               "HERMES_HOME": str(self.home), "FAKE_GH_ISSUES": str(self.issues),
               "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        proc = subprocess.run(
            [sys.executable, "-c", f"import json, card_transition; {limit}print(json.dumps(card_transition.sweep_issue()))"],
            cwd=SCRIPTS, env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def gh_calls(self) -> list:
        log = Path(f"{self.issues}.log")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def comments(self) -> list:
        log = Path(f"{self.state_path}.log")
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call[2:] for call in calls if call[:2] == ["kanban", "--board"] and call[3] == "comment"]

    def read_issue(self, answer: dict) -> None:
        self.issues.write_text(json.dumps({ISSUE: answer}))

    def assert_released_on_both_boards(self, reason: str, comments: tuple = ()) -> None:
        self.wait("default", "t_default", comments=comments)
        self.wait("triage", "t_triage", comments=comments)

        swept = self.sweep()

        self.assertEqual(swept, {"released": ["t_default", "t_triage"], "failed": {}})
        self.assertEqual(self.statuses()["t_default"], ("ready", "claude-worker"))
        self.assertEqual(self.statuses()["t_triage"], ("ready", None))
        note = f"Parked on issue {ISSUE}, and {reason}."
        self.assertEqual(self.comments(), [["default", "comment", "t_default", note], ["triage", "comment", "t_triage", note]])

    def test_an_open_issue_keeps_the_card_parked_and_posts_one_thought_across_sweeps(self) -> None:
        self.wait("default", "t_default")
        self.wait("triage", "t_triage")

        first = self.sweep()
        second = self.sweep()

        self.assertEqual((first, second), ({"released": [], "failed": {}}, {"released": [], "failed": {}}))
        self.assertEqual(self.statuses()["t_default"], ("scheduled", "claude-worker"))
        self.assertEqual(self.statuses()["t_triage"], ("scheduled", "claude-worker"))
        self.assertEqual([activity["content"] for activity in self.activities()], [
            {"type": "thought", "body": f"A required check keeps failing, so this card waits for {ISSUE} and continues when it closes."},
        ] * 2)
        self.assertEqual(
            [(call[0], call[2]) for call in self.comments()], [("default", "t_default"), ("triage", "t_triage")],
        )
        self.assertEqual({call[3] for call in self.comments()}, {
            f"Parked on issue {ISSUE}. This card continues once the issue closes or a person replies.",
        })

    def test_each_issue_is_read_once_per_sweep_with_the_workers_gh_login(self) -> None:
        self.wait("default", "t_default")
        self.wait("triage", "t_triage")

        self.sweep()

        self.assertEqual(self.gh_calls(), [{
            "argv": ["issue", "view", ISSUE, "--json", "state,stateReason"], "config": str(self.home / "gh"),
        }])

    def test_an_issue_closed_as_completed_releases_the_card_on_both_boards(self) -> None:
        self.read_issue({"state": "CLOSED", "stateReason": "COMPLETED"})
        self.assert_released_on_both_boards("the issue closed as completed")

    def test_an_issue_closed_as_not_planned_releases_the_card(self) -> None:
        self.read_issue({"state": "CLOSED", "stateReason": "NOT_PLANNED"})
        self.assert_released_on_both_boards("the issue closed as not planned")

    def test_an_issue_that_is_not_found_releases_the_card(self) -> None:
        self.read_issue(NOT_FOUND)
        self.assert_released_on_both_boards("the issue was not found")

    def test_a_reply_newer_than_the_park_releases_the_card_with_an_open_issue(self) -> None:
        self.assert_released_on_both_boards("a person replied", comments=("Linear reply: go on without it",))
        self.assertEqual(self.gh_calls(), [])

    def test_a_reply_older_than_the_park_does_not_release_the_card(self) -> None:
        self.wait("default", "t_default")
        with closing(sqlite3.connect(self.default_board)) as db:
            db.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t_default', 'default', "
                       "'Linear reply: before the park', 100)")
            db.commit()

        self.assertEqual(self.sweep(), {"released": [], "failed": {}})
        self.assertEqual(self.statuses()["t_default"], ("scheduled", "claude-worker"))

    def test_a_stopped_or_unassigned_card_is_not_released(self) -> None:
        self.read_issue({"state": "CLOSED", "stateReason": "COMPLETED"})
        self.wait("default", "t_stopped", comments=("stopped",))
        self.wait("default", "t_unassigned", assignee=None)

        self.assertEqual(self.sweep(), {"released": [], "failed": {}})
        self.assertEqual({status for status, _ in self.statuses().values()}, {"scheduled"})
        self.assertEqual(self.comments(), [])

    def test_a_gh_failure_or_timeout_releases_nothing_and_is_reported(self) -> None:
        self.wait("default", "t_default")
        for name, answer, seconds, error in (
            ("error", {"error": "gh: HTTP 502"}, None, "gh issue view " + ISSUE + " failed: gh: HTTP 502"),
            ("timeout", {"state": "CLOSED", "stateReason": "COMPLETED", "sleep": 5}, 0.3,
             f"gh issue view {ISSUE} timed out after 0.3 seconds"),
        ):
            with self.subTest(name):
                self.read_issue(answer)

                swept = self.sweep(seconds)

                self.assertEqual(swept, {"released": [], "failed": {"t_default": error}})
                self.assertEqual(self.statuses()["t_default"], ("scheduled", "claude-worker"))

    def test_a_login_that_is_not_fresh_leaves_the_card_parked_and_reads_nothing(self) -> None:
        self.read_issue({"state": "CLOSED", "stateReason": "COMPLETED"})
        self.wait("default", "t_default")
        self.login(14 * 60)

        self.assertEqual(self.sweep(), {"released": [], "failed": {}})
        self.assertEqual((self.gh_calls(), self.statuses()["t_default"]), ([], ("scheduled", "claude-worker")))

    def test_dispatch_triage_reports_a_failed_read_and_still_assigns_the_waiting_card(self) -> None:
        self.read_issue({"error": "gh: HTTP 502"})
        self.wait("default", "t_default")
        self.tasks["triage"].append(task("t_ready", "ready"))

        proc = self.run_script(
            self.tasks["triage"], settings=BRIEF_SETTINGS, default_tasks=self.tasks["default"],
            default_db=str(self.default_board),
            _env={"FAKE_GH_ISSUES": str(self.issues), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"},
        )

        self.assertEqual((proc.returncode, proc.stdout), (1, "assigned t_ready\n"))
        self.assertIn(f"releasing t_default for a GitHub issue failed: gh issue view {ISSUE} failed: gh: HTTP 502", proc.stderr)
        self.assertEqual(self.statuses()["t_default"], ("scheduled", "claude-worker"))

    def test_dispatch_triage_releases_a_card_whose_issue_closed(self) -> None:
        self.read_issue({"state": "CLOSED", "stateReason": "COMPLETED"})
        self.wait("default", "t_default")

        proc = self.run_script(
            self.tasks["triage"], settings=BRIEF_SETTINGS, default_tasks=self.tasks["default"],
            default_db=str(self.default_board),
            _env={"FAKE_GH_ISSUES": str(self.issues), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"},
        )

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "released t_default\n", ""))


if __name__ == "__main__":
    unittest.main()
