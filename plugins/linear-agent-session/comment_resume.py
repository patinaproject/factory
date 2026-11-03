"""When a top-level Linear issue comment resumes a card that waits for a person, as `triage_intent.comment_intent` applies it.

A thread reply reaches a card through the agent session. A top-level comment is often discussion, so it resumes
a card only when its author may direct the card, the card waits for a person, and the comment is not teammate talk.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple, Optional

from .triage_exit import Card, History

# Linear writes a mention in comment markdown as the person's profile URL, which carries the username and no user id.
PROFILE_URL = re.compile(r"https://linear\.app/[^/\s]+/profiles/([^\s/)>\]\"']+)", re.IGNORECASE)
# The park reason prefix of each machine condition, from `blocker-order`'s `result_line.MACHINE_PARKS`. A test fails
# when the two differ. No person's answer releases such a park.
MACHINE_PARKS = (
    "Waiting for the worker's gh login:",
    "Waiting for a factory deploy:",
    "Waiting for triage:",
    "Waiting for an issue:",
)
WAITING_STATUSES = ("blocked", "triage")


class TopLevelComment(NamedTuple):
    id: str
    issue_id: str
    author_id: Optional[str]
    body: str
    url: Optional[str]


def parse(payload: Mapping[str, Any]) -> TopLevelComment | str:
    """The comment, or why this delivery cannot resume a card."""
    data = payload.get("data")
    if payload.get("action") != "create" or not isinstance(data, Mapping):
        return "the delivery is not a created comment"
    if data.get("parentId") is not None:
        return "the comment is a reply in a thread"
    comment_id, issue_id, body = data.get("id"), data.get("issueId"), data.get("body")
    if not (isinstance(comment_id, str) and comment_id and isinstance(issue_id, str) and issue_id and isinstance(body, str)):
        return "the comment names no id, issue, or text"
    author = data.get("userId")
    url = payload.get("url")
    return TopLevelComment(comment_id, issue_id, author if isinstance(author, str) and author else None, body, url if isinstance(url, str) else None)


def comment_url(comment: TopLevelComment, issue_url: Optional[str]) -> Optional[str]:
    """Linear's own link for the comment, else the issue's link with the comment's anchor."""
    if comment.url and "#comment-" in comment.url:
        return comment.url
    base = comment.url or issue_url
    return f"{base.split('#')[0]}#comment-{comment.id}" if base else None


def profile_slugs(text: str) -> set[str]:
    return {slug.lower() for slug in PROFILE_URL.findall(text)}


def profile_slug(url: str) -> Optional[str]:
    match = PROFILE_URL.search(url)
    return match.group(1) if match else None


def scheduled_waits_for_person(history: History) -> bool:
    """The newest park is not a machine park, and no person stopped the card after it."""
    # Hermes writes a `scheduled` event with every park; without one, any stop counts as after it.
    reason, parked_at = history.parks[-1] if history.parks else (None, 0)
    if isinstance(reason, str) and reason.startswith(MACHINE_PARKS):
        return False
    return not any(stopped_at >= parked_at for stopped_at in history.stops)


class Waiting(NamedTuple):
    kind: str
    card: Card


def waiting_card(triage: Optional[Card], work: Optional[Card], delegated: bool, history_of: Any) -> Optional[Waiting]:
    """The card a person's answer would release. The triage card wins when both wait, as `_session_prompted` has it."""
    if triage is not None and (
        triage.status in WAITING_STATUSES or (triage.status == "scheduled" and scheduled_waits_for_person(history_of(triage.id)))
    ):
        return Waiting("triage", triage)
    if work is not None and delegated and work.status in WAITING_STATUSES:
        return Waiting("work", work)
    return None
