from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

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


CARDS = 3000

# Each transaction moves every card to one new status, so a consistent read sees one status on every card.
# `held` keeps a second connection open, so the -wal and -shm files stay.
CONCURRENT_WRITER = """
import sqlite3, sys, time
path, mode, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
if mode == "held":
    holder = sqlite3.connect(path)
    holder.execute("PRAGMA journal_mode = WAL")
    holder.execute("SELECT count(*) FROM tasks").fetchall()
end = time.monotonic() + seconds
version = 0
while time.monotonic() < end:
    version += 1
    board = sqlite3.connect(path, timeout=30)
    board.execute("PRAGMA journal_mode = %s" % ("DELETE" if mode == "delete" else "WAL"))
    board.execute("UPDATE tasks SET status = ?, body = ?", ("s%d" % version, "x" * (400 + version % 7 * 300)))
    board.commit()
    board.close()
"""


# One transaction, as Hermes writes the board. The checkpoint puts it in the database file, as the last
# connection's close does on a live board. A reader's lock can hold it off, and then it gives up.
ONE_WRITE = """
import sqlite3, sys
board = sqlite3.connect(sys.argv[1], timeout=0.5)
board.execute("PRAGMA journal_mode = %s" % sys.argv[2])
board.execute("UPDATE tasks SET status = 's1', body = ?", ("1" * 1500,))
board.commit()
board.execute("PRAGMA wal_checkpoint(TRUNCATE)")
board.close()
"""


def busy_board(path: Path, journal_mode: str) -> None:
    with closing(sqlite3.connect(path)) as board:
        board.execute(f"PRAGMA journal_mode = {journal_mode}")
        board.execute(TASKS)
        board.executemany(
            "INSERT INTO tasks VALUES (?, 'T', ?, 'b', 's0', 'claude-worker')",
            [(f"t_{n}", "0" * 400) for n in range(CARDS)],
        )
        board.commit()


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

    def test_a_wal_board_with_no_wal_files_is_read_without_changing_it(self) -> None:
        with closing(sqlite3.connect(self.path)) as board:
            board.execute("PRAGMA journal_mode = WAL")
            board.execute(TASKS)
            board.execute("INSERT INTO tasks VALUES ('t_a', 'A', '', 'a-work', 'todo', 'claude-worker')")
            board.commit()
        for suffix in ("-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        before = self.path.read_bytes()

        self.assertEqual([card["id"] for card in kanban_board.cards(str(self.path))], ["t_a"])
        self.assertEqual(self.path.read_bytes(), before)
        wal = Path(f"{self.path}-wal")
        self.assertEqual(wal.read_bytes() if wal.exists() else b"", b"")

    def write_during_the_first_read(self, journal_mode: str) -> list[dict]:
        """`kanban_board.cards`, with one Hermes write run partway through its first scan of the board."""
        connect = sqlite3.connect
        pending = [True]

        def write_once() -> int:
            if pending:
                pending.pop()
                subprocess.run(
                    [sys.executable, "-c", ONE_WRITE, str(self.path), journal_mode], stderr=subprocess.DEVNULL
                )
            return 0

        def connect_and_interrupt(*args, **kwargs):
            board = connect(*args, **kwargs)
            board.set_progress_handler(write_once, 20000)
            return board

        with mock.patch.object(kanban_board.sqlite3, "connect", connect_and_interrupt):
            return kanban_board.cards(str(self.path))

    def assert_one_snapshot(self, cards: list[dict]) -> None:
        self.assertEqual(len(cards), CARDS)
        self.assertEqual(len({card["status"] for card in cards}), 1, sorted({card["status"] for card in cards}))

    def test_a_wal_board_with_no_wal_files_is_read_as_one_snapshot_when_hermes_writes_during_the_read(self) -> None:
        busy_board(self.path, "WAL")
        for suffix in ("-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

        self.assert_one_snapshot(self.write_during_the_first_read("WAL"))

    def test_a_delete_mode_board_is_read_as_one_snapshot_when_hermes_writes_during_the_read(self) -> None:
        busy_board(self.path, "DELETE")

        self.assert_one_snapshot(self.write_during_the_first_read("DELETE"))

    def test_a_board_that_changes_during_every_unlocked_read_raises_after_a_bounded_number_of_reads(self) -> None:
        busy_board(self.path, "WAL")
        for suffix in ("-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        # A read-only directory stops every SQLite from creating the -shm file, so each read is unlocked.
        self.path.parent.chmod(0o555)
        self.addCleanup(self.path.parent.chmod, 0o755)
        connect = sqlite3.connect
        reads = []

        def touch() -> int:
            os.utime(self.path, ns=(len(reads), len(reads)))
            return 0

        def connect_and_touch(*args, **kwargs):
            board = connect(*args, **kwargs)
            reads.append(args[0])
            board.set_progress_handler(touch, 20000)
            return board

        with mock.patch.object(kanban_board.sqlite3, "connect", connect_and_touch), \
                mock.patch.object(kanban_board.time, "sleep"):
            with self.assertRaisesRegex(sqlite3.OperationalError, "the board changed while it was read"):
                kanban_board.cards(str(self.path))

        self.assertLessEqual(len(reads), 2 * kanban_board.ATTEMPTS)

    def assert_every_read_is_one_snapshot(self, journal_mode: str, writer_mode: str) -> None:
        busy_board(self.path, journal_mode)
        writer = subprocess.Popen(
            [sys.executable, "-c", CONCURRENT_WRITER, str(self.path), writer_mode, "2.5"]
        )
        self.addCleanup(writer.kill)
        snapshots, torn = set(), []
        while writer.poll() is None:
            try:
                cards = kanban_board.cards(str(self.path))
            except sqlite3.Error:
                continue
            statuses = {card["status"] for card in cards}
            if len(cards) != CARDS or len(statuses) != 1:
                torn.append((len(cards), sorted(statuses)))
            snapshots |= statuses
            time.sleep(0.01)
        writer.wait()

        self.assertEqual(writer.returncode, 0)
        self.assertGreater(len(snapshots), 2)
        self.assertEqual((torn[:3], len(torn)), ([], 0))

    def test_a_delete_mode_board_is_read_as_one_snapshot_while_hermes_writes(self) -> None:
        self.assert_every_read_is_one_snapshot("DELETE", "delete")

    def test_a_wal_board_held_open_is_read_as_one_snapshot_while_hermes_writes(self) -> None:
        self.assert_every_read_is_one_snapshot("WAL", "held")

    def test_a_missing_board_raises_the_error_sqlite_gives(self) -> None:
        with self.assertRaisesRegex(sqlite3.OperationalError, "^unable to open database file$"):
            kanban_board.cards(str(self.path))

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
