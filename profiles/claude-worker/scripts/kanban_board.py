"""Read a Hermes board database without writing to it.

`claude-session` reads the cards for its blocker check, `dispatch-triage` and `check-install` read the cards that
wait for a reply, and `check-install` probes that every read still works, so all of them read the board here.
The columns are Hermes' internal `tasks`, `task_comments`, and `task_events` schema, which Hermes may change.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from typing import NamedTuple
from urllib.parse import quote

COLUMNS = ("id", "title", "body", "branch_name", "status", "block_kind")
COMMENT_COLUMNS = ("id", "task_id", "author", "body", "created_at")
EVENT_COLUMNS = ("id", "task_id", "kind", "payload", "created_at")
TABLES = {"tasks": COLUMNS, "task_comments": COMMENT_COLUMNS, "task_events": EVENT_COLUMNS}
BOARD_QUERY = f"SELECT {', '.join(COLUMNS)} FROM tasks WHERE status != 'archived'"
# The ways a card waited for input before triage rounds parked in `scheduled`.
BLOCKED_FOR_INPUT_QUERY = (
    "SELECT id, status FROM tasks WHERE block_kind = 'needs_input' AND status IN ('blocked', 'triage') ORDER BY id"
)
# Hermes' `show --json` orders comments by created_at and events by created_at, then id.
SCHEDULED_QUERY = """
SELECT c.task_id AS task_id, 'comments' AS list, c.author AS author, c.body AS body, NULL AS kind,
       c.created_at AS created_at, c.id AS row
  FROM task_comments c JOIN tasks t ON t.id = c.task_id WHERE t.status = 'scheduled'
UNION ALL
SELECT e.task_id, 'events', NULL, e.payload, e.kind, e.created_at, e.id
  FROM task_events e JOIN tasks t ON t.id = e.task_id WHERE t.status = 'scheduled' AND e.kind = 'scheduled'
ORDER BY task_id, list, created_at, row
"""
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


def blocked_for_input(path: str) -> list[dict]:
    """The cards blocked with kind `needs_input`, or in Hermes' `triage` status for it."""
    return read(path, BLOCKED_FOR_INPUT_QUERY)


def scheduled(path: str) -> dict[str, dict]:
    """Each `scheduled` card's comments and `scheduled` events, from one snapshot, in the shape
    `hermes kanban show --json` gives them. A card with neither is left out."""
    cards: dict[str, dict] = {}
    for row in read(path, SCHEDULED_QUERY):
        card = cards.setdefault(row["task_id"], {"comments": [], "events": []})
        if row["list"] == "comments":
            card["comments"].append({"author": row["author"], "body": row["body"], "created_at": row["created_at"]})
        else:
            payload = json.loads(row["body"]) if row["body"] else None
            card["events"].append({"kind": row["kind"], "payload": payload, "created_at": row["created_at"]})
    return cards


def missing_columns(path: str) -> list[str]:
    """The columns of `TABLES` that the board lacks, each as `<table>.<column>`."""
    missing = []
    for table, columns in TABLES.items():
        present = {row["name"] for row in read(path, f"PRAGMA table_info({table})")}
        missing += [f"{table}.{column}" for column in columns if column not in present]
    return missing
