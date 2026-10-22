"""Read a Hermes board database without writing to it.

`claude-session` reads the cards for its blocker check and `check-install` probes that the read still works,
so both read the board here. The columns are Hermes' internal `tasks` schema, which Hermes may change.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from typing import NamedTuple
from urllib.parse import quote

COLUMNS = ("id", "title", "body", "branch_name", "status")
BOARD_QUERY = f"SELECT {', '.join(COLUMNS)} FROM tasks WHERE status != 'archived'"
ATTEMPTS = 5


class FileState(NamedTuple):
    inode: int
    size: int
    mtime_ns: int
    wal: bool
    journal: bool


def file_state(path: str) -> FileState:
    stat = os.stat(path)
    return FileState(
        stat.st_ino, stat.st_size, stat.st_mtime_ns, os.path.exists(f"{path}-wal"), os.path.exists(f"{path}-journal")
    )


def run(path: str, mode: str, sql: str) -> list[dict]:
    with closing(sqlite3.connect(f"file:{quote(path)}?mode={mode}", uri=True)) as board:
        board.row_factory = sqlite3.Row
        return [dict(row) for row in board.execute(sql)]


def read(path: str, sql: str) -> list[dict]:
    """The rows of one consistent snapshot of the board, or `sqlite3.OperationalError`.

    A `mode=ro` connection takes SQLite's normal locks, so it reads one snapshot in either journal mode, and
    a read-only connection never writes the database or checkpoints the WAL on close. The SQLite that macOS
    ships with its Python refuses that open on a WAL database whose -shm file is missing, which is whenever no
    connection has the board open. Then no change waits in a WAL, and the database file alone is read as
    `immutable`, without locks. A writer changes that file only while a -wal or -journal file exists, and
    the change moves the file's size or modification time, so that read counts only when no -wal or
    -journal file existed before it and none of them moved or appeared by its end."""
    error: sqlite3.Error | None = None
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(0.05 * attempt)
        try:
            return run(path, "ro", sql)
        except sqlite3.OperationalError as refused:
            if not os.path.exists(path):
                raise
            error = refused
        before = file_state(path)
        if before.wal or before.journal:
            continue
        try:
            rows = run(path, "ro&immutable=1", sql)
        except sqlite3.DatabaseError as torn:
            if file_state(path) == before:
                raise
            error = torn
            continue
        if file_state(path) == before:
            return rows
        error = sqlite3.OperationalError("the board changed while it was read")
    raise sqlite3.OperationalError(f"no consistent read of {path} in {ATTEMPTS} attempts: {error}")


def cards(path: str) -> list[dict]:
    return read(path, BOARD_QUERY)


def missing_columns(path: str) -> list[str]:
    """The columns of `COLUMNS` that the board's `tasks` table lacks."""
    present = {row["name"] for row in read(path, "PRAGMA table_info(tasks)")}
    return [column for column in COLUMNS if column not in present]
