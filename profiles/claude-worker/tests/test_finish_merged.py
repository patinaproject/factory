from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import REPO, SCRIPTS, with_interpreter

# A board whose acceptance refuses every contract but `local-only`, with its cards in a real database.
FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sqlite3, sys
state = json.load(open(os.environ["FAKE_STATE"]))
argv = sys.argv[1:]
assert argv[0] == "kanban", argv
argv = argv[1:]
board = "default"
if argv[0] == "--board":
    board, argv = argv[1], argv[2:]
if argv[:2] == ["boards", "list"]:
    print(json.dumps([{"slug": "default", "db_path": state["db"]}, {"slug": "triage", "db_path": "/nowhere"}]))
    sys.exit()
verb, task_id = argv[0], argv[1]
db = sqlite3.connect(state["db"])
status, contract = db.execute("SELECT status, completion_contract FROM tasks WHERE id = ?", (task_id,)).fetchone()
log = state["log"]
open(log, "a").write(json.dumps([board, *argv]) + "\\n")
comments = [json.loads(line)[3] for line in open(log) if json.loads(line)[1:3] == ["comment", task_id]]
if verb == "show":
    print(json.dumps({"task": {"id": task_id, "status": status}, "events": [],
                      "comments": [{"body": body, "created_at": 1} for body in comments]}))
elif verb == "complete":
    if status not in ("ready", "blocked", "review", "running") or contract != "local-only":
        sys.exit(f"cannot complete {task_id} (unknown id or terminal state)")
    db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
elif verb != "comment":
    sys.exit(f"unexpected {argv}")
db.commit()
"""

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_STATE"]))
args = sys.argv[1:]
open(state["gh_log"], "a").write(json.dumps(args) + "\\n")
if args[:2] == ["api", "repos/example-org/app/rules/branches/main?per_page=100"]:
    print(json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Validate"}]}}]))
    sys.exit()
assert args[:2] == ["pr", "view"], args
target = args[2] if "-R" not in args else f"{args[args.index('-R') + 1]}:{args[2]}"
pull = state["pulls"].get(target)
if pull == "error":
    sys.exit("gh: HTTP 502")
if pull is None:
    sys.exit(f'no pull requests found for branch "{args[2]}"')
print(json.dumps({"state": pull["state"], "url": pull["url"], "headRefOid": "abc", "baseRefName": "main",
                  "statusCheckRollup": [{"__typename": "CheckRun", "name": "Validate", "workflowName": "PR",
                                         "status": "COMPLETED", "conclusion": "FAILURE", "startedAt": "2026-10-16T05:00:00Z"}]}))
"""


def pull(number: int, state: str = "MERGED") -> dict:
    return {"state": state, "url": f"https://github.com/example-org/app/pull/{number}"}


CARDS = [
    ("t_bound", "review", "https://github.com/example-org/app/pull/12", "b12", ""),
    ("t_repo", "blocked", "example-org/app", "b13", ""),
    ("t_retry", "ready", "local-only", "b14", "repo: example-org/app"),
    ("t_open", "review", "example-org/app", "b15", ""),
    ("t_no_pull", "ready", "example-org/app", "b16", ""),
    ("t_done", "done", "example-org/app", "b12", ""),
    ("t_no_branch", "ready", "example-org/app", None, ""),
    ("t_local_without_repo", "ready", "local-only", "b18", ""),
    ("t_unreadable", "review", "example-org/app", "b17", ""),
]
PULLS = {
    "https://github.com/example-org/app/pull/12": pull(12),
    "example-org/app:b13": pull(13),
    "https://github.com/example-org/app/pull/13": pull(13),
    "example-org/app:b14": pull(14),
    "https://github.com/example-org/app/pull/14": pull(14),
    "example-org/app:b15": pull(15, "OPEN"),
    "example-org/app:b17": "error",
}


class FinishMergedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "bin").mkdir()
        for name, script in (("hermes", FAKE_HERMES), ("gh", FAKE_GH)):
            (self.dir / "bin" / name).write_text(with_interpreter(script))
            os.chmod(self.dir / "bin" / name, 0o755)
        self.db = self.dir / "kanban.db"
        with closing(sqlite3.connect(self.db)) as board:
            board.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, branch_name TEXT, status TEXT, "
                          "block_kind TEXT, completion_contract TEXT)")
            board.executemany("INSERT INTO tasks VALUES (?, 'title', ?, ?, ?, NULL, ?)",
                              [(task, body, branch, status, contract) for task, status, contract, branch, body in CARDS])
            board.commit()
        self.home = self.dir / "home"
        (self.home / "plugins").mkdir(parents=True)
        (self.home / "plugins" / "blocker-order").symlink_to(REPO / "plugins" / "blocker-order")
        self.log, self.gh_log = self.dir / "hermes.log", self.dir / "gh.log"
        self.log.touch()
        self.gh_log.touch()
        state = {"db": str(self.db), "log": str(self.log), "gh_log": str(self.gh_log), "pulls": PULLS}
        (self.dir / "state.json").write_text(json.dumps(state))

    def run_script(self, **env) -> subprocess.CompletedProcess:
        env = {**os.environ, "HERMES_HOME": str(self.home), "HERMES_BIN": str(self.dir / "bin" / "hermes"),
               "FAKE_STATE": str(self.dir / "state.json"), "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", **env}
        return subprocess.run([sys.executable, str(SCRIPTS / "finish-merged")], capture_output=True, text=True, env=env)

    def statuses(self) -> dict:
        with closing(sqlite3.connect(self.db)) as board:
            return {task: (status, contract) for task, status, contract in
                    board.execute("SELECT id, status, completion_contract FROM tasks")}

    def test_every_card_whose_pull_request_merged_ends_done_and_the_others_are_left_alone(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, (
            "t_bound https://github.com/example-org/app/pull/12 completed\n"
            "t_repo https://github.com/example-org/app/pull/13 completed\n"
            "t_retry https://github.com/example-org/app/pull/14 completed\n"
        ))
        self.assertEqual(proc.stderr, "finish-merged: t_unreadable: gh pr view failed: gh: HTTP 502\n")
        self.assertEqual(self.statuses(), {
            "t_bound": ("done", "local-only"), "t_repo": ("done", "local-only"), "t_retry": ("done", "local-only"),
            "t_open": ("review", "example-org/app"), "t_no_pull": ("ready", "example-org/app"),
            "t_done": ("done", "example-org/app"), "t_no_branch": ("ready", "example-org/app"),
            "t_local_without_repo": ("ready", "local-only"), "t_unreadable": ("review", "example-org/app"),
        })
        comments = [json.loads(line) for line in self.log.read_text().splitlines() if json.loads(line)[1] == "comment"]
        self.assertEqual(comments, [
            ["default", "comment", task, f"Merged past required checks: example-org/app#{number}: Validate failed or "
             "had no run on the merged head."] for task, number in (("t_bound", 12), ("t_repo", 13), ("t_retry", 14))
        ])

    def test_a_second_run_changes_nothing_and_comments_nothing(self):
        self.run_script()
        before = self.log.read_text()
        proc = self.run_script()
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.log.read_text()[len(before):].count('"comment"'), 0)

    def test_a_done_card_is_never_read_from_github(self):
        self.run_script()
        reads = [json.loads(line)[2] for line in self.gh_log.read_text().splitlines() if json.loads(line)[:2] == ["pr", "view"]]
        self.assertNotIn("t_done", reads)
        self.assertEqual(reads.count("b12"), 0)

    def test_without_hermes_home_it_refuses(self):
        proc = self.run_script(HERMES_HOME="")
        self.assertEqual((proc.returncode, proc.stderr), (
            2, "finish-merged: HERMES_HOME is not set; point it at the claude-worker profile home\n",
        ))


if __name__ == "__main__":
    unittest.main()
