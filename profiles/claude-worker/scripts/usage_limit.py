"""The `usage_limit` holder of the dispatch hold list: new runs wait while the Claude usage limit is reached.

Two triggers add the entry. `claude-session` adds it when Claude Code's result is a rate-limit API error
(`is_limit`), and `dispatch-triage` adds it when a board run ended `rate_limited` (`read_board`), which Hermes
records for a worker turn that hit its model provider's limit.

An episode runs from the first hit to the first finished run that started after the entry ended and did not end in
a limit. Its state is `dispatch-holds.usage-limit.json` beside the hold list:

    {"hits": int, "released_at": ISO-8601 UTC or null, "started_notified": bool, "released_notified": bool,
     "cursor": epoch seconds or null}

`released_at` is the `until` of the episode's newest entry, so the episode is released from then on. `cursor` is
the `ended_at` of the newest board run `read_board` has read. The file changes only under `host_hold`'s lock, in
the same critical section as the entry it describes, and a file that does not parse raises `host_hold.HoldError`.

A hit while the entry is live changes nothing, so many cards that hit the limit together make one hit. Otherwise
the entry lasts until the reset time plus the margin when the limit message gives one, or the base doubled for each
earlier hit of the episode, and the cap clamps both.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import factory_settings
import host_hold

EPISODE_FILE = "dispatch-holds.usage-limit.json"
USAGE_LIMIT = host_hold.USAGE_LIMIT
REASON = "the Claude usage limit was reached"
RATE_LIMITED = "rate_limited"
# The outcomes of a finished run that did work, so one that started after the entry ended ends the episode.
CLEAN_OUTCOMES = ("completed", "review_requested")
DEFAULT_BACKOFF = factory_settings.UsageLimitBackoff(timedelta(minutes=15), timedelta(minutes=2), timedelta(hours=5))
# 2 ** 20 base periods is past any cap, and keeps the multiplication small.
MAX_DOUBLINGS = 20

# The API's 429 `rate_limit_error`, the model proxy's "cooling down ... try again later", and Claude Code's own
# subscription message.
LIMIT_TEXT = re.compile(
    r"API Error: 429|rate_limit_error|cooling down.*try again later|usage limit reached|hit your (?:usage )?limit",
    re.IGNORECASE | re.DOTALL,
)
# webhook-retry's `reset_delay_seconds` forms. That plugin is installed only in the root home, so they are copied.
UNITS = re.compile(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.IGNORECASE)
RESETS_IN = re.compile(r"resets?\s+in\s*~?\s*((?:\d+\s*[a-z]+\s*)+)", re.IGNORECASE)
RETRY_AFTER = re.compile(r"retry\s+after\s+(\d+)\s*(?:seconds?|secs?|s)\b", re.IGNORECASE)
RESETS_AT = re.compile(r"resets?\s+at\s+(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))", re.IGNORECASE)
# Claude Code prints its subscription limit as `Claude AI usage limit reached|<reset epoch seconds>`.
RESETS_EPOCH = re.compile(r"limit reached\|(\d{10})\b", re.IGNORECASE)


@dataclass
class Episode:
    hits: int = 0
    released_at: datetime | None = None
    started_notified: bool = False
    released_notified: bool = False
    cursor: int | None = None


def is_limit(result: dict) -> bool:
    """Whether a Claude Code `--output-format json` result hit the usage limit. One with `is_error` false finished,
    whatever its text says."""
    text = result.get("result")
    return result.get("is_error") is True and isinstance(text, str) and LIMIT_TEXT.search(text) is not None


def reset_time(text: str, now: datetime) -> datetime | None:
    """The reset time a limit message gives, or None. Most proxy messages give none, and a time that cannot be a
    date counts as none, so the backoff still holds new runs."""
    try:
        at = RESETS_AT.search(text)
        if at:
            return datetime.fromisoformat(at.group(1).replace("Z", "+00:00"))
        epoch = RESETS_EPOCH.search(text)
        if epoch:
            return datetime.fromtimestamp(int(epoch.group(1)), timezone.utc)
        resets = RESETS_IN.search(text)
        if resets:
            minutes = sum(int(amount) * (60 if unit[0].lower() == "h" else 1) for amount, unit in UNITS.findall(resets.group(1)))
            if minutes:
                return now + timedelta(minutes=minutes)
        after = RETRY_AFTER.search(text)
        return now + timedelta(seconds=int(after.group(1))) if after else None
    except (ValueError, OverflowError):
        return None


def hit(episode: Episode, now: datetime, reset_at: datetime | None, backoff) -> datetime:
    """Count a hit made while no entry is live, and return the new entry's `until`."""
    if episode.hits == 0:
        episode.started_notified = episode.released_notified = False
    episode.hits += 1
    if reset_at is not None:
        wait = max(reset_at + backoff.margin - now, backoff.margin)
    else:
        wait = backoff.base * 2 ** min(episode.hits - 1, MAX_DOUBLINGS)
    episode.released_at = now + min(wait, backoff.cap)
    return episode.released_at


def clean_run(episode: Episode, live: bool, started_at: datetime) -> bool:
    """End the episode for a run that finished without a limit and started after the entry ended."""
    if live or episode.hits == 0 or episode.released_at is None or started_at < episode.released_at:
        return False
    episode.hits = 0
    return True


def notice(episode: Episode, live_until: datetime | None) -> tuple[str, datetime] | None:
    """The episode's next operator notice, `("start", until)` or `("release", released_at)`, with its flag set, or
    None. Each episode gets one of each. The release waits for the clean run that ends the episode, because a hit
    after the entry ends holds new runs again with no second start notice."""
    if live_until is not None and not episode.started_notified:
        episode.started_notified = True
        return "start", live_until
    if live_until is None and episode.hits == 0 and episode.started_notified and not episode.released_notified and episode.released_at:
        episode.released_notified = True
        return "release", episode.released_at
    return None


def read_episode(root: Path) -> Episode:
    path = root / EPISODE_FILE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return Episode()
    except (OSError, ValueError) as error:
        raise host_hold.HoldError(f"cannot read {path}: {error}") from None
    if not isinstance(data, dict) or set(data) != set(asdict(Episode())):
        raise host_hold.HoldError(f"{path} is not a usage-limit episode")
    hits, released, cursor = data["hits"], data["released_at"], data["cursor"]
    flags = (data["started_notified"], data["released_notified"])
    if isinstance(hits, bool) or not isinstance(hits, int) or hits < 0 or not all(isinstance(f, bool) for f in flags):
        raise host_hold.HoldError(f"{path} has a bad hit count or notice flag")
    if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int)):
        raise host_hold.HoldError(f"{path} has a cursor that is not epoch seconds")
    try:
        released_at = None if released is None else host_hold.parse(released)
    except (TypeError, ValueError):
        raise host_hold.HoldError(f"{path} has a released_at that is not an ISO-8601 UTC time") from None
    return Episode(hits, released_at, *flags, cursor)


def write_episode(root: Path, episode: Episode) -> None:
    data = {**asdict(episode), "released_at": episode.released_at and host_hold.stamp(episode.released_at)}
    path = root / EPISODE_FILE
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(temp, path)


def live_until(data: dict, now: datetime) -> datetime | None:
    entry = data["holds"].get(USAGE_LIMIT)
    until = host_hold.parse(entry["until"]) if entry else None
    return until if until and until > now else None


def record_hit(holds: host_hold.HoldList, reset_at: datetime | None, backoff) -> datetime:
    """Add the entry for a hit unless one is live, and return the live entry's `until`."""
    now = holds.clock()
    with holds.locked():
        data = host_hold.read_holds(holds.root)
        episode = read_episode(holds.root)
        current = live_until(data, now)
        if current is not None:
            return current
        until = hit(episode, now, reset_at, backoff)
        write_episode(holds.root, episode)
        holds.add_locked(data, USAGE_LIMIT, REASON, until)
        return until


def read_board(holds: host_hold.HoldList, ended_since: Callable[[int], list], backoff) -> datetime | None:
    """Apply the board runs that ended after the cursor: a `rate_limited` run is a hit, and a clean run may end the
    episode. With no cursor yet, store the current time and read nothing older. The `until` of an entry this call
    added, or None."""
    now = holds.clock()
    with holds.locked():
        data = host_hold.read_holds(holds.root)
        episode = read_episode(holds.root)
        added = None
        runs = [] if episode.cursor is None else ended_since(episode.cursor)
        if episode.cursor is None:
            episode.cursor = int(now.timestamp())
        for run in runs:
            live = live_until(data, now) is not None
            if run["outcome"] == RATE_LIMITED and not live:
                added = hit(episode, now, None, backoff)
                holds.add_locked(data, USAGE_LIMIT, REASON, added)
            elif run["outcome"] in CLEAN_OUTCOMES:
                clean_run(episode, live, datetime.fromtimestamp(run["started_at"], timezone.utc))
            episode.cursor = max(episode.cursor, run["ended_at"])
        write_episode(holds.root, episode)
        return added


def next_notice(holds: host_hold.HoldList) -> tuple[str, datetime] | None:
    """The episode's next operator notice, with its sent flag written before it is returned."""
    with holds.locked():
        episode = read_episode(holds.root)
        found = notice(episode, live_until(host_hold.read_holds(holds.root), holds.clock()))
        if found is not None:
            write_episode(holds.root, episode)
        return found
