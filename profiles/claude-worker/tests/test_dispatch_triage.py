from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import REPO, SCRIPTS, FakeBroker, git, granted, with_interpreter
from tests.test_kanban_board import COMMENTS_AND_EVENTS, TASKS

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_HERMES_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]
with open(path + ".log", "a") as log:
    log.write(json.dumps(argv) + "\\n")
if argv[:3] == ["config", "get", "kanban.dispatch_profiles"]:
    if "dispatch_profiles" in state:
        print(json.dumps(state["dispatch_profiles"]))
    else:
        sys.stderr.write("Config key not set: kanban.dispatch_profiles\\n")
        sys.exit(1)
elif argv[:2] == ["config", "get"]:
    print(json.dumps(state["settings"]))
elif argv[:2] == ["config", "set"]:
    state["dispatch_profiles"] = []
    json.dump(state, open(path, "w"))
elif argv[:2] == ["config", "unset"]:
    state.pop("dispatch_profiles", None)
    json.dump(state, open(path, "w"))
elif argv == ["kanban", "boards", "list", "--json"]:
    print(json.dumps([{"slug": "default", "db_path": state.get("default_db", "/nowhere")},
                      {"slug": "triage", "db_path": state["db_path"]}]))
elif argv[:2] == ["kanban", "--board"] and argv[3] in ("list", "assign", "unblock", "show", "create", "archive", "comment"):
    tasks = state["tasks"] if argv[2] == "triage" else state["default_tasks"]
    if argv[3] == "list":
        print(json.dumps([t for t in tasks if t["status"] != "archived"]))
    elif argv[3] == "create":
        option = lambda name: argv[argv.index(name) + 1]
        key = option("--idempotency-key")
        card = next((t for t in tasks if t.get("idempotency_key") == key and t["status"] != "archived"), None)
        if card is None:
            card = {"id": f"t_triage_{len(tasks) + 1}", "title": argv[4], "body": option("--body"), "status": "ready",
                    "assignee": None, "priority": int(option("--priority")), "created_at": 500,
                    "idempotency_key": key, "workspace": option("--workspace"), "workspace_kind": "scratch"}
            tasks.append(card)
            json.dump(state, open(path, "w"))
        print(json.dumps(card))
    elif argv[3] == "archive":
        next(t for t in tasks if t["id"] == argv[4])["status"] = "archived"
        json.dump(state, open(path, "w"))
    elif argv[3] == "comment":
        import sqlite3
        db = sqlite3.connect(state["db_path"] if argv[2] == "triage" else state["default_db"])
        db.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'claude-worker', ?, 1000)",
                   (argv[4], argv[5]))
        db.commit()
    elif argv[3] == "assign" and not state.get("assign_fails"):
        next(t for t in tasks if t["id"] == argv[4])["assignee"] = None if argv[5] == "none" else argv[5]
        json.dump(state, open(path, "w"))
    elif argv[3] == "assign":
        sys.exit("hermes: refused " + repr(argv))
    elif argv[3] == "show":
        print(json.dumps({"task": next(t for t in tasks if t["id"] == argv[4])}))
    elif argv[3] == "unblock":
        card = next(t for t in tasks if t["id"] == argv[4])
        if card["status"] != "scheduled" or state.get("unblock_fails"):
            sys.exit("cannot unblock " + argv[4])
        card["status"] = "ready"
        json.dump(state, open(path, "w"))
else:
    sys.exit("hermes: refused " + repr(argv))
"""


def task(task_id: str, status: str, assignee: str | None = None, priority: int = 0, created_at: int = 100) -> dict:
    return {"id": task_id, "status": status, "assignee": assignee, "priority": priority, "created_at": created_at}


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.hermes = self.tmp / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)
        self.state_path = self.tmp / "state.json"
        self.home = self.tmp / "r" / "p" / "h"
        (self.home / "plugins").mkdir(parents=True)
        (self.home / "plugins" / "blocker-order").symlink_to(REPO / "plugins" / "blocker-order")
        self.board = self.tmp / "triage.db"
        with closing(sqlite3.connect(self.board)) as db:
            db.execute(TASKS)
            db.executescript(COMMENTS_AND_EVENTS)

    def run_script(self, tasks: list, settings: object = None, _env: dict | None = None, **state) -> subprocess.CompletedProcess:
        settings = {"triage": {"max_running": 2}} if settings is None else settings
        state = {"default_tasks": [], "tasks": tasks, "settings": settings, "db_path": str(self.board), **state}
        self.state_path.write_text(json.dumps(state))
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path),
               "HERMES_HOME": str(self.home), "HOST_HOLD_LOADAVG": "0,0,0", "HOST_HOLD_CPUS": "8", **(_env or {})}
        return subprocess.run([sys.executable, str(SCRIPTS / "dispatch-triage")], env=env, capture_output=True, text=True)

    def assignees(self) -> dict:
        return {t["id"]: t["assignee"] for t in json.loads(self.state_path.read_text())["tasks"]}


class DispatchTriageTest(Harness):
    def test_assigns_waiting_cards_up_to_the_cap_by_priority_then_age(self) -> None:
        proc = self.run_script([
            task("t_new", "ready", created_at=300),
            task("t_old", "ready", created_at=100),
            task("t_urgent", "ready", priority=4, created_at=400),
        ])

        self.assertEqual((proc.returncode, proc.stdout), (0, "assigned t_urgent\nassigned t_old\n"))
        self.assertEqual(self.assignees(), {"t_new": None, "t_old": "claude-worker", "t_urgent": "claude-worker"})

    def test_running_and_assigned_ready_cards_use_the_cap(self) -> None:
        proc = self.run_script([
            task("t_running", "running", "claude-worker"),
            task("t_queued", "ready", "claude-worker"),
            task("t_waiting", "ready"),
        ])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIsNone(self.assignees()["t_waiting"])

    def test_blocked_scheduled_done_and_review_cards_do_not_use_the_cap(self) -> None:
        proc = self.run_script([
            task("t_blocked", "blocked", "claude-worker"),
            task("t_asking", "scheduled", "claude-worker"),
            task("t_done", "done", "claude-worker"),
            task("t_review", "review", "claude-worker"),
            task("t_parked", "triage", "claude-worker"),
            task("t_waiting", "ready"),
        ], settings={"triage": {"max_running": 1}})

        self.assertEqual((proc.returncode, proc.stdout), (0, "assigned t_waiting\n"))

    def waiting_card(self, task_id: str, seen: int, comments: list[str]) -> None:
        reason = json.dumps({"reason": f"Needs input: which screen?\n\ncomments_seen: {seen}"})
        with closing(sqlite3.connect(self.board)) as db:
            db.execute("INSERT INTO tasks (id, title, body, branch_name, status, assignee, block_kind) "
                       "VALUES (?, 'T', '', NULL, 'scheduled', 'claude-worker', NULL)", (task_id,))
            db.executemany("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'default', ?, 100)",
                           [(task_id, body) for body in comments])
            db.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, 200)",
                       (task_id, reason))
            db.commit()

    def test_a_waiting_card_with_a_reply_past_its_count_is_released_then_assigned_within_the_cap(self) -> None:
        self.waiting_card("t_answered", 1, ["Asked.", "Linear reply: the settings screen"])
        self.waiting_card("t_asking", 1, ["Asked."])

        proc = self.run_script([
            task("t_answered", "scheduled", "claude-worker"),
            task("t_asking", "scheduled", "claude-worker"),
            task("t_running", "running", "claude-worker"),
            task("t_later", "ready", created_at=300),
        ])

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "released t_answered\nassigned t_answered\n", ""))
        tasks = {t["id"]: (t["status"], t["assignee"]) for t in json.loads(self.state_path.read_text())["tasks"]}
        self.assertEqual(tasks, {
            "t_answered": ("ready", "claude-worker"), "t_asking": ("scheduled", "claude-worker"),
            "t_running": ("running", "claude-worker"), "t_later": ("ready", None),
        })
        calls = [json.loads(line) for line in Path(f"{self.state_path}.log").read_text().splitlines()]
        board_calls = [call[3:] for call in calls if call[:3] == ["kanban", "--board", "triage"]]
        self.assertEqual(board_calls, [
            ["assign", "t_answered", "none"], ["unblock", "t_answered"], ["list", "--json"],
            ["assign", "t_answered", "claude-worker"],
        ])

    def test_a_board_it_cannot_read_still_assigns_and_exits_1(self) -> None:
        self.board.unlink()

        proc = self.run_script([task("t_waiting", "ready")])

        self.assertEqual((proc.returncode, proc.stdout), (1, "assigned t_waiting\n"))
        self.assertIn("cannot read the waiting cards", proc.stderr)

    def test_an_unset_or_invalid_cap_refuses_without_touching_the_board(self) -> None:
        for settings in ({}, {"triage": {}}, {"triage": {"max_running": 0}}, {"triage": {"max_running": "2"}},
                         {"triage": {"max_running": True}}):
            with self.subTest(settings=settings):
                proc = self.run_script([task("t_waiting", "ready")], settings=settings)

                self.assertEqual(proc.returncode, 2)
                self.assertIn("triage.max_running", proc.stderr)
                self.assertIsNone(self.assignees()["t_waiting"])

    def test_a_failed_assignment_exits_1(self) -> None:
        proc = self.run_script([task("t_waiting", "ready")], assign_fails=True)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("hermes kanban --board triage assign failed", proc.stderr)


PARKED = "Waiting for the worker's gh login: the worker's gh login has 3 minutes left"


class ParkedHarness(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.default_board = self.tmp / "default.db"
        with closing(sqlite3.connect(self.default_board)) as db:
            db.execute(TASKS)
            db.executescript(COMMENTS_AND_EVENTS)

    def park(self, board: Path, task_id: str, reason: str = PARKED, assignee: str | None = "claude-worker",
             comments: tuple = ()) -> None:
        with closing(sqlite3.connect(board)) as db:
            db.execute("INSERT INTO tasks (id, title, body, branch_name, status, assignee, block_kind) "
                       "VALUES (?, 'T', '', NULL, 'scheduled', ?, NULL)", (task_id, assignee))
            db.executemany("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'default', ?, 300)",
                           [(task_id, body) for body in comments])
            db.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, 200)",
                       (task_id, json.dumps({"reason": reason})))
            db.commit()

    def sweep(self, tasks: list, default_tasks: list) -> subprocess.CompletedProcess:
        return self.run_script(tasks, default_tasks=default_tasks, default_db=str(self.default_board))

    def statuses(self) -> dict:
        state = json.loads(self.state_path.read_text())
        return {t["id"]: (t["status"], t["assignee"]) for t in state["tasks"] + state["default_tasks"]}


class LoginReleaseTest(ParkedHarness):
    def login(self, seconds_left: float | None) -> None:
        gh = self.home / "gh"
        gh.mkdir(exist_ok=True)
        (gh / "token-expiry.json").unlink(missing_ok=True)
        if seconds_left is not None:
            (gh / "token-expiry.json").write_text(json.dumps({"expires_at": time.time() + seconds_left}))

    def test_a_fresh_login_releases_a_default_card_ready_and_assigned_and_a_triage_card_unassigned(self) -> None:
        self.login(15 * 60 + 30)
        self.park(self.default_board, "t_default")
        self.park(self.board, "t_triage")
        busy = [task("t_a", "running", "claude-worker"), task("t_b", "running", "claude-worker")]

        proc = self.sweep([task("t_triage", "scheduled", "claude-worker"), *busy], [task("t_default", "scheduled", "claude-worker")])

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "released t_default\nreleased t_triage\n", ""))
        self.assertEqual(self.statuses()["t_default"], ("ready", "claude-worker"))
        self.assertEqual(self.statuses()["t_triage"], ("ready", None))

    def test_a_released_triage_card_is_assigned_within_the_cap_in_the_same_run(self) -> None:
        self.login(20 * 60)
        self.park(self.board, "t_triage")

        proc = self.sweep([task("t_triage", "scheduled", "claude-worker")], [])

        self.assertEqual(proc.stdout, "released t_triage\nassigned t_triage\n")
        self.assertEqual(self.statuses()["t_triage"], ("ready", "claude-worker"))

    def test_a_login_under_15_minutes_missing_or_unreadable_releases_nothing_and_reads_no_board(self) -> None:
        self.park(self.default_board, "t_default")
        for name, setup in (
            ("stale", lambda: self.login(14 * 60 + 50)),
            ("expired", lambda: self.login(-60)),
            ("missing", lambda: self.login(None)),
            ("unreadable", lambda: (self.login(None), (self.home / "gh" / "token-expiry.json").write_text("{"))),
        ):
            with self.subTest(name):
                setup()

                proc = self.sweep([], [task("t_default", "scheduled", "claude-worker")])

                self.assertEqual((proc.returncode, proc.stdout), (0, ""))
                self.assertEqual(self.statuses()["t_default"], ("scheduled", "claude-worker"))

    def test_a_card_a_person_stopped_or_unassigned_after_the_park_stays_parked(self) -> None:
        self.login(30 * 60)
        self.park(self.default_board, "t_stopped", assignee=None, comments=("stopped",))
        self.park(self.default_board, "t_unassigned", assignee=None)
        self.park(self.default_board, "t_stopped_assigned", comments=("stopped",))

        proc = self.sweep([], [task(name, "scheduled", assignee) for name, assignee in
                               (("t_stopped", None), ("t_unassigned", None), ("t_stopped_assigned", "claude-worker"))])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual({status for status, _ in self.statuses().values()}, {"scheduled"})

    def test_a_stop_before_the_park_does_not_hold_the_card(self) -> None:
        self.login(30 * 60)
        self.park(self.default_board, "t_default")
        with closing(sqlite3.connect(self.default_board)) as db:
            db.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t_default', 'default', 'stopped', 100)")
            db.commit()

        proc = self.sweep([], [task("t_default", "scheduled", "claude-worker")])

        self.assertEqual(proc.stdout, "released t_default\n")

    def test_a_reply_never_releases_a_login_park(self) -> None:
        self.login(None)
        self.park(self.board, "t_login", comments=("Linear reply: any news?",))

        proc = self.sweep([task("t_login", "scheduled", "claude-worker")], [])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.statuses()["t_login"], ("scheduled", "claude-worker"))

    def test_a_fresh_login_never_releases_a_question(self) -> None:
        self.login(30 * 60)
        self.park(self.default_board, "t_question", reason="Needs input: which screen?\n\ncomments_seen: 0",
                  comments=("Linear reply: the settings screen",))

        proc = self.sweep([], [task("t_question", "scheduled", "claude-worker")])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.statuses()["t_question"], ("scheduled", "claude-worker"))

    def test_a_triage_card_whose_unblock_fails_is_assigned_again_so_a_later_sweep_releases_it(self) -> None:
        self.login(30 * 60)
        self.park(self.board, "t_triage")

        proc = self.run_script([task("t_triage", "scheduled", "claude-worker")], unblock_fails=True)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("releasing t_triage for the gh login failed", proc.stderr)
        self.assertEqual(self.statuses()["t_triage"], ("scheduled", "claude-worker"))

    def test_a_board_that_cannot_be_read_does_not_stop_the_others_and_exits_1(self) -> None:
        self.login(30 * 60)
        self.park(self.board, "t_triage")
        self.default_board.write_bytes(b"not a database")

        proc = self.sweep([task("t_triage", "scheduled", "claude-worker")], [])

        self.assertEqual((proc.returncode, proc.stdout), (1, "released t_triage\nassigned t_triage\n"))
        self.assertIn("cannot read the board", proc.stderr)


if __name__ == "__main__":
    unittest.main()


DEPLOY_PARKED = "Waiting for a factory deploy: the profile scripts in /w differ from the deployed commit 0123456789ab at /src"


class DeployReleaseTest(ParkedHarness):
    def setUp(self) -> None:
        super().setUp()
        root = self.tmp / "root"
        self.home = root / "profiles" / "claude-worker"
        self.source = self.tmp / "source"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        for path in (REPO / "plugins" / "blocker-order").glob("*.py"):
            for target in (self.source / "plugins" / "blocker-order", self.home / "plugins" / "blocker-order"):
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target / path.name)
        self.commit("TOOL = 1\n")
        self.deployed = git(self.source, "rev-parse", "HEAD")
        (self.home / "scripts").mkdir()
        (self.home / "scripts" / "tool.py").write_text("TOOL = 1\n")
        (self.home / "distribution.yaml").write_text(f"name: claude-worker\nsource: {self.source}/profiles/claude-worker\n")
        self.state_file = root / "auto-deploy.json"
        self.record(self.deployed)

    def commit(self, tool: str) -> str:
        path = self.source / "profiles" / "claude-worker" / "scripts" / "tool.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tool)
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "change")
        return git(self.source, "rev-parse", "HEAD")

    def record(self, deployed: str) -> None:
        self.state_file.write_text(json.dumps({"deployed": deployed, "failed": None, "blocked": None}))

    def park(self, board: Path, task_id: str, reason: str = DEPLOY_PARKED, **fields) -> None:
        super().park(board, task_id, reason, **fields)

    def test_an_install_that_matches_the_deployed_commit_releases_on_every_board(self) -> None:
        self.park(self.default_board, "t_default")
        self.park(self.board, "t_triage")

        proc = self.sweep([task("t_triage", "scheduled", "claude-worker")], [task("t_default", "scheduled", "claude-worker")])

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(sorted(proc.stdout.splitlines()), ["assigned t_triage", "released t_default", "released t_triage"])
        self.assertEqual(self.statuses()["t_default"], ("ready", "claude-worker"))
        self.assertEqual(self.statuses()["t_triage"], ("ready", "claude-worker"))
        calls = [json.loads(line) for line in Path(f"{self.state_path}.log").read_text().splitlines()]
        self.assertIn(["kanban", "--board", "triage", "assign", "t_triage", "none"], calls)

    def test_a_checkout_ahead_of_the_deployed_commit_still_releases(self) -> None:
        self.commit("TOOL = 2\n")
        self.park(self.default_board, "t_default")

        proc = self.sweep([], [task("t_default", "scheduled", "claude-worker")])

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "released t_default\n", ""))

    def test_an_install_that_differs_from_the_deployed_commit_releases_nothing(self) -> None:
        self.record(self.commit("TOOL = 2\n"))
        self.park(self.default_board, "t_default")
        self.park(self.board, "t_triage")

        proc = self.sweep([task("t_triage", "scheduled", "claude-worker")], [task("t_default", "scheduled", "claude-worker")])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual({status for status, _ in self.statuses().values()}, {"scheduled"})

    def test_a_failed_or_blocked_deploy_keeps_the_card_parked_until_a_deploy_installs_the_commit(self) -> None:
        for field in ("failed", "blocked"):
            with self.subTest(field):
                self.tmp_board = self.tmp / f"{field}.db"
                with closing(sqlite3.connect(self.tmp_board)) as db:
                    db.execute(TASKS)
                    db.executescript(COMMENTS_AND_EVENTS)
                commit = self.commit(f"TOOL = {field}\n")
                self.record(commit)
                self.state_file.write_text(json.dumps({**json.loads(self.state_file.read_text()), field: "a" * 40}))
                self.park(self.tmp_board, "t_default")
                tasks = [task("t_default", "scheduled", "claude-worker")]

                held = self.run_script([], default_tasks=tasks, default_db=str(self.tmp_board))

                self.assertEqual((held.returncode, held.stdout), (0, ""))

                (self.home / "scripts" / "tool.py").write_text(f"TOOL = {field}\n")
                self.record(commit)

                released = self.run_script([], default_tasks=tasks, default_db=str(self.tmp_board))

                self.assertEqual((released.returncode, released.stdout), (0, "released t_default\n"))

    def test_a_deployed_commit_the_checkout_lacks_releases_nothing(self) -> None:
        self.record("f" * 40)
        self.park(self.default_board, "t_default")

        proc = self.sweep([], [task("t_default", "scheduled", "claude-worker")])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.statuses()["t_default"], ("scheduled", "claude-worker"))

    def test_a_card_a_person_stopped_or_unassigned_after_the_park_stays_parked(self) -> None:
        self.park(self.default_board, "t_stopped", comments=("stopped",))
        self.park(self.default_board, "t_unassigned", assignee=None)

        proc = self.sweep([], [task("t_stopped", "scheduled", "claude-worker"), task("t_unassigned", "scheduled", None)])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual({status for status, _ in self.statuses().values()}, {"scheduled"})

    def test_a_reply_never_releases_a_deploy_park(self) -> None:
        self.record(self.commit("TOOL = 2\n"))
        self.park(self.board, "t_deploy", comments=("Linear reply: any news?",))

        proc = self.sweep([task("t_deploy", "scheduled", "claude-worker")], [])

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.statuses()["t_deploy"], ("scheduled", "claude-worker"))



NO_BRIEF_PARK = "Waiting for triage: Linear issue ABC-123 has no Document titled `ABC-123 Agent brief`"
IMPLEMENTATION_BODY = (
    "```text\nlinear_issue_id: issue-uuid\nlinear_issue_identifier: ABC-123\n"
    "linear_issue_url: https://linear.app/example/issue/ABC-123\nlinear_agent_session_id: session-1\n"
    "repo: example-org/app\nbranch: abc-123-fix\n```\n"
)
TRIAGE_BODY = (
    "```text\ncard: triage\nlinear_issue_id: issue-uuid\nlinear_issue_identifier: ABC-123\n"
    "linear_issue_url: https://linear.app/example/issue/ABC-123\nlinear_agent_session_id: session-1\n"
    "repo: example-org/app\n```\n"
)
BRIEF_SETTINGS = {"triage": {"max_running": 2}, "repositories": [
    {"full_name": "example-org/app", "path": "/repos/app", "routing": "synced_github"},
]}
QUEUED_NOTE = "No ABC-123 Agent brief yet, so triage runs first. Queued for triage as t_triage_1."


class BriefHarness(ParkedHarness):
    """A card parked for its Agent brief, the real Linear client on a fake Linear, and the factory checkout."""

    def setUp(self) -> None:
        super().setUp()
        plugin = self.home / "plugins" / "linear-agent-session"
        plugin.mkdir()
        (plugin / "linear.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "tests" / "staged_linear.py")
        (plugin / "broker_client.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "broker_client.py")
        self.linear({"accepted": ["linear-token"], "priority": 3})
        broker = FakeBroker(self.home / "factory-uploads.sock", linear_token=[granted("linear-token")])
        self.addCleanup(broker.close)
        (self.home / "distribution.yaml").write_text(f"name: claude-worker\nsource: {REPO}/profiles/claude-worker\n")
        self.park(self.default_board, "t_impl", NO_BRIEF_PARK)
        self.impl = {**task("t_impl", "scheduled", "claude-worker"), "body": IMPLEMENTATION_BODY}

    def linear(self, state: dict) -> None:
        path = self.home / "linear-fake.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps({**current, **state}))

    def activities(self) -> list:
        log = self.home / "linear-fake.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [entry["activity"] for entry in map(json.loads, lines) if "activity" in entry]


class BriefDispatchTest(BriefHarness):
    def test_dispatch_triage_queues_the_triage_card_and_assigns_it_in_the_same_run(self) -> None:
        self.linear({"documents": []})

        proc = self.run_script([], settings=BRIEF_SETTINGS, default_tasks=[self.impl], default_db=str(self.default_board))

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "queued t_triage_1 for t_impl\nassigned t_triage_1\n", ""))
        self.assertEqual(self.statuses()["t_impl"], ("scheduled", "claude-worker"))
        self.assertEqual([activity["content"]["body"] for activity in self.activities()], [QUEUED_NOTE])

    def test_a_failed_linear_read_is_reported_and_exits_1(self) -> None:
        self.linear({"documents_error": "Entity not found"})

        proc = self.run_script([], settings=BRIEF_SETTINGS, default_tasks=[self.impl], default_db=str(self.default_board))

        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertIn("releasing t_impl for an Agent brief failed: reading Linear issue ABC-123 failed: Entity not found",
                      proc.stderr)
        self.assertEqual(self.statuses()["t_impl"], ("scheduled", "claude-worker"))


class PullRequestSweepDispatchTest(ParkedHarness):
    """`dispatch-triage` runs `card_transition.sweep_pull_request`, here replaced by a fake that reports `swept`."""

    DRIVER = (
        "import importlib.machinery, importlib.util, sys, card_transition\n"
        "card_transition.sweep_pull_request = lambda: {swept!r}\n"
        "loader = importlib.machinery.SourceFileLoader('dispatch_triage', 'dispatch-triage')\n"
        "module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))\n"
        "loader.exec_module(module)\n"
        "sys.exit(module.main())\n"
    )

    def dispatch(self, swept: dict) -> subprocess.CompletedProcess:
        self.state_path.write_text(json.dumps({
            "default_tasks": [], "tasks": [], "settings": {"triage": {"max_running": 2}},
            "db_path": str(self.board), "default_db": str(self.default_board),
        }))
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path),
               "HERMES_HOME": str(self.home), "HOST_HOLD_LOADAVG": "0,0,0", "HOST_HOLD_CPUS": "8"}
        return subprocess.run([sys.executable, "-c", self.DRIVER.format(swept=swept)], cwd=SCRIPTS, env=env,
                              capture_output=True, text=True)

    def test_a_failed_pull_request_read_exits_1_and_names_the_card(self) -> None:
        proc = self.dispatch({"released": [], "failed": {"t_waiting": "gh pr view o/r#7 failed: gh: HTTP 502"}})

        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertIn("releasing t_waiting for a GitHub pull request failed: gh pr view o/r#7 failed: gh: HTTP 502",
                      proc.stderr)

    def test_a_released_card_is_reported(self) -> None:
        proc = self.dispatch({"released": ["t_waiting"], "failed": {}})

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "released t_waiting\n", ""))


HOST_LOAD_PARKED = "Waiting for the host load to fall: load average 20 (1 min) against a threshold of 12"
OLD = "2020-01-01T00:00:00Z"


class HostLoadTest(ParkedHarness):
    def holds_file(self) -> dict:
        return json.loads((self.home.parent.parent / "dispatch-holds.json").read_text())

    def write_holds(self, holds: dict, applied: bool) -> None:
        (self.home.parent.parent / "dispatch-holds.json").write_text(json.dumps({"holds": holds, "applied": applied}))

    def config_calls(self) -> list:
        log = Path(f"{self.state_path}.log")
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call[1:] for call in calls if call[:1] == ["config"] and call[1] in ("set", "unset")]


    def entry(self, until: str = "2999-01-01T00:00:00Z") -> dict:
        return {"reason": "load average 20", "until": until}

    def test_a_load_over_the_threshold_assigns_no_triage_card_and_holds_dispatch(self) -> None:
        proc = self.run_script([task("t_waiting", "ready")], _env={"HOST_HOLD_LOADAVG": "13,2,2"})

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertIsNone(self.assignees()["t_waiting"])
        self.assertEqual(self.holds_file()["applied"], True)
        reason = self.holds_file()["holds"]["host_load"]["reason"]
        self.assertEqual(reason, "load average 13 (1 min) and 2 (5 min) against a threshold of 12")
        self.assertEqual(self.config_calls(), [["set", "kanban.dispatch_profiles", "[]"]])

    def test_a_key_a_person_set_is_neither_overwritten_nor_unset_by_a_load_hold(self) -> None:
        proc = self.run_script([task("t_waiting", "ready")], _env={"HOST_HOLD_LOADAVG": "13,2,2"}, dispatch_profiles=["claude-worker"])

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertIsNone(self.assignees()["t_waiting"])
        self.assertIn("host_load", self.holds_file()["holds"])
        self.assertFalse(self.holds_file()["applied"])

        proc = self.run_script([task("t_waiting", "ready")], dispatch_profiles=["claude-worker"])

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.holds_file(), {"holds": {}, "applied": False})
        self.assertEqual(self.config_calls(), [])
        self.assertEqual(json.loads(self.state_path.read_text())["dispatch_profiles"], ["claude-worker"])

    def test_the_five_minute_average_alone_holds(self) -> None:
        self.run_script([], _env={"HOST_HOLD_LOADAVG": "1,12.5,2"})

        self.assertIn("host_load", self.holds_file()["holds"])

    def test_a_load_at_or_below_the_threshold_for_both_averages_clears_the_hold_and_releases_parked_cards_in_the_same_run(self) -> None:
        self.write_holds({"host_load": self.entry()}, applied=True)
        self.park(self.default_board, "t_default", HOST_LOAD_PARKED)
        self.park(self.board, "t_triage", HOST_LOAD_PARKED)

        proc = self.sweep(
            [task("t_triage", "scheduled", "claude-worker")], [task("t_default", "scheduled", "claude-worker")],
        )

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(sorted(proc.stdout.splitlines()), ["assigned t_triage", "released t_default", "released t_triage"])
        self.assertEqual(self.statuses()["t_default"], ("ready", "claude-worker"))
        self.assertEqual(self.holds_file(), {"holds": {}, "applied": False})
        self.assertEqual(self.config_calls(), [["unset", "kanban.dispatch_profiles"]])

    def test_a_parked_card_stays_parked_while_the_load_holds(self) -> None:
        self.park(self.default_board, "t_default", HOST_LOAD_PARKED)

        proc = self.sweep([], [task("t_default", "scheduled", "claude-worker")], )
        self.assertEqual(proc.stdout, "released t_default\n")

        self.park(self.default_board, "t_other", HOST_LOAD_PARKED)
        held = self.run_script(
            [], default_tasks=[task("t_other", "scheduled", "claude-worker")], default_db=str(self.default_board),
            _env={"HOST_HOLD_LOADAVG": "30,30,30"},
        )

        self.assertEqual((held.returncode, held.stdout), (0, ""))
        self.assertEqual(self.statuses()["t_other"], ("scheduled", "claude-worker"))

    def test_an_entry_past_its_until_is_removed_by_the_next_run(self) -> None:
        self.write_holds({"auto-deploy": self.entry(OLD)}, applied=True)

        proc = self.run_script([])

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.holds_file(), {"holds": {}, "applied": False})
        self.assertEqual(self.config_calls(), [["unset", "kanban.dispatch_profiles"]])

    def test_a_key_a_person_set_stays_set_with_an_empty_list(self) -> None:
        self.write_holds({}, applied=False)

        proc = self.run_script([task("t_waiting", "ready")])

        self.assertEqual((proc.returncode, proc.stdout), (0, "assigned t_waiting\n"))
        self.assertEqual(self.config_calls(), [])

    def test_an_unreadable_load_holds_nothing_and_assigns(self) -> None:
        proc = self.run_script([task("t_waiting", "ready")], _env={"HOST_HOLD_LOADAVG": "error"})

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "assigned t_waiting\n", ""))
        self.assertFalse((self.home.parent.parent / "dispatch-holds.json").exists())
        log = (self.home.parent.parent / "dispatch-holds.log").read_text()
        self.assertEqual(json.loads(log)["event"], "load-unreadable")

    def test_a_threshold_setting_overrides_the_default(self) -> None:
        settings = {"triage": {"max_running": 2}, "claude_session": {"load_hold_threshold": 40}}

        proc = self.run_script([task("t_waiting", "ready")], settings, _env={"HOST_HOLD_LOADAVG": "30,30,30"})

        self.assertEqual(proc.stdout, "assigned t_waiting\n")

    def test_an_invalid_threshold_still_expires_and_applies_skips_the_load_step_and_exits_nonzero(self) -> None:
        for bad in (0, -1, "x", True):
            with self.subTest(bad=bad):
                self.write_holds({"auto-deploy": self.entry(OLD), "usage_limit": self.entry()}, applied=False)
                settings = {"triage": {"max_running": 2}, "claude_session": {"load_hold_threshold": bad}}

                proc = self.run_script([], settings, _env={"HOST_HOLD_LOADAVG": "99,99,99"})

                self.assertEqual(proc.returncode, 2)
                self.assertIn(f"claude_session.load_hold_threshold {bad!r} must be a positive number", proc.stderr)
                self.assertEqual(sorted(self.holds_file()["holds"]), ["usage_limit"])
                self.assertEqual(self.holds_file()["applied"], True)
