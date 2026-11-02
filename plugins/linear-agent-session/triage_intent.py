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
    "gate_needs_brief": "the issue carries the ready label but has no Agent brief; start a triage pass and create no card",
    "gate_start_triage": "the issue has not passed the readiness check and a person delegated it or replied; start a triage pass and create no implementation card",
    "gate_not_ready": "an update that is not a delegation found the issue not ready; create no card",
    "foreign_cards": "cards the factory did not create work the issue; create no card",
    "create_card": "create the issue's implementation card",
    "reuse_card": "the issue has a card; move it to this session and handle its status",
    "card_reply": "the issue's assignee, its delegator, or a configured operator replied on an issue with a card",
    "card_reply_not_assignee": "someone who is none of the issue's assignee, delegator, or configured operators replied on an issue with a card; wake no card",
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
REPLY_AUTHOR_KINDS = {
    "person": "the reply's author is a person",
    "app": "the reply's author is an app account; wake no card",
    "unknown": "the factory could not tell whether the author is a person or an app; wake no card",
}
GATE_ACTIONS = {
    "ready-for-human": "gate_ready_for_human",
    "needs-brief": "gate_needs_brief",
    "not-ready": "gate_not_ready",
}
GATE_ACTION_NAMES = {*GATE_ACTIONS.values(), "gate_start_triage"}
SESSION_ACTIONS = ("use_session", "open_session")


class Reply(NamedTuple):
    text: Optional[str]
    source: Optional[str]
    author_id: Optional[str] = None
    author_kind: Optional[str] = None


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
    reply_author_id: Optional[str] = None
    reply_author_kind: Optional[str] = None
    assignee: Optional[dict] = None
    from_assignee: Optional[bool] = None
    authorized_as: Optional[str] = None
    approvers: Optional[list] = None
    not_assignee_reason: Optional[str] = None
    thread: Optional[list] = None
    thread_source: Optional[str] = None
    error: Optional[str] = None


def error_intent(message: str) -> Intent:
    return Intent(action="error", error=message)


def render(intent: Intent) -> str:
    """The block that starts the turn. `<` and `>` are escaped so a reply cannot close it early."""
    body = json.dumps(asdict(intent), indent=2, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"## Computed triage intent\n\n<triage_intent>\n{body}\n</triage_intent>\n\n"


def gate_verdict(labels: list[str], identifier: str, document_titles: list[str]) -> str:
    roles = {linear.role_key(name) for name in labels}
    if "readyforhuman" in roles:
        return "ready-for-human"
    if "readyforagent" not in roles:
        return "not-ready"
    return "pass" if f"{identifier} Agent brief" in document_titles else "needs-brief"


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


def _gate_action(gate: str, delegation: bool) -> str:
    return "gate_start_triage" if gate == "not-ready" and delegation else GATE_ACTIONS[gate]


def _delegated(facts: Intent, delegation: bool) -> str:
    if facts.gate != "pass":
        return _gate_action(facts.gate, delegation)
    if facts.card:
        return "reuse_card"
    return "foreign_cards" if facts.foreign_cards else "create_card"


def _session_created(facts: Intent, creator_id: Optional[str]) -> str:
    if facts.delegated:
        return _delegated(facts, delegation=True)
    return "answer_mention" if creator_id else "ignore_factory_session"


def _session_prompted(facts: Intent, stop: bool) -> str:
    if stop:
        if facts.triage_card and facts.triage_card["status"] != "done":
            return "stop_triage_card"
        return "stop_card" if facts.card else "nothing_to_stop"
    if facts.triage_card and facts.triage_card["status"] != "done":
        return "triage_card_reply"
    if facts.card:
        return "card_reply" if facts.from_assignee else "card_reply_not_assignee"
    return _delegated(facts, delegation=True) if facts.delegated else "answer_reply"


def _issue_update(facts: Intent, updated_from: Mapping[str, Any]) -> str:
    if facts.issue["state_type"] in triage_exit.CLOSED_STATE_TYPES:
        return "issue_closed"
    if not facts.delegated:
        return "undelegated"
    delegation = "delegateId" in updated_from
    if "labelIds" in updated_from and not facts.card:
        return _gate_action(facts.gate, delegation) if facts.gate != "pass" else "use_session"
    if facts.session_duplicate:
        return "session_created_handles_it"
    return _gate_action(facts.gate, delegation) if facts.gate != "pass" else "open_session"


def decide(
    payload: Mapping[str, Any],
    issue: Mapping[str, Any],
    triage_cards: list[Card],
    work_cards: list[Card],
    app_user_id: str,
    now: dt.datetime,
    reply: Reply,
    thread: Thread,
    document_titles: list[str],
    approvers: list[dict],
) -> Intent:
    found = triage_exit.find_card(work_cards, issue["id"], issue["identifier"], issue["gitBranchName"])
    triage_card = find_triage_card(triage_cards, issue["id"])
    delegated = bool(app_user_id) and issue["delegate"] == app_user_id
    facts = Intent(
        action="",
        issue={"id": issue["id"], "identifier": issue["identifier"], "state_type": issue["stateType"]},
        delegated=delegated,
        gate=gate_verdict(issue["labels"], issue["identifier"], document_titles),
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
        action = _issue_update(facts, payload.get("updatedFrom") or {})
        session = _open_session(issue, app_user_id) if action == "use_session" or action in GATE_ACTION_NAMES else None
        then = _delegated(replace(facts, gate="pass"), delegation=False) if action in SESSION_ACTIONS else None
        return replace(facts, action=action, agent_session_id=session, then=then)
    session = payload["agentSession"]["id"]
    if payload["action"] == "created":
        action = _session_created(facts, payload["agentSession"].get("creatorId"))
    elif (payload.get("agentActivity") or {}).get("signal") == "stop":
        action = _session_prompted(facts, stop=True)
    else:
        facts = _with_authority(facts, approvers, reply.author_id, reply.author_kind)
        action = _session_prompted(facts, stop=False)
    return replace(facts, action=action, agent_session_id=session)


def _with_authority(facts: Intent, approvers: list[dict], author_id: Optional[str], author_kind: Optional[str]) -> Intent:
    role, reason = linear.authority(approvers, author_id, "id")
    assignee = next((person for person in approvers if person["role"] == "assignee"), None)
    return replace(
        facts,
        reply_author_id=author_id,
        reply_author_kind=author_kind,
        assignee={key: assignee[key] for key in ("id", "name", "gitHubUserId")} if assignee else None,
        from_assignee=role is not None,
        authorized_as=role,
        approvers=linear.approver_summary(approvers),
        not_assignee_reason=reason,
    )


def carries_no_reply(payload: Mapping[str, Any]) -> bool:
    return (
        payload["type"] != "AgentSessionEvent"
        or payload["action"] != "prompted"
        or (payload.get("agentActivity") or {}).get("signal") == "stop"
    )


def _kind(is_app: Any) -> str:
    if not isinstance(is_app, bool):
        return "unknown"
    return "app" if is_app else "person"


def resolve_reply(payload: Mapping[str, Any], client: Any) -> Reply:
    if carries_no_reply(payload):
        return NO_REPLY
    activity = payload.get("agentActivity") or {}
    body = (activity.get("content") or {}).get("body")
    author = activity.get("userId")
    kind = "unknown"
    if author:
        try:
            kind = _kind(client.user_is_app(author))
        except Exception:
            pass
    if isinstance(body, str) and author:
        return Reply(body, "payload", author, kind)
    try:
        found = client.read_prompt(
            agent_activity_id=activity.get("id"),
            agent_session_id=None if activity.get("id") else payload["agentSession"]["id"],
        )
    except Exception:
        found = {"found": False}
    if not author:
        author, kind = found.get("authorId"), _kind(found.get("authorApp"))
    if isinstance(body, str):
        return Reply(body, "payload", author, kind)
    return Reply(found["body"], "linear", author, kind) if found.get("found") else Reply(None, "missing", author, kind)


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
    operator_user_ids: Callable[[], list[str]],
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
        documents = linear_client.issue_documents(issue["id"])["documents"]
        app = app_user_id()
        people = [] if carries_no_reply(payload) else linear.lookup_approvers(linear_client, issue, app, operator_user_ids())
        return decide(
            payload,
            issue,
            cards(TRIAGE_BOARD),
            cards(None),
            app,
            now or delivery_time(payload),
            resolve_reply(payload, linear_client),
            resolve_thread(payload, linear_client),
            [document["title"] for document in documents],
            people,
        )
    except Exception as failure:
        return error_intent(redact(f"{type(failure).__name__}: {failure}"))
