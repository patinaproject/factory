"""Clear the `Issue` burst accumulator that `skills/setup-factory/scripts/linear-route` keeps.

The script and this module share `<home>/linear-route/bursts.json`, its `lock`, and the `factoryRoute` stamp. The
dispatch hook clears an issue's entries once its settled turn has been handled, whatever the outcome, so the next burst
unions only its own keys.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Tuple

STORE = "linear-route"
STAMP = "factoryRoute"
# Well under Hermes' 30 s script timeout, which would drop the event.
LOCK_SECONDS = 5.0


def take_lock(handle) -> None:
    deadline = time.monotonic() + LOCK_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"the store lock was held for {LOCK_SECONDS:.0f} s")
            time.sleep(0.05)


def stamp(payload: Any) -> Optional[Tuple[str, int]]:
    """The issue id and seq the script stamped on a coalescible `Issue` payload, or None."""
    value = payload.get(STAMP) if isinstance(payload, dict) else None
    if isinstance(value, dict) and isinstance(value.get("issue"), str) and isinstance(value.get("seq"), int):
        return value["issue"], value["seq"]
    return None


def clear(home: Path, payload: Any) -> None:
    """Drop the issue's entries up to the stamped seq. An update that arrived after the settle has a higher seq and stays."""
    found = stamp(payload)
    if found is None:
        return
    issue, seq = found
    store = Path(home) / STORE
    store.mkdir(parents=True, exist_ok=True)
    with open(store / "lock", "a") as lock:
        take_lock(lock)
        path = store / "bursts.json"
        try:
            bursts = json.loads(path.read_text())
        except FileNotFoundError:
            return
        issues = bursts.setdefault("issues", {})
        remaining = [entry for entry in issues.get(issue, []) if entry["seq"] > seq]
        if remaining:
            issues[issue] = remaining
        else:
            issues.pop(issue, None)
        fd, temporary = tempfile.mkstemp(dir=store, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(bursts, handle)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
