"""Read a Hermes board database without writing to it.

`claude-session` reads the cards for its blocker check and `check-install` probes that the read still works,
so both open the board here. The columns are Hermes' internal `tasks` schema, which Hermes may change.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

COLUMNS = ("id", "title", "body", "branch_name", "status")
BOARD_QUERY = f"SELECT {', '.join(COLUMNS)} FROM tasks WHERE status != 'archived'"


def connect(path: str) -> sqlite3.Connection:
    """Open the board so that reading it, and closing it, change no file.

    A read-write connection that closes last checkpoints the WAL into the database, which is a write, and
    `setconfig` can stop that only on Python 3.12 and later, while macOS ships 3.9. So the connection is
    read-only. A read-only connection to a WAL database needs the -wal and -shm files, which the system
    Python will not create. Those files exist whenever a connection, such as the gateway's, has the board
    open. Without them no connection exists, no change is pending in a WAL, and the database file alone is
    complete, so it opens as `immutable`."""
    mode = "ro" if all(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm")) else "ro&immutable=1"
    board = sqlite3.connect(f"file:{quote(path)}?mode={mode}", uri=True)
    board.row_factory = sqlite3.Row
    return board


def cards(path: str) -> list[dict]:
    with closing(connect(path)) as board:
        return [dict(row) for row in board.execute(BOARD_QUERY)]


def missing_columns(path: str) -> list[str]:
    """The columns of `COLUMNS` that the board's `tasks` table lacks."""
    with closing(connect(path)) as board:
        present = {row["name"] for row in board.execute("PRAGMA table_info(tasks)")}
    return [column for column in COLUMNS if column not in present]
