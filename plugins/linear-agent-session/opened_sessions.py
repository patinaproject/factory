"""Sessions the `linear_agent_session_create` tool opened.

Linear sends a `created` webhook for every session, and its `creatorId` names a person even when the factory made the
session, so the payload cannot say whose it is. The tool records the ID it got back; triage looks it up.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
from typing import Mapping, Optional

RETENTION = dt.timedelta(days=1)


def store_dir(environ: Mapping[str, str]) -> Optional[str]:
    if environ.get("HERMES_HOME"):
        return os.path.join(environ["HERMES_HOME"], "plugin-data", "linear-agent-session", "opened-sessions")
    return None


def _path(directory: str, session_id: str) -> str:
    return os.path.join(directory, hashlib.sha256(session_id.encode()).hexdigest())


def record(directory: Optional[str], session_id: str, now: dt.datetime) -> None:
    if directory is None:
        return
    os.makedirs(directory, exist_ok=True)
    with open(_path(directory, session_id), "w"):
        pass
    cutoff = (now - RETENTION).timestamp()
    for entry in os.scandir(directory):
        with contextlib.suppress(FileNotFoundError):
            if entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)


def recorded(directory: Optional[str], session_id: str) -> bool:
    return directory is not None and os.path.exists(_path(directory, session_id))
