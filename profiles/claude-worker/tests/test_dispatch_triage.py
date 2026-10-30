from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import REPO, SCRIPTS, with_interpreter
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
    print(json.dumps([{"slug": "default", "db_path": "/nowhere"}, {"slug": "triage", "db_path": state["db_path"]}]))
elif argv[:4] == ["kanban", "--board", "triage", "list"]:
    print(json.dumps(state["tasks"]))
elif argv[:4] == ["kanban", "--board", "triage", "assign"] and not state.get("assign_fails"):
    next(t for t in state["tasks"] if t["id"] == argv[4])["assignee"] = None if argv[5] == "none" else argv[5]
    json.dump(state, open(path, "w"))
elif argv[:4] == ["kanban", "--board", "triage", "unblock"]:
    card = next(t for t in state["tasks"] if t["id"] == argv[4])
    if card["status"] != "scheduled":
        sys.exit("cannot unblock " + argv[4])
    card["status"] = "ready"
    json.dump(state, open(path, "w"))
else:
    sys.exit("hermes: refused " + repr(argv))
"""


def task(task_id: str, status: str, assignee: str | None = None, priority: int = 0, created_at: int = 100) -> dict:
    return {"id": task_id, "status": status, "assignee": assignee, "priority": priority, "created_at": created_at}


class DispatchTriageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.hermes = self.tmp / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)
        self.state_path = self.tmp / "state.json"
        (self.tmp / "home" / "plugins").mkdir(parents=True)
        (self.tmp / "home" / "plugins" / "blocker-order").symlink_to(REPO / "plugins" / "blocker-order")
        self.board = self.tmp / "triage.db"
        with closing(sqlite3.connect(self.board)) as db:
            db.execute(TASKS)
            db.executescript(COMMENTS_AND_EVENTS)

    def run_script(self, tasks: list, settings: object = None, **state) -> subprocess.CompletedProcess:
        settings = {"triage": {"max_running": 2}} if settings is None else settings
        self.state_path.write_text(json.dumps({"tasks": tasks, "settings": settings, "db_path": str(self.board), **state}))
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path),
               "HERMES_HOME": str(self.tmp / "home")}
        return subprocess.run([sys.executable, str(SCRIPTS / "dispatch-triage")], env=env, capture_output=True, text=True)

    def assignees(self) -> dict:
        return {t["id"]: t["assignee"] for t in json.loads(self.state_path.read_text())["tasks"]}

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


if __name__ == "__main__":
    unittest.main()
