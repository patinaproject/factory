"""The JSON line that `claude-session` prints last to report a run, and its parser.

`claude-session` writes the line through `line`, and the `blocker-order` hook reads it through `last`, so a
key named here is the same key on both sides. `claude-session` imports this file from the worker home's
copy of the plugin, the way it imports `linear.py`. The worker reads the same line through
`profiles/claude-worker/SOUL.md`.

A triage round's park reason ends with the `comments_seen` marker, which `mark_comments_seen` writes and
`read_comments_seen` reads. `card_transition.py` imports this file the same way to read it back.
"""
from __future__ import annotations

import json
import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union

NEEDS_INPUT = "Needs input:"
COMMENTS_SEEN = "comments_seen:"
GH_LOGIN = "gh_login"
# A park whose reason starts with this waits for the worker's gh login, not for a person. It never starts with
# `Needs input:`, so no reply rule and no triage round treats it as a question.
GH_LOGIN_PARK = "Waiting for the worker's gh login:"
# claude-session reads this line from the installed copy of this file, without importing it, to tell whether the
# installed blocker-order parks on it.
INSTALL_SKEW = "install_skew"
INSTALL_SKEW_PARK = "Waiting for a factory deploy:"
NEEDS_BRIEF = "needs_brief"
NEEDS_BRIEF_PARK = "Waiting for triage:"
# A live claude-session holds the card's worktree; the card waits until the lock is free.
WORKTREE_HELD = "worktree_held"
WORKTREE_HELD_PARK = "Waiting for the worktree:"
# GitHub or the gateway's token broker answered with a server error; the card waits a few minutes and runs again.
SERVER_ERROR = "server_error"
SERVER_ERROR_PARK = "Waiting after a server error:"
HOST_LOAD = "host_load"
HOST_LOAD_PARK = "Waiting for the host load to fall:"
# A finished implementation run that waits for a GitHub issue ends with this first line, then the issue's URL.
# The hook parks the card with a reason that starts with ISSUE_PARK and the URL, which `card_transition.py` reads
# back. It is not a `Refused` precondition, so it is in MACHINE_PARKS but not in PRECONDITION_PARKS.
WAITING_FOR_ISSUE = "Waiting for issue:"
ISSUE_PARK = "Waiting for an issue:"
# A card comment that starts with this and the URL records the sweep's first pass, and tells a resumed run that
# the card already parked on that issue.
ISSUE_NOTED = "Parked on issue"
_ISSUE_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/[0-9]+")
# A run stopped by SIGTERM, SIGINT, or SIGHUP during a Hermes pause parks the card until the pause ends.
STOPPED_PARK = "Waiting for hermes resume:"
# A finished implementation run that waits for another pull request to merge ends with this first line, then the
# pull request's URL. Every reason, comment, and thought names the pull request as `owner/name#number`, never by
# URL, because Hermes holds a `ready` card for a day after a card comment that holds a pull request URL.
WAITING_FOR_PULL_REQUEST = "Waiting for pull request:"
PULL_REQUEST_PARK = "Waiting for a pull request:"
# A card comment that starts with this and the short form records the sweep's first pass, and tells a resumed run
# that the card already parked on that pull request.
PULL_REQUEST_NOTED = "Parked on pull request"
_PULL_REQUEST_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([0-9]+)")
_PULL_REQUEST_REF = re.compile(r"([\w.-]+/[\w.-]+)#([0-9]+)")
# The park reason prefix of each machine condition a refusal can wait for. No reply releases such a park.
# claude-session sends a precondition only when its key is here, so an older copy blocks no card on a new key.
PRECONDITION_PARKS = {
    GH_LOGIN: GH_LOGIN_PARK,
    INSTALL_SKEW: INSTALL_SKEW_PARK,
    NEEDS_BRIEF: NEEDS_BRIEF_PARK,
    WORKTREE_HELD: WORKTREE_HELD_PARK,
    SERVER_ERROR: SERVER_ERROR_PARK,
    HOST_LOAD: HOST_LOAD_PARK,
}
MACHINE_PARKS = (*PRECONDITION_PARKS.values(), ISSUE_PARK, STOPPED_PARK, PULL_REQUEST_PARK)
_COMMENTS_SEEN_LINE = re.compile(r"\s*^" + COMMENTS_SEEN + r"[ \t]*(\d+)[ \t]*\Z", re.MULTILINE)


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


# The default of a field whose key an implementation run leaves out, where a triage run writes null.
UNSET: Any = _Unset()


@dataclass(frozen=True)
class Session:
    """The Claude session a run used, and what `claude-session` did around it. `progress` is true when the run
    changed the worktree's HEAD or its `git status --porcelain`. `comments_seen` is the number of card comments
    in the snapshot the run read; a triage round's park records it, so a reply past it releases the card.
    `session_links` reports the pull requests that `claude-session` added to the card's agent session."""

    session_id: str
    resumed: bool
    reason: str
    worktree: str
    branch: str
    progress: bool
    comments_seen: int
    adopted: Optional[dict]
    superseded: Optional[dict]
    issue_links: Optional[dict]
    triage_documents: Optional[dict]
    session_title: Optional[dict]
    session_links: Optional[dict]


@dataclass(frozen=True)
class Outcome:
    """What Claude's own JSON output said about the run."""

    subtype: Optional[str]
    num_turns: Optional[int]
    result: Optional[str]
    usage: Optional[dict]
    first_context_tokens: Optional[int]


@dataclass(frozen=True)
class Waiting:
    """Cards of the issue's blockers are still open, so Claude did not start."""

    waiting_on: tuple[str, ...]


@dataclass(frozen=True)
class Done:
    """Claude finished. A triage run names the Linear activity that reports it, which may be none."""

    session: Session = field(metadata={"flat": Session})
    outcome: Outcome = field(metadata={"flat": Outcome})
    blocker_error: Optional[str] = None
    activity: Optional[str] = UNSET


@dataclass(frozen=True)
class Failed:
    """Claude ran but the run failed: it printed no JSON output, so there is no outcome, or it ended with a
    subtype that does not count as finished."""

    error: str
    session: Session = field(metadata={"flat": Session})
    outcome: Optional[Outcome] = field(default=None, metadata={"flat": Outcome})
    blocker_error: Optional[str] = None


@dataclass(frozen=True)
class Refused:
    """`claude-session` stopped without a Claude session, on a refusal or an unexpected error. A transient
    one goes back to the queue. `precondition` names a machine condition that, once met, lets the card run:
    `gh_login` parks the card until the worker's gh login is fresh, `install_skew` until the install matches
    the commit auto-deploy recorded as deployed (a deploy that failed or was blocked keeps the card parked until a
    person finishes it and runs `auto-deploy --retry`), `needs_brief` until triage writes the issue's Agent brief, `worktree_held` until no run holds the card's
    worktree, and `server_error` until five minutes have passed since the park."""

    error: str
    transient: bool = False
    precondition: Optional[str] = None


@dataclass(frozen=True)
class Crashed:
    """Claude ran in this session, then `claude-session` failed before it could report the run."""

    error: str
    session_id: str


@dataclass(frozen=True)
class Stopped:
    """A person or process stopped `claude-session` with `signal` (such as `SIGTERM`) after `elapsed` whole
    seconds. `paused` is true when a Hermes pause was in place. `session_id` is set once Claude had started."""

    signal: str
    elapsed: int
    paused: bool
    session_id: Optional[str] = None


Report = Union[Waiting, Done, Failed, Refused, Crashed, Stopped]
VARIANTS = (Waiting, Done, Failed, Refused, Crashed, Stopped)


def precondition_refusal(report: Optional[Report]) -> Optional[Refused]:
    """A refusal that waits for a machine condition, which parks the card instead of blocking it."""
    if isinstance(report, Refused) and report.precondition in PRECONDITION_PARKS:
        return report
    return None


def stopped(report: Optional[Report]) -> Optional[Stopped]:
    """The report when `claude-session` was stopped by a signal."""
    return report if isinstance(report, Stopped) else None


def paused(home: Path) -> bool:
    """Hermes' `agent.estop.is_engaged` for a profile home: an `ESTOP` sentinel in the home or the fleet root,
    and a stat error counts as engaged. `webhook-retry`'s `queue_store.paused` is the same check, but that plugin
    is installed only in the root home, and the worker home's scripts read this one."""
    home = Path(home)
    root = home.parent.parent if home.parent.name == "profiles" else home
    try:
        return any(sentinel.exists() for sentinel in {home / "ESTOP", root / "ESTOP"})
    except OSError:
        return True


def waiting_on(report: Optional[Report]) -> Optional[list[str]]:
    """The cards a waiting report names."""
    return list(report.waiting_on) if isinstance(report, Waiting) else None


def needs_input(report: Optional[Report]) -> Optional[str]:
    """The run's `result` when it starts with `Needs input:`, from a finished or a failed run."""
    outcome = getattr(report, "outcome", None)
    text = outcome.result if outcome is not None else None
    return text if isinstance(text, str) and text.startswith(NEEDS_INPUT) else None


def waiting_for_issue(report: Optional[Report]) -> Optional[str]:
    """The run's `result` when a finished run starts it with `Waiting for issue:`."""
    text = report.outcome.result if isinstance(report, Done) else None
    return text if isinstance(text, str) and text.startswith(WAITING_FOR_ISSUE) else None


def issue_url(result: str) -> Optional[str]:
    """The GitHub issue URL that alone follows `Waiting for issue:` on its line, or None when the line holds
    anything else, such as a pull request URL. A URL in angle brackets counts."""
    first = result[len(WAITING_FOR_ISSUE):].split("\n", 1)[0].strip()
    first = first[1:-1] if first.startswith("<") and first.endswith(">") else first
    return first if _ISSUE_URL.fullmatch(first) else None


class PullRequest(NamedTuple):
    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"


def waiting_for_pull_request(report: Optional[Report]) -> Optional[str]:
    """The run's `result` when a finished run starts it with `Waiting for pull request:`."""
    text = report.outcome.result if isinstance(report, Done) else None
    return text if isinstance(text, str) and text.startswith(WAITING_FOR_PULL_REQUEST) else None


def pull_request(result: str) -> Optional[PullRequest]:
    """The pull request whose URL alone follows `Waiting for pull request:` on its line, or None when the line
    holds anything else, such as an issue URL or a pull request URL with a suffix. A URL in angle brackets counts."""
    first = result[len(WAITING_FOR_PULL_REQUEST):].split("\n", 1)[0].strip()
    first = first[1:-1] if first.startswith("<") and first.endswith(">") else first
    match = _PULL_REQUEST_URL.fullmatch(first)
    return PullRequest(match.group(1), int(match.group(2))) if match else None


def park_pull_request(reason: str) -> Optional[PullRequest]:
    """The pull request that a `PULL_REQUEST_PARK` reason names in the short form, or None."""
    if not reason.startswith(PULL_REQUEST_PARK):
        return None
    match = _PULL_REQUEST_REF.fullmatch(reason[len(PULL_REQUEST_PARK):].strip())
    return PullRequest(match.group(1), int(match.group(2))) if match else None


def mark_comments_seen(result: str, count: int) -> str:
    """A triage park's reason: the run's whole result, then a last line with the comments the run saw."""
    return f"{result.rstrip()}\n\n{COMMENTS_SEEN} {count}"


def read_comments_seen(reason: Optional[str]) -> tuple[str, Optional[int]]:
    """The reason without its marker, and the count the marker records, or None when it has no marker."""
    text = reason or ""
    match = _COMMENTS_SEEN_LINE.search(text)
    if match is None:
        return text, None
    return text[: match.start()], int(match.group(1))


def encode(report: Report) -> dict:
    """Each field is a key, and a `Session` or `Outcome` adds its own fields as keys. A field that holds its
    default writes no key."""
    body: dict = {}
    for each in fields(report):
        value = getattr(report, each.name)
        if each.default is not MISSING and value == each.default:
            continue
        if "flat" in each.metadata:
            body.update(asdict(value))
        else:
            body[each.name] = list(value) if isinstance(value, tuple) else value
    return body


def line(report: Report) -> str:
    return json.dumps(encode(report))


def decode(data: Any) -> Optional[Report]:
    """The one report whose keys are exactly the line's, or None when the line is not a report."""
    if not isinstance(data, dict):
        return None
    for variant in VARIANTS:
        report = _read(variant, data)
        if report is not None:
            return report
    return None


def last(output: str) -> Optional[Report]:
    """The report on the last line of the output that is JSON."""
    for text in reversed(output.splitlines()):
        try:
            data = json.loads(text)
        except ValueError:
            continue
        return decode(data)
    return None


def _read(variant, data: dict) -> Optional[Report]:
    values, used = {}, set()
    for each in fields(variant):
        flat = each.metadata.get("flat")
        names = [name.name for name in fields(flat)] if flat else [each.name]
        if all(name in data for name in names):
            values[each.name] = flat(**{name: data[name] for name in names}) if flat else data[each.name]
            used.update(names)
        elif each.default is MISSING:
            return None
    if used != set(data):
        return None
    if variant is Waiting:
        (name,) = values
        cards = values[name]
        if not (isinstance(cards, list) and cards and all(isinstance(card, str) and card for card in cards)):
            return None
        values[name] = tuple(cards)
    return variant(**values)
