"""Which Kanban card each running `claude-session` serves, and the stop of a run whose card no longer owns it.

Hermes archives a card by signaling only its worker. `claude-session` runs in its own session, so it and its
`claude -p` child outlive the archive, hold the worktree lock and keep the Claude session id busy, and the issue's
next card is refused. Each `claude-session` therefore writes a record under `$HERMES_HOME/claude-session-runs`
while it runs, and two readers use it. The run itself reads its card and stops when the card is no longer the
live one. A new `claude-session` that finds the lock or the session id busy looks up the holder's record and
stops the holder when its card is not live.

A record names the process by pid and by the start time `ps` reports, so a recycled pid never matches. A card is
live while its status is `running` and its current run is the run the record was made for. A card that cannot
be read is not judged: the run is neither stopped nor taken over.
"""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import kanban_board

GRACE_SECONDS = float(os.environ.get("CLAUDE_SESSION_GRACE_SECONDS", 10.0))
# `claude-session` ends claude with SIGTERM, waits one grace period, then SIGKILL and waits another.
STOP_WAIT_SECONDS = 2 * GRACE_SECONDS + 5
CARD_POLL_ENV = "CLAUDE_SESSION_CARD_POLL_SECONDS"
CARD_POLL_SECONDS = 5.0
RUNS_DIR = "claude-session-runs"


@dataclasses.dataclass
class Record:
    pid: int
    started: str
    task_id: str
    run_id: str | None
    board: str | None
    session_id: str | None
    worktree: str
    claude_pid: int | None = None

    @property
    def path(self) -> Path:
        return runs_dir() / f"{self.pid}.json"

    def save(self) -> None:
        """Rewrite the record, unless a reader already claimed it."""
        if self.path.exists():
            write(self)


class Verdict(NamedTuple):
    status: str | None
    stale_reason: str | None


def runs_dir() -> Path:
    return Path(os.environ["HERMES_HOME"]) / RUNS_DIR


def write(record: Record) -> None:
    directory = runs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temp = record.path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(dataclasses.asdict(record), file)
    os.replace(temp, record.path)


def process_start(pid: int) -> str | None:
    """The start time `ps` reports for a live process, or None when the process is gone or a zombie."""
    listing = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True)
    state, _, started = listing.stdout.strip().partition(" ")
    return None if not started or state.startswith("Z") else started.strip()


def alive(record: Record) -> bool:
    return process_start(record.pid) == record.started


def card_state(board: str | None, task_id: str) -> tuple[str, int | None] | None:
    """The card's status and current run id, or None when the board is unset, unreadable, or lacks the card."""
    if not board:
        return None
    try:
        return kanban_board.run_state(board, task_id)
    except sqlite3.Error:
        return None


def register(task_id: str, worktree: Path) -> Record:
    board = os.environ.get("HERMES_KANBAN_DB")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not run_id:
        state = card_state(board, task_id)
        run_id = str(state[1]) if state and state[0] == "running" and state[1] is not None else None
    record = Record(
        pid=os.getpid(), started=process_start(os.getpid()), task_id=task_id, run_id=run_id, board=board,
        session_id=None, worktree=str(worktree),
    )
    write(record)
    return record


def unregister(record: Record) -> None:
    record.path.unlink(missing_ok=True)


def owner(*, session_id: str | None = None, worktree: str | None = None) -> Record | None:
    """The live run whose record has this session id or worktree. A record of an ended process is deleted."""
    for path in sorted(runs_dir().glob("*.json")) if runs_dir().is_dir() else ():
        record = Record(**json.loads(path.read_text()))
        if not alive(record):
            path.unlink(missing_ok=True)
        elif (session_id and record.session_id == session_id) or (worktree and record.worktree == worktree):
            return record
    return None


def live_count() -> int:
    count = 0
    for path in sorted(runs_dir().glob("*.json")) if runs_dir().is_dir() else ():
        if alive(Record(**json.loads(path.read_text()))):
            count += 1
        else:
            path.unlink(missing_ok=True)
    return count


def judge(record: Record) -> Verdict:
    """The card's status and why the run is no longer its card's, None for a live card or one that cannot be read."""
    state = card_state(record.board, record.task_id)
    if state is None or record.run_id is None:
        return Verdict(state[0] if state else None, None)
    status, run = state
    if status != "running":
        return Verdict(status, f"the card is {status}")
    if str(run) != record.run_id:
        return Verdict(status, "the card started a newer run")
    return Verdict(status, None)


def claim(record: Record) -> bool:
    """True for the one caller that removed the record, so only that caller stops the run and tells the issue."""
    try:
        record.path.unlink()
    except FileNotFoundError:
        return False
    return True


def stop(record: Record) -> None:
    """End the run the way its own signal handler does, then force it when it outlasts that."""
    if not alive(record):
        return
    os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not alive(record):
            return
        time.sleep(0.1)
    if alive(record):
        os.kill(record.pid, signal.SIGKILL)
        if record.claude_pid:
            try:
                os.killpg(record.claude_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Watch:
    """Calls `judge` for the run's own record at most every `poll_seconds`."""

    def __init__(self, record: Record, poll_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.record = record
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.next_check = clock() + poll_seconds

    def stale_reason(self) -> str | None:
        if self.record.run_id is None or self.clock() < self.next_check:
            return None
        self.next_check = self.clock() + self.poll_seconds
        return judge(self.record).stale_reason
