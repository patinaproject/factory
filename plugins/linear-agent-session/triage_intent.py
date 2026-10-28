"""Compute what a Linear triage turn is about, so the prompt branches on `action` instead of working it out.

The `pre_gateway_dispatch` hook puts `render(compute(...))` in front of the turn's prompt, and `cli.py triage-intent`
does the same for a `webhook-retry` replay. The routing it replaces is the card lookup, the foreign-card scan, the
delegation gate, the "Session created" duplicate check, and the reply text. The effects stay in `templates/triage-linear.md`.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Mapping, NamedTuple, Optional

from . import blocks, linear, triage_exit
from .triage_exit import Card

TRIAGE_BOARD = triage_exit.TRIAGE_BOARD
DUPLICATE_WINDOW = dt.timedelta(minutes=5)
# Linear stamps a session and its webhook within moments of each other, in either order.
CLOCK_SKEW = dt.timedelta(minutes=1)

# Every action the prompt handles. A machine contract: `templates/tests/test_render.py` fails when the prompt omits one.
ACTIONS = {
    "ignore_factory_session": "the factory opened a session on an undelegated issue; post nothing",
    "answer_mention": "a person mentioned the agent on an undelegated issue; answer once",
    "gate_ready_for_human": "triage marked the issue for a person; create no card",
    "gate_not_ready": "the issue has not passed the readiness check; create no card",
    "foreign_cards": "cards the factory did not create work the issue; create no card",
    "create_card": "create the issue's implementation card",
    "reuse_card": "the issue has a card; move it to this session and handle its status",
    "card_reply": "a reply on an issue with a card",
    "triage_card_reply": "a reply on an issue whose triage card is live",
    "answer_reply": "a reply on an undelegated issue with no card; answer once",
    "stop_triage_card": "stop the live triage card",
    "stop_card": "stop the issue's card",
    "nothing_to_stop": "a stop with no card to stop",
    "issue_closed": "the issue is completed, canceled, or a duplicate",
    "undelegated": "the issue is not delegated to the agent app",
    "session_created_handles_it": "a recent session a person opened handles this delegation; do nothing",
    "use_session": "the issue is delegated; handle it on `agent_session_id`, or open a session when it is null",
    "open_session": "the issue is delegated; open a session and handle it there",
    "error": "the intent could not be computed; report `error` and stop",
}
GATE_ACTIONS = {"ready-for-human": "gate_ready_for_human", "not-ready": "gate_not_ready"}
SESSION_ACTIONS = ("use_session", "open_session")


class Reply(NamedTuple):
    text: Optional[str]
    source: Optional[str]


NO_REPLY = Reply(None, None)


class Thread(NamedTuple):
    items: Optional[list]
    source: Optional[str]


NO_THREAD = Thread(None, None)


@dataclass(frozen=True)
class Intent:
    action: str
    issue: Optional[dict] = None
    delegated: bool = False
    gate: Optional[str] = None
    card: Optional[dict] = None
    triage_card: Optional[dict] = None
    foreign_cards: list = field(default_factory=list)
    session_duplicate: bool = False
    agent_session_id: Optional[str] = None
    then: Optional[str] = None
    reply: Optional[str] = None
    reply_source: Optional[str] = None
    thread: Optional[list] = None
    thread_source: Optional[str] = None
    error: Optional[str] = None


def error_intent(message: str) -> Intent:
    return Intent(action="error", error=message)


def render(intent: Intent) -> str:
    """The block that starts the turn. `<` and `>` are escaped so a reply cannot close it early."""
    body = json.dumps(asdict(intent), indent=2, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"## Computed triage intent\n\n<triage_intent>\n{body}\n</triage_intent>\n\n"


def gate_verdict(labels: list[str]) -> str:
    roles = {linear.role_key(name) for name in labels}
    if "readyforhuman" in roles:
        return "ready-for-human"
    return "pass" if "readyforagent" in roles else "not-ready"


def foreign_card_ids(cards: list[Card], issue: Mapping[str, Any]) -> list[str]:
    return [
        card.id
        for card in cards
        if card.status not in ("done", "archived")
        and not triage_exit.LINEAR_ISSUE_ID.search(card.body)
        and (issue["identifier"] in card.title or issue["identifier"] in card.body)
    ]


def find_triage_card(cards: list[Card], issue_id: str) -> Optional[Card]:
    found = [card for card in cards if card.status != "archived" and triage_exit.names_issue(card, issue_id)]
    return next((card for card in found if card.status != "done"), found[0] if found else None)


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _recent_person_session(issue: Mapping[str, Any], app_user_id: str, now: dt.datetime) -> bool:
    return any(
        s["appUserId"] == app_user_id
        and s["creatorId"] is not None
        and now - DUPLICATE_WINDOW <= parse_time(s["createdAt"]) <= now + CLOCK_SKEW
        for s in issue["agentSessions"]
    )


def _open_session(issue: Mapping[str, Any], app_user_id: str) -> Optional[str]:
    return next((s["id"] for s in issue["agentSessions"] if s["appUserId"] == app_user_id and s["status"] != "complete"), None)


def _delegated(facts: Intent) -> str:
    if facts.gate != "pass":
        return GATE_ACTIONS[facts.gate]
    if facts.card:
        return "reuse_card"
    return "foreign_cards" if facts.foreign_cards else "create_card"


def _session_created(facts: Intent, creator_id: Optional[str]) -> str:
    if facts.delegated:
        return _delegated(facts)
    return "answer_mention" if creator_id else "ignore_factory_session"


def _session_prompted(facts: Intent, stop: bool) -> str:
    if stop:
        if facts.triage_card and facts.triage_card["status"] != "done":
            return "stop_triage_card"
        return "stop_card" if facts.card else "nothing_to_stop"
    if facts.triage_card and facts.triage_card["status"] != "done":
        return "triage_card_reply"
    if facts.card:
        return "card_reply"
    return _delegated(facts) if facts.delegated else "answer_reply"


def _issue_update(facts: Intent, labels_changed: bool) -> str:
    if facts.issue["state_type"] in ("completed", "canceled", "duplicate"):
        return "issue_closed"
    if not facts.delegated:
        return "undelegated"
    if labels_changed and not facts.card:
        return GATE_ACTIONS.get(facts.gate, "use_session")
    if facts.session_duplicate:
        return "session_created_handles_it"
    return GATE_ACTIONS.get(facts.gate, "open_session")


def decide(
    payload: Mapping[str, Any],
    issue: Mapping[str, Any],
    triage_cards: list[Card],
    work_cards: list[Card],
    app_user_id: str,
    now: dt.datetime,
    reply: Reply,
    thread: Thread = NO_THREAD,
) -> Intent:
    found = triage_exit.find_card(work_cards, issue["id"], issue["identifier"], issue["gitBranchName"])
    triage_card = find_triage_card(triage_cards, issue["id"])
    delegated = bool(app_user_id) and issue["delegate"] == app_user_id
    facts = Intent(
        action="",
        issue={"id": issue["id"], "identifier": issue["identifier"], "state_type": issue["stateType"]},
        delegated=delegated,
        gate=gate_verdict(issue["labels"]),
        card={"id": found[0].id, "status": found[0].status, "matched_by": found[1]} if found else None,
        triage_card={"id": triage_card.id, "status": triage_card.status} if triage_card else None,
        foreign_cards=[] if found else foreign_card_ids(work_cards, issue),
        session_duplicate=_recent_person_session(issue, app_user_id, now),
        reply=reply.text,
        reply_source=reply.source,
        thread=thread.items,
        thread_source=thread.source,
    )
    if payload["type"] == "Issue":
        action = _issue_update(facts, "labelIds" in (payload.get("updatedFrom") or {}))
        session = _open_session(issue, app_user_id) if action == "use_session" or action in GATE_ACTIONS.values() else None
        then = _delegated(replace(facts, gate="pass")) if action in SESSION_ACTIONS else None
        return replace(facts, action=action, agent_session_id=session, then=then)
    session = payload["agentSession"]["id"]
    if payload["action"] == "created":
        action = _session_created(facts, payload["agentSession"].get("creatorId"))
    else:
        stop = (payload.get("agentActivity") or {}).get("signal") == "stop"
        action = _session_prompted(facts, stop)
    return replace(facts, action=action, agent_session_id=session)


def carries_no_reply(payload: Mapping[str, Any]) -> bool:
    return (
        payload["type"] != "AgentSessionEvent"
        or payload["action"] != "prompted"
        or (payload.get("agentActivity") or {}).get("signal") == "stop"
    )


def resolve_reply(payload: Mapping[str, Any], client: Any) -> Reply:
    if carries_no_reply(payload):
        return NO_REPLY
    activity = payload.get("agentActivity") or {}
    body = (activity.get("content") or {}).get("body")
    if isinstance(body, str):
        return Reply(body, "payload")
    try:
        found = client.read_prompt(
            agent_activity_id=activity.get("id"),
            agent_session_id=None if activity.get("id") else payload["agentSession"]["id"],
        )
    except Exception:
        return Reply(None, "missing")
    return Reply(found["body"], "linear") if found.get("found") else Reply(None, "missing")


def resolve_thread(payload: Mapping[str, Any], client: Any) -> Thread:
    if carries_no_reply(payload):
        return NO_THREAD
    reply_id = (payload.get("agentActivity") or {}).get("id")
    try:
        items = client.session_thread(payload["agentSession"]["id"])
    except Exception:
        return Thread(None, "missing")
    return Thread([item for item in items if item["id"] != reply_id], "linear")


def delivery_time(payload: Mapping[str, Any]) -> dt.datetime:
    millis = payload.get("webhookTimestamp")
    if isinstance(millis, (int, float)):
        return dt.datetime.fromtimestamp(millis / 1000, dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def _issue_id(payload: Mapping[str, Any]) -> str:
    if payload["type"] == "Issue":
        return payload["data"]["id"]
    return payload["agentSession"]["issue"]["id"]


def compute(
    payload: Any,
    *,
    app_user_id: Callable[[], str],
    client: Callable[[], Any],
    cards: Callable[[Optional[str]], list[Card]],
    now: Optional[dt.datetime] = None,
    redact: Callable[[str], str] = blocks.hermes_redact,
) -> Intent:
    """Read the issue and both boards, then decide. Any failure becomes the `error` intent, never an exception."""
    try:
        if not isinstance(payload, Mapping) or payload.get("type") not in ("Issue", "AgentSessionEvent"):
            raise ValueError("the payload is not a Linear Issue or AgentSessionEvent")
        linear_client = client()
        issue = linear_client.get_issue(_issue_id(payload))
        return decide(
            payload,
            issue,
            cards(TRIAGE_BOARD),
            cards(None),
            app_user_id(),
            now or delivery_time(payload),
            resolve_reply(payload, linear_client),
            resolve_thread(payload, linear_client),
        )
    except Exception as failure:
        return error_intent(redact(f"{type(failure).__name__}: {failure}"))
