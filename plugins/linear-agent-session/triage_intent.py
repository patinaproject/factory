"""Compute what a Linear triage turn is about, so the prompt branches on `action` instead of working it out.

The `pre_gateway_dispatch` hook puts `render(compute(...))` in front of the turn's prompt, and `cli.py triage-intent`
does the same for a `webhook-retry` replay. The routing it replaces is the card lookup, the foreign-card scan, the
delegation gate, the "Session created" duplicate check, and the reply text. The effects stay in `templates/triage-linear.md`.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Mapping, NamedTuple, Optional

from . import blocks, comment_resume, linear, triage_exit, triage_round
from .triage_exit import Card, History

TRIAGE_BOARD = triage_exit.TRIAGE_BOARD
TRIAGE_OUTCOME = "Triage outcome:"
DUPLICATE_WINDOW = dt.timedelta(minutes=5)
# Linear stamps a session and its webhook within moments of each other, in either order.
CLOCK_SKEW = dt.timedelta(minutes=1)

# Every action the prompt handles. A machine contract: `templates/tests/test_render.py` fails when the prompt omits one.
ACTIONS = {
    "ignore_factory_session": "the factory opened this session itself (delegated or not); post nothing",
    "answer_mention": "a person mentioned the agent on an undelegated issue; answer once",
    "gate_ready_for_human": "triage marked the issue for a person; create no card",
    "gate_needs_brief": "the issue carries the ready label but has no Agent brief; start a triage pass and create no card",
    "gate_start_triage": "the issue has not passed the readiness check and a person delegated it or replied; start a triage pass and create no implementation card",
    "gate_not_ready": "an update that is not a delegation found the issue not ready; create no card",
    "foreign_cards": "cards the factory did not create work the issue; create no card",
    "create_card": "create the issue's implementation card",
    "reuse_card": "the issue has a card; move it to this session and handle its status",
    "card_reply": "the issue's assignee, its delegator, or a configured operator replied on an issue with a card",
    "comment_resume": "an approver answered in a top-level issue comment while a card waits for a person; resume that card",
    "skip_comment": "a Comment that resumes no card; the hook skips it and no turn runs",
    "card_reply_not_assignee": "someone who is none of the issue's assignee, delegator, or configured operators replied on an issue with a card; wake no card",
    "triage_card_reply": "a reply on an issue whose triage card is live",
    "start_triage_with_reply": "a person answered the session's open triage questions on an undelegated issue with no card and no live triage card; start triage with the reply",
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
    "app": "the reply's author is an app account; the reply changes nothing",
    "unknown": "the factory could not read the author's account; the reply changes nothing",
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
    delegate: Optional[dict] = None
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
    comment_id: Optional[str] = None
    reply_key: Optional[str] = None
    comment_url: Optional[str] = None
    skip_reason: Optional[str] = None
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


def _session_created(facts: Intent, creator_id: Optional[str], factory_session: bool) -> str:
    if factory_session:
        return "ignore_factory_session"
    if facts.delegated:
        return _delegated(facts, delegation=True)
    return "answer_mention" if creator_id else "ignore_factory_session"


def _open_triage_round(thread: Optional[list]) -> bool:
    """True when the thread's newest triage round has no `Triage outcome:` after it."""
    open_round = False
    for item in thread or []:
        body = item["body"] or ""
        if item["type"] == "elicitation" and body.startswith(triage_round.TRIAGE_DISCLAIMER):
            open_round = True
        elif item["type"] in ("response", "thought") and body.startswith(TRIAGE_OUTCOME):
            open_round = False
    return open_round


def _answers_triage(facts: Intent) -> bool:
    return (
        facts.issue["state_type"] not in triage_exit.CLOSED_STATE_TYPES
        and facts.reply_author_kind == "person"
        and facts.thread_source == "linear"
        and _open_triage_round(facts.thread)
    )


def _session_prompted(facts: Intent, stop: bool) -> str:
    if stop:
        if facts.triage_card and facts.triage_card["status"] != "done":
            return "stop_triage_card"
        return "stop_card" if facts.card else "nothing_to_stop"
    if facts.triage_card and facts.triage_card["status"] != "done":
        return "triage_card_reply"
    if facts.card:
        return "card_reply" if facts.from_assignee else "card_reply_not_assignee"
    if facts.delegated:
        return _delegated(facts, delegation=True)
    return "start_triage_with_reply" if _answers_triage(facts) else "answer_reply"


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
    operators_configured: bool,
    factory_session: bool,
) -> Intent:
    found = triage_exit.find_card(work_cards, issue["id"], issue["identifier"], issue["gitBranchName"])
    triage_card = find_triage_card(triage_cards, issue["id"])
    delegated = bool(app_user_id) and issue["delegate"] == app_user_id
    facts = Intent(
        action="",
        issue={"id": issue["id"], "identifier": issue["identifier"], "state_type": issue["stateType"]},
        delegated=delegated,
        delegate={"id": issue["delegate"], "name": issue["delegateName"]} if issue["delegate"] else None,
        gate=gate_verdict(issue["labels"], issue["identifier"], document_titles),
        card=(
            {
                "id": found[0].id,
                "status": found[0].status,
                "matched_by": found[1],
                "session_id": blocks.session_id(found[0].body),
            }
            if found
            else None
        ),
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
        if found and action in GATE_ACTION_NAMES:
            session = blocks.session_id(found[0].body) or session
        then = _delegated(replace(facts, gate="pass"), delegation=False) if action in SESSION_ACTIONS else None
        return replace(facts, action=action, agent_session_id=session, then=then)
    session = payload["agentSession"]["id"]
    if payload["action"] == "created":
        action = _session_created(facts, payload["agentSession"].get("creatorId"), factory_session)
    elif (payload.get("agentActivity") or {}).get("signal") == "stop":
        action = _session_prompted(facts, stop=True)
    else:
        facts = _with_authority(facts, approvers, reply.author_id, reply.author_kind, operators_configured)
        facts = replace(facts, reply_key=(payload.get("agentActivity") or {}).get("id"))
        action = _session_prompted(facts, stop=False)
    return replace(facts, action=action, agent_session_id=session)


def _with_authority(
    facts: Intent, approvers: list[dict], author_id: Optional[str], author_kind: Optional[str], operators_configured: bool
) -> Intent:
    role, reason = linear.authority(approvers, author_id, "id", operators_configured or author_kind != "person")
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


def _triage_inbox_is_noop(intent: Intent, payload: Mapping[str, Any]) -> bool:
    moved = "stateId" in (payload.get("updatedFrom") or {})
    if intent.issue["state_type"] != "triage":
        return intent.triage_card is None or not moved
    return payload.get("action") != "create" and not moved


def _undelegated_needs_no_turn(intent: Intent, payload: Mapping[str, Any]) -> bool:
    card_gone = intent.card is None or intent.card["status"] == "done"
    return card_gone and intent.triage_card is None and _triage_inbox_is_noop(intent, payload)


def _reuse_card_needs_no_turn(intent: Intent, payload: Mapping[str, Any]) -> bool:
    card = intent.card
    return (
        card is not None
        and card["session_id"] is not None
        and card["session_id"] == intent.agent_session_id
        and card["status"] not in ("blocked", "triage", "done")
        and intent.issue["state_type"] != "triage"
    )


# The actions the route prompt answers by doing nothing, each with the condition that makes it so.
NO_TURN_RULES: dict[str, Callable[[Intent, Mapping[str, Any]], bool]] = {
    "ignore_factory_session": lambda intent, payload: True,
    "session_created_handles_it": _triage_inbox_is_noop,
    "issue_closed": lambda intent, payload: intent.card is None and intent.triage_card is None,
    "undelegated": _undelegated_needs_no_turn,
    "reuse_card": _reuse_card_needs_no_turn,
}


def needs_no_turn(intent: Intent, payload: Mapping[str, Any]) -> bool:
    """True when the prompt would do nothing for this intent, so the hook can skip the turn."""
    rule = NO_TURN_RULES.get(intent.action)
    return rule is not None and rule(intent, payload)


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


def _factory_session(payload: Mapping[str, Any], opened_by_factory: Callable[[str], bool]) -> bool:
    if payload["type"] != "AgentSessionEvent" or payload["action"] != "created":
        return False
    try:
        return opened_by_factory(payload["agentSession"]["id"])
    except Exception:
        return False


def skip_intent(reason: str) -> Intent:
    return Intent(action="skip_comment", skip_reason=reason)


def comment_intent(
    payload: Mapping[str, Any],
    *,
    app_user_id: Callable[[], str],
    operator_user_ids: Callable[[], list[str]],
    client: Callable[[], Any],
    cards: Callable[[Optional[str]], list[Card]],
    history: Callable[[Optional[str], str], History],
) -> Intent:
    comment = comment_resume.parse(payload)
    if isinstance(comment, str):
        return skip_intent(comment)
    linear_client = client()
    kind = "unknown"
    if comment.author_id:
        try:
            kind = _kind(linear_client.user_is_app(comment.author_id))
        except Exception:
            pass
    if kind != "person":
        return skip_intent(f"the comment's author is {kind}, not a person")
    issue = linear_client.get_issue(comment.issue_id)
    app = app_user_id()
    delegated = bool(app) and issue["delegate"] == app
    work = triage_exit.find_card(cards(None), issue["id"], issue["identifier"], issue["gitBranchName"])
    waiting = comment_resume.waiting_card(
        find_triage_card(cards(TRIAGE_BOARD), issue["id"]),
        work[0] if work else None,
        delegated,
        lambda task_id: history(TRIAGE_BOARD, task_id),
    )
    if waiting is None:
        return skip_intent("no card of the issue waits for a person")
    operators = operator_user_ids()
    people = linear.lookup_approvers(linear_client, issue, app, operators)
    facts = _with_authority(Intent(action=""), people, comment.author_id, kind, bool(operators))
    if not facts.from_assignee:
        return skip_intent("the comment's author is not an approver")
    mentioned = comment_resume.profile_slugs(comment.body)
    if mentioned and (comment_resume.profile_slug(linear_client.user_profile_url(app)) or "").lower() not in mentioned:
        return skip_intent("the comment mentions people and not the agent app")
    card = {"id": waiting.card.id, "status": waiting.card.status}
    return replace(
        facts,
        action="comment_resume",
        issue={"id": issue["id"], "identifier": issue["identifier"], "state_type": issue["stateType"]},
        delegated=delegated,
        card=None if waiting.kind == "triage" else {**card, "matched_by": work[1]},
        triage_card=card if waiting.kind == "triage" else None,
        agent_session_id=blocks.session_id(waiting.card.body) or _open_session(issue, app),
        reply=comment.body,
        reply_source="payload",
        comment_id=comment.id,
        reply_key=comment.id,
        comment_url=comment_resume.comment_url(comment, issue.get("url")),
    )


# Linear can serve a delegate-less issue for a few seconds after the delegation that opened the session.
DELEGATE_REREAD_DELAYS = (1, 1, 2, 2, 4)


def _delegation_may_be_pending(payload: Mapping[str, Any], issue: Mapping[str, Any]) -> bool:
    return (
        payload["type"] == "AgentSessionEvent"
        and payload["action"] == "created"
        and payload["agentSession"].get("creatorId") is not None
        and issue["delegate"] is None
    )


def compute(
    payload: Any,
    *,
    app_user_id: Callable[[], str],
    operator_user_ids: Callable[[], list[str]],
    client: Callable[[], Any],
    cards: Callable[[Optional[str]], list[Card]],
    now: Optional[dt.datetime] = None,
    history: Callable[[Optional[str], str], History] = triage_exit.kanban_history,
    redact: Callable[[str], str] = blocks.hermes_redact,
    opened_by_factory: Callable[[str], bool] = lambda _id: False,
    sleep: Callable[[float], None] = time.sleep,
) -> Intent:
    """Read the issue and both boards, then decide. Any failure becomes the `error` intent, never an exception."""
    try:
        if not isinstance(payload, Mapping) or payload.get("type") not in ("Issue", "AgentSessionEvent", "Comment"):
            raise ValueError("the payload is not a Linear Issue, AgentSessionEvent, or Comment")
        if payload["type"] == "Comment":
            return comment_intent(
                payload, app_user_id=app_user_id, operator_user_ids=operator_user_ids, client=client, cards=cards, history=history
            )
        linear_client = client()
        issue = linear_client.get_issue(_issue_id(payload))
        factory_session = _factory_session(payload, opened_by_factory)
        if not factory_session and _delegation_may_be_pending(payload, issue):
            for delay in DELEGATE_REREAD_DELAYS:
                sleep(delay)
                issue = linear_client.get_issue(_issue_id(payload))
                if issue["delegate"] is not None:
                    break
        documents = linear_client.issue_documents(issue["id"])["documents"]
        app = app_user_id()
        no_reply = carries_no_reply(payload)
        operators = [] if no_reply else operator_user_ids()
        people = [] if no_reply else linear.lookup_approvers(linear_client, issue, app, operators)
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
            bool(operators),
            factory_session,
        )
    except Exception as failure:
        return error_intent(redact(f"{type(failure).__name__}: {failure}"))
