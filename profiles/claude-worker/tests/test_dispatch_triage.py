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

from tests.support import REPO, SCRIPTS, git, with_interpreter
from tests.test_kanban_board import COMMENTS_AND_EVENTS, TASKS

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_HERMES_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]
with open(path + ".log", "a") as log:
    log.write(json.dumps(argv) + "\\n")
if argv[:2] == ["config", "get"]:
    print(json.dumps(state["settings"]))
elif argv == ["kanban", "boards", "list", "--json"]:
    print(json.dumps([{"slug": "default", "db_path": state.get("default_db", "/nowhere")},
                      {"slug": "triage", "db_path": state["db_path"]}]))
elif argv[:2] == ["kanban", "--board"] and argv[3] in ("list", "assign", "unblock", "show"):
    tasks = state["tasks"] if argv[2] == "triage" else state["default_tasks"]
    if argv[3] == "list":
        print(json.dumps(tasks))
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
        self.home = self.tmp / "home"
        (self.home / "plugins").mkdir(parents=True)
        (self.home / "plugins" / "blocker-order").symlink_to(REPO / "plugins" / "blocker-order")
        self.board = self.tmp / "triage.db"
        with closing(sqlite3.connect(self.board)) as db:
            db.execute(TASKS)
            db.executescript(COMMENTS_AND_EVENTS)

    def run_script(self, tasks: list, settings: object = None, **state) -> subprocess.CompletedProcess:
        settings = {"triage": {"max_running": 2}} if settings is None else settings
        state = {"default_tasks": [], "tasks": tasks, "settings": settings, "db_path": str(self.board), **state}
        self.state_path.write_text(json.dumps(state))
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path),
               "HERMES_HOME": str(self.home)}
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
        gh = self.tmp / "home" / "gh"
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
            ("unreadable", lambda: (self.login(None), (self.tmp / "home" / "gh" / "token-expiry.json").write_text("{"))),
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
