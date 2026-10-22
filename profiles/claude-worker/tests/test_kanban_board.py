from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import kanban_board  # noqa: E402

TASKS = "CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, branch_name TEXT, status TEXT, assignee TEXT)"


WRITER = """
import json, os, sqlite3, sys
board = sqlite3.connect(sys.argv[1])
board.execute("PRAGMA journal_mode = WAL")
board.execute(%r)
board.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, 'claude-worker')", json.loads(sys.argv[2]))
board.commit()
os._exit(0)
""" % TASKS


def wal_board(path: Path, rows: list[tuple]) -> None:
    """A WAL database whose rows sit in the -wal file, as when the writer that made them exited without closing."""
    subprocess.run([sys.executable, "-c", WRITER, str(path), json.dumps(rows)], check=True)


class KanbanBoardTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "kanban.db"

    def test_reading_a_wal_board_with_no_other_connection_changes_no_file(self) -> None:
        wal_board(self.path, [("t_a", "A", "", "a-work", "todo"), ("t_b", "B", "", "b-work", "archived")])
        wal = Path(f"{self.path}-wal")

        def contents() -> tuple:
            return tuple(file.read_bytes() if file.exists() else None for file in (self.path, wal))

        before = contents()
        self.assertGreater(len(before[1]), 0)

        cards = kanban_board.cards(str(self.path))

        self.assertEqual(cards, [{"id": "t_a", "title": "A", "body": "", "branch_name": "a-work", "status": "todo"}])
        self.assertEqual(contents(), before)

    def test_a_wal_board_with_no_wal_files_is_read_without_creating_them(self) -> None:
        with closing(sqlite3.connect(self.path)) as board:
            board.execute("PRAGMA journal_mode = WAL")
            board.execute(TASKS)
            board.execute("INSERT INTO tasks VALUES ('t_a', 'A', '', 'a-work', 'todo', 'claude-worker')")
            board.commit()
        for suffix in ("-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

        self.assertEqual([card["id"] for card in kanban_board.cards(str(self.path))], ["t_a"])
        self.assertEqual(sorted(file.name for file in self.path.parent.iterdir()), ["kanban.db"])

    def test_missing_columns_names_each_column_the_tasks_table_lacks(self) -> None:
        with sqlite3.connect(self.path) as board:
            board.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, status TEXT)")
        board.close()

        self.assertEqual(kanban_board.missing_columns(str(self.path)), ["branch_name"])

    def test_a_board_with_every_column_is_missing_none(self) -> None:
        wal_board(self.path, [])

        self.assertEqual(kanban_board.missing_columns(str(self.path)), [])


if __name__ == "__main__":
    unittest.main()
