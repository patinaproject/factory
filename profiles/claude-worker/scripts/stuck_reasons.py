"""The reasons a factory pull request can be stuck, as one table, and the pure rule that picks one.

`reopen-conflicts` reads GitHub and the card, fills a `Facts` record, and asks `classify` for the earliest
reason that applies. The order of `REASONS` is the precedence. A pending or unknown input is a fact that
settles nothing, so `classify` returns None for it and the sweep acts on a later run. This module reads
nothing from GitHub.

Kinds. A `handback` sends the card back to its worker. An `escalate` tells the card's Linear session and
the card one human step. A `repair` makes one safe change on GitHub, within a limit, and escalates when it
cannot. A `none` reason is never acted on. Reasons 2 to 4 keep their handback code in `reopen-conflicts`,
and the table lists them so the order is in one place.

A `<name>_head: <sha>` line dedupes an escalation or handback for one head. An attempt line records a
repair before it is made: `requeue_attempt: <sha> <n>`, `rerun_attempt: <sha> <run-id>`, and
`branch_update_attempt: <pr> <sha>`. A repair that reached its limit escalates once under
`<name>_limit_head: <sha>`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, NamedTuple

REVIEW = frozenset({"review"})
EVERY_STATUS = frozenset({"review", "blocked", "triage"})
NONE, HANDBACK, ESCALATE, REPAIR = "none", "handback", "escalate", "repair"

DEFAULT_LIMITS = {"requeues": 2, "reruns": 1, "branch_updates": 2}
REQUEUE_ATTEMPT = "requeue_attempt"
RERUN_ATTEMPT = "rerun_attempt"
BRANCH_UPDATE_ATTEMPT = "branch_update_attempt"
# Written by a handback, which sends the worker to push a head the sweep did not make.
HANDBACK_MARKERS = ("conflict_head", "failed_check_head", "review_head", "dequeued_own_failure_head")
REQUEUE_BLOCKED = "dequeued_base_failure_requeue_head"


@dataclass(frozen=True)
class Facts:
    status: str
    unsettled: bool = False
    draft: bool = False
    conflicting: bool = False
    own_check_failed: bool = False
    review_threads_open: bool = False
    approval_stale: bool = False
    dequeued: bool = False
    base: str | None = None
    base_escalated: bool = False
    flaky_failed: bool = False
    behind: bool = False
    branch_updatable: bool = False
    awaiting_queue: bool = False


@dataclass(frozen=True)
class Reason:
    name: str
    kind: str
    statuses: frozenset
    applies: Callable[[Facts], bool]
    repair: str | None = None
    step: str = ""
    limit_step: str = ""

    @property
    def marker(self) -> str:
        return f"{self.name}_head"

    @property
    def limit_marker(self) -> str:
        return f"{self.name}_limit_head"


REASONS = (
    Reason("draft", NONE, EVERY_STATUS, lambda f: f.draft),
    Reason("conflicting", HANDBACK, EVERY_STATUS, lambda f: f.conflicting),
    Reason("own_check_failed", HANDBACK, REVIEW, lambda f: f.own_check_failed),
    Reason("review_threads_open", HANDBACK, EVERY_STATUS, lambda f: f.review_threads_open),
    Reason(
        "approval_stale", ESCALATE, REVIEW, lambda f: f.approval_stale,
        step=(
            "{pr} has no approval on its current head {head}, and the base branch rules require approval of the "
            "most recent push. A reviewer approves the pull request again on GitHub."
        ),
    ),
    Reason(
        "dequeued_base_failure", REPAIR, REVIEW,
        lambda f: f.dequeued and (f.base == "failed" or (f.base == "passed" and f.base_escalated)),
        repair="requeue",
        step=(
            "The merge queue dropped {pr} because a required check failed on the head of {base}, so the "
            "pull request is not at fault. A person fixes {base}. {requeue_note}"
        ),
        limit_step=(
            "The sweep requeued {pr} {attempts} times on head {head} and the merge queue dropped it each time. "
            "A person reads the failing queue run and queues the pull request again, or fixes what fails."
        ),
    ),
    Reason(
        "dequeued_own_failure", HANDBACK, REVIEW, lambda f: f.dequeued and f.base in ("passed", "missing"),
        step=(
            "The merge queue dropped pull request {pr} at head {head}. {base_note} Read the queue run of "
            "{checks}, fix the cause, and publish the fix with push-signed. When you cannot fix it with "
            "confidence, stop and make the first line of your final message `Needs input:` followed by the reason."
        ),
    ),
    Reason(
        "flaky_check", REPAIR, REVIEW, lambda f: f.flaky_failed, repair="rerun",
        limit_step=(
            "{checks} failed on {pr} at head {head} again after the sweep re-ran it {attempts} time(s). "
            "A person reads the failing job and re-runs it or fixes the cause."
        ),
    ),
    Reason(
        "behind_base", REPAIR, REVIEW, lambda f: f.behind and f.branch_updatable, repair="update_branch",
        step=(
            "GitHub refused to update {pr} from {base}: {detail}. A person updates the branch on GitHub or "
            "grants the App what the refusal names."
        ),
        limit_step=(
            "The sweep updated {pr} from {base} {attempts} times and it is behind again. A person updates "
            "the branch, or turns on branch updates in the merge queue and sets `updates_branch` for the repository."
        ),
    ),
    Reason(
        "awaiting_queue_entry", ESCALATE, REVIEW, lambda f: f.awaiting_queue,
        step=(
            "{pr} is approved and its required checks pass, and only the merge queue can merge it. "
            "A person adds it to the merge queue."
        ),
    ),
)
BY_NAME = {reason.name: reason for reason in REASONS}


def classify(facts: Facts) -> Reason | None:
    """The earliest reason that applies to the card's status, or None while an input is pending or unknown."""
    if facts.unsettled:
        return None
    return next((reason for reason in REASONS if facts.status in reason.statuses and reason.applies(facts)), None)


class Requeue(NamedTuple):
    kind: str
    value: str | None = None


def requeue_mechanism(entry) -> Requeue:
    """What a `merge_queue.<repo>.requeue` value declares; a missing or malformed value is `none`."""
    value = entry.get("requeue") if isinstance(entry, dict) else None
    if isinstance(value, dict) and len(value) == 1:
        (kind, text), = value.items()
        if kind in ("command", "label") and isinstance(text, str) and text.strip():
            return Requeue(kind, text.strip() if kind == "label" else text)
    return Requeue("none")


def updates_branch(entry) -> bool:
    return isinstance(entry, dict) and entry.get("updates_branch") is True


def flake_signals(entry) -> frozenset:
    value = entry.get("flake_signals") if isinstance(entry, dict) else None
    return frozenset(name for name in value if isinstance(name, str) and name) if isinstance(value, list) else frozenset()


def queue_labels(entry) -> frozenset:
    value = entry.get("labels") if isinstance(entry, dict) else None
    return frozenset(str(name).lower() for name in value) if isinstance(value, list) else frozenset()


def queue_checks(entry) -> frozenset:
    value = entry.get("checks") if isinstance(entry, dict) else None
    return frozenset(name for name in value if isinstance(name, str)) if isinstance(value, list) else frozenset()


def queued_by_person(events: list, labels: frozenset, command: str | None, is_app: Callable[[str], bool]) -> bool:
    """True when the newest queue action by someone other than the App is an add, from the issue timeline."""
    newest: bool | None = None
    for event in events:
        kind = event.get("event")
        login = ((event.get("actor") or event.get("user") or {}).get("login")) or ""
        if is_app(login):
            continue
        if kind in ("labeled", "unlabeled") and str((event.get("label") or {}).get("name", "")).lower() in labels:
            newest = kind == "labeled"
        elif kind == "commented" and command is not None and (event.get("body") or "").strip() == command.strip():
            newest = True
    return bool(newest)


def marker_lines(comments: list[list[str]]) -> set[str]:
    return {line for lines in comments for line in lines}


def attempts(comments: list[list[str]], kind: str) -> list[str]:
    """The text after `<kind>: ` of each attempt line, oldest first."""
    prefix = f"{kind}: "
    return [line[len(prefix):] for lines in comments for line in lines if line.startswith(prefix)]


def branch_update_attempts(comments: list[list[str]], pull: int) -> list[str]:
    """Head shas the sweep updated for this pull request since a handback sent the worker to push its own head."""
    found: list[str] = []
    for lines in comments:
        if any(line.startswith(f"{marker}: ") for marker in HANDBACK_MARKERS for line in lines):
            found = []
        for field in attempts([lines], BRANCH_UPDATE_ATTEMPT):
            number, _, sha = field.partition(" ")
            if number == str(pull):
                found.append(sha)
    return found
