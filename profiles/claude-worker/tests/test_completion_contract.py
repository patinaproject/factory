from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from tests.support import SCRIPTS, with_interpreter

sys.path.insert(0, str(SCRIPTS))
import completion_contract  # noqa: E402
from completion_contract import Write  # noqa: E402

URL = "https://github.com/example-org/app/pull/12"
# Hermes' tables, cut to the columns a write could touch, from `hermes_cli/kanban_db_connect.py`.
SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT, branch_name TEXT,
                    completion_contract TEXT, current_run_id INTEGER, result TEXT);
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT, body TEXT, created_at INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT,
                          created_at INTEGER);
"""


def rows(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as board:
        return {table: board.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("tasks", "task_comments", "task_events")}


class ReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "kanban.db"
        with closing(sqlite3.connect(self.path)) as board:
            board.execute("PRAGMA journal_mode = WAL")
            board.executescript(SCHEMA)
            for task_id in ("t_merged", "t_other"):
                board.execute(
                    "INSERT INTO tasks VALUES (?, 'title', 'body', 'review', 'claude-worker', 'pat-1', ?, 3, NULL)",
                    (task_id, URL),
                )
                board.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'a', 'c', 1)", (task_id,))
                board.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'created', '{}', 1)", (task_id,))
            board.commit()

    def given(self, status: str = "review", contract: str = URL) -> None:
        with closing(sqlite3.connect(self.path)) as board:
            board.execute("UPDATE tasks SET status = ?, completion_contract = ? WHERE id = 't_merged'", (status, contract))
            board.commit()

    def release(self, url: str = URL) -> tuple[Write, str | None]:
        return completion_contract.release(str(self.path), "t_merged", url)

    def test_the_write_changes_only_the_contract_of_the_one_card(self):
        before = rows(self.path)
        self.assertEqual(self.release(), (Write.WRITTEN, URL))
        after = rows(self.path)
        changed = {table: [(old, new) for old, new in zip(before[table], after[table]) if old != new] for table in before}
        self.assertEqual(changed, {
            "tasks": [(("t_merged", "title", "body", "review", "claude-worker", "pat-1", URL, 3, None),
                       ("t_merged", "title", "body", "review", "claude-worker", "pat-1", "local-only", 3, None))],
            "task_comments": [], "task_events": [],
        })
        self.assertEqual({table: len(found) for table, found in after.items()}, {table: len(found) for table, found in before.items()})

    def test_a_contract_that_names_this_pull_request_or_its_repository_is_written(self):
        for contract in (URL, "https://github.com/Example-Org/APP/pull/12", "example-org/app", "Example-Org/App"):
            with self.subTest(contract):
                self.given(contract=contract)
                self.assertEqual(self.release("https://github.com/example-org/app/pull/12/"), (Write.WRITTEN, contract))

    def test_a_contract_that_names_another_pull_request_or_repository_is_left_alone(self):
        for contract in ("https://github.com/example-org/app/pull/120", "https://github.com/example-org/other/pull/12",
                         "example-org/other"):
            with self.subTest(contract):
                self.given(contract=contract)
                self.assertEqual(self.release(), (Write.OTHER, contract))
                self.assertEqual(rows(self.path)["tasks"][0][6], contract)

    def test_a_local_only_or_empty_contract_writes_nothing(self):
        for contract in ("local-only", ""):
            with self.subTest(contract):
                self.given(contract=contract)
                before = rows(self.path)
                self.assertEqual(self.release(), (Write.ALREADY, contract))
                self.assertEqual(rows(self.path), before)

    def test_a_done_or_archived_card_is_never_written(self):
        for status in ("done", "archived"):
            with self.subTest(status):
                self.given(status=status)
                self.assertEqual(self.release(), (Write.FINISHED, URL))
                self.assertEqual(rows(self.path)["tasks"][0][6], URL)

    def test_a_card_that_is_not_on_the_board_is_refused(self):
        with self.assertRaisesRegex(completion_contract.Failed, "^card t_none is not on the board "):
            completion_contract.release(str(self.path), "t_none", URL)

    def test_the_write_waits_for_a_writer_that_holds_the_board(self):
        holder = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE tasks SET status = 'ready' WHERE id = 't_merged'")
        threading.Timer(0.5, lambda: holder.execute("COMMIT")).start()
        started = time.monotonic()
        self.assertEqual(self.release(), (Write.WRITTEN, URL))
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertEqual(rows(self.path)["tasks"][0][3:7:3], ("ready", "local-only"))

    def test_the_busy_timeout_is_never_below_hermes_default(self):
        for configured, expected in ((None, 120_000), ("5000", 120_000), ("300000", 300_000), ("soon", 120_000)):
            with self.subTest(configured):
                env = {} if configured is None else {"HERMES_KANBAN_BUSY_TIMEOUT_MS": configured}
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(completion_contract.busy_timeout_ms(), expected)


# Runs in Hermes' own interpreter: a refused completion, the write, and the completion Hermes then accepts.
HERMES_RUN = """
import json, sqlite3, sys
from pathlib import Path
tree, scripts, path = sys.argv[1:4]
sys.path[:0] = [tree, scripts]
from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import connect
import completion_contract
url = "https://github.com/example-org/app/pull/12"
def snapshot():
    board = sqlite3.connect(path)
    found = {table: board.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
             for table in ("tasks", "task_comments", "task_events")}
    board.close()
    return found
conn = connect(Path(path))
merged = kanban_db.create_task(conn, title="merged", completion_contract="example-org/app")
other = kanban_db.create_task(conn, title="other", completion_contract="example-org/app")
kanban_db.add_comment(conn, merged, "tester", "a comment")
report = {}
report["refused"] = kanban_db.complete_task(conn, merged, summary="merged", metadata={"published_pr": url})
row = lambda task: conn.execute("SELECT status, completion_contract FROM tasks WHERE id=?", (task,)).fetchone()
report["after_refusal"] = list(row(merged))
conn.close()
before = snapshot()
report["write"] = completion_contract.release(path, merged, url)[0].value
after = snapshot()
report["changed"] = {table: [[list(old), list(new)] for old, new in zip(before[table], after[table]) if old != new]
                     for table in before}
report["counts"] = [{t: len(v) for t, v in before.items()}, {t: len(v) for t, v in after.items()}]
conn = connect(Path(path))
report["completed"] = kanban_db.complete_task(conn, merged, summary="merged", metadata={"published_pr": url})
report["final"] = [list(row(merged)), list(row(other))]
report["events"] = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (merged,))]
report["ids"] = [merged, other]
print(json.dumps(report))
"""

FAILING_GH = """#!/usr/bin/env python3
import sys
sys.exit("gh: HTTP 502")
"""


def hermes_python() -> tuple[Path, Path] | tuple[None, str]:
    """Hermes' interpreter and source tree on this machine, or None and the reason they cannot be used."""
    root = Path(os.environ.get("FACTORY_HERMES_ROOT") or Path.home() / ".hermes")
    tree = root / "hermes-agent"
    if not (tree / "hermes_cli" / "kanban_db.py").is_file():
        return None, f"no Hermes source tree at {tree}"
    for facts in sorted((root / "installs").glob("*/facts.json")):
        try:
            python = Path(json.loads(facts.read_text())["packages"]["venv"]["environment"]) / "bin" / "python"
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if python.is_file():
            return python, tree
    return None, f"no Hermes interpreter recorded under {root / 'installs'}"


class HermesCompleteTest(unittest.TestCase):
    """The installed Hermes refuses a repository contract's completion, and accepts it after the write."""

    def test_after_the_write_hermes_completes_a_card_its_acceptance_refused(self):
        python, tree = hermes_python()
        if python is None:
            self.skipTest(f"Hermes is not installed: {tree}")
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / "bin").mkdir()
        (work / "bin" / "gh").write_text(with_interpreter(FAILING_GH))
        os.chmod(work / "bin" / "gh", 0o755)
        (work / "home").mkdir()
        env = {"HOME": str(work), "HERMES_HOME": str(work / "home"), "PATH": f"{work / 'bin'}:/usr/bin:/bin"}
        proc = subprocess.run(
            [str(python), "-c", HERMES_RUN, str(tree), str(SCRIPTS), str(work / "kanban.db")],
            capture_output=True, text=True, env=env, cwd=work, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout.strip().splitlines()[-1])
        merged, other = report["ids"]
        self.assertEqual((report["refused"], report["after_refusal"]), (False, ["ready", URL]))
        self.assertEqual(report["write"], "written")
        self.assertEqual(report["counts"][0], report["counts"][1])
        self.assertEqual([table for table, changes in report["changed"].items() if changes], ["tasks"])
        [[old, new]] = report["changed"]["tasks"]
        self.assertEqual(old[0], merged)
        self.assertEqual([(a, b) for a, b in zip(old, new) if a != b], [(URL, "local-only")])
        self.assertEqual(report["completed"], True)
        self.assertEqual(report["final"], [["done", "local-only"], ["ready", "example-org/app"]])
        self.assertEqual(report["events"][-1], "completed")


if __name__ == "__main__":
    unittest.main()
