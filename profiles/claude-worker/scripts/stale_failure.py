"""Clear a card's `last_failure_error` once a review ended its newest run, so Hermes' dispatcher stops holding it.

Hermes' `check_respawn_guard` holds a `ready` card as `blocker_auth` while `tasks.last_failure_error` names a quota,
rate-limit, or auth failure and the card's newest ended run is not `crashed`. `request-review` and `reopen-review`
leave that field alone, so a card that hit a quota wall, asked for review, and was sent back stays held for good.
Hermes' own unblock, promote, reclaim, complete, and assign clear the field.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from completion_contract import busy_timeout_ms

NEWEST_ENDED_RUN = (
    "SELECT outcome FROM task_runs WHERE task_id=tasks.id AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
)
REVIEWED = ("review_requested", "completed")


def clear_after_review(path: str, task_id: str) -> bool:
    """Whether the card's `last_failure_error` was set to NULL."""
    timeout = busy_timeout_ms()
    with closing(sqlite3.connect(path, timeout=timeout / 1000, isolation_level=None)) as board:
        board.execute(f"PRAGMA busy_timeout={timeout}")
        return board.execute(
            "UPDATE tasks SET last_failure_error=NULL WHERE id=? AND last_failure_error IS NOT NULL "
            f"AND ({NEWEST_ENDED_RUN}) IN ({', '.join('?' * len(REVIEWED))})",
            (task_id, *REVIEWED),
        ).rowcount > 0
