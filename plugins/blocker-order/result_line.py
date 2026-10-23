"""The JSON line that `claude-session` prints last to report a run, and its parser.

`claude-session` writes the line through `line`, and the `blocker-order` hook reads it through `last`, so a
key named here is the same key on both sides. `claude-session` imports this file from the worker home's
copy of the plugin, the way it imports `linear.py`. The worker reads the same line through
`profiles/claude-worker/SOUL.md`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Optional, Union

NEEDS_INPUT = "Needs input:"


@dataclass(frozen=True)
class Session:
    """The Claude session a run used, and what `claude-session` did around it."""

    session_id: str
    resumed: bool
    reason: str
    worktree: str
    branch: str
    adopted: Optional[dict]
    superseded: Any
    issue_links: Any
    triage_documents: Optional[dict]


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

    session: Session
    outcome: Outcome
    blocker_error: Optional[str] = None
    triage: bool = False
    activity: Optional[str] = None


@dataclass(frozen=True)
class Failed:
    """Claude ran but the run failed: it printed no JSON output, so there is no outcome, or it ended with a
    subtype that does not count as finished."""

    error: str
    session: Session
    outcome: Optional[Outcome] = None
    blocker_error: Optional[str] = None


@dataclass(frozen=True)
class Refused:
    """`claude-session` stopped without a Claude session, on a refusal or an unexpected error. A transient
    one goes back to the queue."""

    error: str
    transient: bool = False


Report = Union[Waiting, Done, Failed, Refused]


def needs_input(report: Optional[Report]) -> Optional[str]:
    """The run's `result` when it starts with `Needs input:`, from a finished or a failed run."""
    outcome = getattr(report, "outcome", None)
    text = outcome.result if outcome is not None else None
    return text if isinstance(text, str) and text.startswith(NEEDS_INPUT) else None


def encode(report: Report) -> dict:
    if isinstance(report, Waiting):
        return {"waiting_on": list(report.waiting_on)}
    if isinstance(report, Refused):
        return {"error": report.error, **({"transient": True} if report.transient else {})}
    body = {**asdict(report.session), **_optional(blocker_error=report.blocker_error)}
    if report.outcome is not None:
        body.update(asdict(report.outcome))
    if isinstance(report, Failed):
        return {"error": report.error, **body}
    return {**body, **({"activity": report.activity} if report.triage else {})}


def line(report: Report) -> str:
    return json.dumps(encode(report))


def decode(data: Any) -> Optional[Report]:
    """The report a line's JSON object holds, or None when it is not one."""
    if not isinstance(data, dict):
        return None
    if set(data) == {"waiting_on"}:
        cards = data["waiting_on"]
        valid = isinstance(cards, list) and cards and all(isinstance(card, str) and card for card in cards)
        return Waiting(tuple(cards)) if valid else None
    if "session_id" not in data:
        return Refused(data["error"], bool(data.get("transient"))) if isinstance(data.get("error"), str) else None
    session = _build(Session, data)
    outcome = _build(Outcome, data) if "subtype" in data else None
    if "error" in data:
        return Failed(data["error"], session, outcome, data.get("blocker_error"))
    if outcome is None:
        return None
    return Done(session, outcome, data.get("blocker_error"), "activity" in data, data.get("activity"))


def last(output: str) -> Optional[Report]:
    """The report on the last line of the output that is JSON."""
    for text in reversed(output.splitlines()):
        try:
            data = json.loads(text)
        except ValueError:
            continue
        return decode(data)
    return None


def _optional(**values: Any) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def _build(cls, data: dict):
    return cls(**{field.name: data.get(field.name) for field in fields(cls)})
