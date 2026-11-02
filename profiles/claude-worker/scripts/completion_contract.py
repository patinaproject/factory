"""Set a merged card's `completion_contract` to `local-only`, the one write the factory makes to a card's contract.

Hermes refuses `hermes kanban complete` on a card whose contract names a repository or pull request unless every
required check on the head passed, and it counts check runs per suite, so a merged pull request whose old runs
failed can never finish its card. Hermes' `prepare_acceptance` skips acceptance for `local-only`, so once GitHub
reports the pull request `MERGED`, `card_transition.py merged` writes that and completes the card the normal way.

The write is a compare-and-set: it runs only while the card is not `done` or `archived` and its contract is this
pull request's URL, in any case, or its `<owner>/<name>`. A contract that is already `local-only`, a retry, or
empty, which Hermes' acceptance also skips, writes nothing. A contract that names another pull request or repository is left alone.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from enum import Enum

LOCAL_ONLY = "local-only"
# Hermes' DEFAULT_BUSY_TIMEOUT_MS and its override variable, in hermes_cli/kanban_db_connect.py.
HERMES_BUSY_TIMEOUT_MS = 120_000
BUSY_TIMEOUT_VARIABLE = "HERMES_KANBAN_BUSY_TIMEOUT_MS"
PULL = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)/?", re.IGNORECASE)
REPOSITORY = re.compile(r"[^/\s]+/[^/\s]+")
FINISHED = ("done", "archived")


class Write(Enum):
    WRITTEN = "written"
    ALREADY = "already"
    FINISHED = "finished"
    OTHER = "other"


class Failed(Exception):
    pass


def pull_key(url: str) -> tuple[str, int] | None:
    """`(<owner>/<name> in lower case, number)` for a pull request URL; GitHub names are case-insensitive."""
    match = PULL.fullmatch(url.strip())
    return (match.group(1).lower(), int(match.group(2))) if match else None


def names_pull(contract: str, pull_url: str) -> bool:
    key = pull_key(pull_url)
    if key is None:
        raise Failed(f"{pull_url} is not a GitHub pull request URL")
    if REPOSITORY.fullmatch(contract):
        return contract.lower() == key[0]
    return pull_key(contract) == key


def busy_timeout_ms() -> int:
    try:
        configured = int(os.environ.get(BUSY_TIMEOUT_VARIABLE, ""))
    except ValueError:
        configured = 0
    return max(HERMES_BUSY_TIMEOUT_MS, configured)


def release(path: str, task_id: str, pull_url: str) -> tuple[Write, str | None]:
    """The outcome and the contract the card held."""
    timeout = busy_timeout_ms()
    with closing(sqlite3.connect(path, timeout=timeout / 1000, isolation_level=None)) as board:
        board.execute(f"PRAGMA busy_timeout={timeout}")
        board.execute("BEGIN IMMEDIATE")
        try:
            row = board.execute("SELECT status, completion_contract FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise Failed(f"card {task_id} is not on the board {path}")
            status, contract = row
            if status in FINISHED:
                return Write.FINISHED, contract
            if not contract or contract == LOCAL_ONLY:
                return Write.ALREADY, contract
            if not names_pull(contract, pull_url):
                return Write.OTHER, contract
            board.execute(
                "UPDATE tasks SET completion_contract=? WHERE id=? AND status NOT IN ('done','archived') "
                "AND completion_contract=?",
                (LOCAL_ONLY, task_id, contract),
            )
            board.execute("COMMIT")
            return Write.WRITTEN, contract
        finally:
            if board.in_transaction:
                board.execute("ROLLBACK")
