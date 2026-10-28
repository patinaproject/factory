from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

QUEUE_DIRECTORY = "webhook-retry"
USAGE_LIMIT_REASONS = frozenset({"rate_limit", "upstream_rate_limit"})
# Hermes waits up to 600 s between its own attempts, so a turn that reported a usage limit may still be running.
MIN_DELAY_SECONDS = 30 * 60
BACKOFF_CAP_SECONDS = 4 * 60 * 60
GIVE_UP_AGE = dt.timedelta(hours=48)
PENDING_AGE = dt.timedelta(hours=24)
UNSTARTED_AGE = dt.timedelta(minutes=10)
MAX_ATTEMPTS = 8
# Hermes stops a cron script after an hour, so a run starts no retry after this much time.
RUN_BUDGET = dt.timedelta(minutes=40)
DEFAULT_REASON = "usage_limit"
SENTINEL_NAME = "ESTOP"
KEY_PREFIX = "webhook:v2:"
ERROR_LIMIT = 300
REPORT_VARIABLE = "WEBHOOK_RETRY_REPORT"

REMOVED = "removed"
TRANSITIONS: dict[tuple[Optional[str], str], str] = {
    (None, "dispatch"): "pending",
    ("pending", "dispatch"): "pending",
    (None, "paused"): "queued",
    ("pending", "paused"): "queued",
    ("pending", "admitted"): "pending",
    ("pending", "unadmitted"): "queued",
    ("pending", "usage_limit"): "queued",
    ("queued", "usage_limit"): "queued",
    ("pending", "turn_ended"): REMOVED,
    ("queued", "turn_ended"): REMOVED,
    ("pending", "expire"): REMOVED,
    ("queued", "retry_started"): "running",
    ("running", "retry_succeeded"): REMOVED,
    ("running", "retry_usage_limit"): "queued",
    ("running", "retry_failed"): "failed",
    ("running", "interrupted"): "failed",
    ("queued", "give_up"): "given_up",
}

_UNITS = re.compile(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.IGNORECASE)
_RESETS_IN = re.compile(r"resets?\s+in\s*~?\s*((?:\d+\s*[a-z]+\s*)+)", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry\s+after\s+(\d+)\s*(?:seconds?|secs?|s)\b", re.IGNORECASE)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text: Optional[str]) -> Optional[dt.datetime]:
    if not text:
        return None
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def truncate(text: Any, limit: int) -> str:
    return str(text)[:limit]


def reason_of(entry: dict[str, Any]) -> str:
    return entry.get("reason", DEFAULT_REASON)


def fleet_root(home: Path) -> Path:
    """Hermes' `get_default_hermes_root` for a home, which this script cannot import."""
    home = Path(home)
    return home.parent.parent if home.parent.name == "profiles" else home


def paused(home: Path) -> bool:
    """Hermes' `agent.estop.is_engaged`: either sentinel exists, and a stat error counts as engaged."""
    engaged = False
    for sentinel in {Path(home) / SENTINEL_NAME, fleet_root(home) / SENTINEL_NAME}:
        try:
            engaged = engaged or sentinel.exists()
        except OSError:
            return True
    return engaged


def queue_dir(home: Path) -> Path:
    return Path(home) / QUEUE_DIRECTORY


def decode_key(key: str) -> Optional[tuple[str, str, str]]:
    if not isinstance(key, str) or not key.startswith(KEY_PREFIX):
        return None
    body = key[len(KEY_PREFIX):]
    try:
        parts = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if not (isinstance(parts, list) and len(parts) == 3 and all(isinstance(part, str) for part in parts)):
        return None
    return parts[0], parts[1], parts[2]


def reset_delay_seconds(message: str) -> Optional[float]:
    if not message:
        return None
    resets = _RESETS_IN.search(message)
    if resets:
        total = 0
        for amount, unit in _UNITS.findall(resets.group(1)):
            total += int(amount) * (3600 if unit[0].lower() == "h" else 60)
        if total:
            return float(total)
    after = _RETRY_AFTER.search(message)
    if after:
        return float(after.group(1))
    return None


def ensure_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)


@contextlib.contextmanager
def file_lock(directory: Path, name: str, *, blocking: bool = True) -> Iterator[bool]:
    ensure_dir(directory)
    fd = os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def load(directory: Path) -> dict[str, dict[str, Any]]:
    path = directory / "queue.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text())["entries"]


def save(directory: Path, entries: dict[str, dict[str, Any]]) -> None:
    fd, temp = tempfile.mkstemp(dir=directory, prefix="queue.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"version": 1, "entries": entries}, handle, indent=2, sort_keys=True)
        os.chmod(temp, 0o600)
        os.replace(temp, directory / "queue.json")
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def mutate(directory: Path, change: Callable[[dict[str, dict[str, Any]]], bool]) -> None:
    with file_lock(directory, "queue.lock"):
        entries = load(directory)
        if change(entries):
            save(directory, entries)


def apply(entries: dict[str, dict[str, Any]], key: str, event: str, /, **fields: Any) -> bool:
    current = entries.get(key)
    state = current["state"] if current else None
    following = TRANSITIONS.get((state, event))
    if following is None:
        return False
    if following == REMOVED:
        del entries[key]
        return True
    entry = current or {}
    entry.update(fields)
    entry.update(state=following, updated_at=stamp(now()))
    entries[key] = entry
    return True


def backoff_seconds(attempts: int) -> float:
    return min(MIN_DELAY_SECONDS * 2 ** (attempts - 1), BACKOFF_CAP_SECONDS)
