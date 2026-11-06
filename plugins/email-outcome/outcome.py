"""`email_record_outcome`: the mail run's one write. It writes one Linear outcome, records it in the ledger, and
completes the card, in that order, and refuses a second call for the same card."""
from __future__ import annotations

import json
import re
from typing import Any, Callable, NamedTuple, Optional

UNTRUSTED_HEADING = "**Untrusted email content**"
FENCE = re.compile(r"`{3,}")

SCHEMA = {
    "name": "email_record_outcome",
    "description": (
        "Record this email's one outcome and finish the card. `outcome` is `new-issue` (with `title` and "
        "`description`), `duplicate` (with `issue`, the identifier of the existing issue, and `comment`), or "
        "`no-action` (with `reason`, one line). Put any text copied from the email in `excerpt`, never in the "
        "other fields; it is posted below your prose in a block headed Untrusted email content. A write that "
        "holds a one-time code, a reset or sign-in link, or a credential from the email is refused; leave the "
        "value out and call again. A second call after a recorded outcome is refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["new-issue", "duplicate", "no-action"]},
            "title": {"type": "string", "description": "new-issue: the issue title, in your own words."},
            "description": {"type": "string", "description": "new-issue: what the email asks for and why it matters."},
            "issue": {"type": "string", "description": "duplicate: the existing issue's identifier, such as ABC-123."},
            "comment": {"type": "string", "description": "duplicate: what this email adds to the existing issue."},
            "reason": {"type": "string", "description": "no-action: one line on why nothing is needed."},
            "excerpt": {"type": "string", "description": "Optional. Text copied from the email."},
        },
        "required": ["outcome"],
    },
}

SEARCH_SCHEMA = {
    "name": "email_issue_search",
    "description": "Search Linear issues for `query`. Returns up to 10 issues with identifier, title, url, team, and state.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


class Refused(Exception):
    pass


class Context(NamedTuple):
    ledger: Any
    ledger_module: Any
    client: Callable[[], Any]
    card_id: Callable[[], Optional[str]]
    complete: Callable[[str, str], None]
    secret_refusal: Callable[[str, str], Optional[str]]
    team_key: str


def untrusted_block(excerpt: str) -> str:
    longest = max((len(match.group(0)) for match in FENCE.finditer(excerpt)), default=2)
    fence = "`" * (longest + 1)
    return f"{UNTRUSTED_HEADING}\n\n{fence}text\n{excerpt}\n{fence}"


def compose(prose: str, excerpt: Optional[str]) -> str:
    prose = (prose or "").strip()
    if not excerpt or not excerpt.strip():
        return prose
    return f"{prose}\n\n{untrusted_block(excerpt.strip())}"


def email_text(delivery) -> str:
    return "\n".join([*(str(value) for value in delivery.headers.values()), delivery.text])


def _text(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{name} is required for outcome {args.get('outcome')}")
    return value.strip()


def plan(args: dict) -> tuple:
    """The outcome kind, the Linear write it needs as (title, body, issue), and every string it would write."""
    kind = args.get("outcome")
    excerpt = args.get("excerpt") if isinstance(args.get("excerpt"), str) else None
    if kind == "new-issue":
        title, body = _text(args, "title"), compose(_text(args, "description"), excerpt)
        return kind, (title, body, None), [title, body]
    if kind == "duplicate":
        issue, body = _text(args, "issue").upper(), compose(_text(args, "comment"), excerpt)
        return kind, (None, body, issue), [issue, body]
    if kind == "no-action":
        reason = _text(args, "reason")
        return kind, None, [reason, excerpt or ""]
    raise Refused("outcome must be new-issue, duplicate, or no-action")


def record(context: Context, args: dict) -> dict:
    card_id = context.card_id()
    if not card_id:
        raise Refused("a mail run must run as a Kanban card")
    row = context.ledger.by_card(card_id)
    if row is None:
        raise Refused(f"card {card_id} has no email in the ledger")
    if row.outcome is not None:
        raise Refused(f"this email's outcome is already recorded: {row.outcome}")
    if row.state != context.ledger_module.READY:
        raise Refused(f"this email's files are not ready; its fetch state is {row.state}")
    kind, write, texts = plan(args if isinstance(args, dict) else {})
    source = email_text(row.delivery)
    for text in texts:
        reason = context.secret_refusal(text, source)
        if reason is not None:
            raise Refused(f"the write was refused because {reason}. Leave that value out and call again.")
    pending = context.ledger_module.PENDING + kind
    if not context.ledger.set_outcome(card_id, None, pending):
        raise Refused("this email's outcome is already recorded")
    try:
        if kind == "new-issue":
            title, body, _ = write
            created = context.client().create_issue(context.team_key, title, body, state_type="triage")
            value = created["identifier"]
        elif kind == "duplicate":
            _, body, issue = write
            value = context.client().get_issue(issue)["identifier"]
            context.client().comment_on_issue(value, body)
        else:
            value = texts[0]
        final = context.ledger_module.format_outcome(kind, value)
    except Exception:
        context.ledger.set_outcome(card_id, pending, None)
        raise
    context.ledger.set_outcome(card_id, pending, final)
    context.complete(card_id, final)
    return {"ok": True, "outcome": final}


def handler(context: Context) -> Callable:
    def handle(args: dict, **_: Any) -> str:
        try:
            return json.dumps(record(context, args))
        except Refused as refused:
            return json.dumps({"ok": False, "error": str(refused)})
        except Exception as error:
            return json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"})

    return handle


def search_handler(client: Callable[[], Any]) -> Callable:
    def handle(args: dict, **_: Any) -> str:
        query = args.get("query") if isinstance(args, dict) else None
        if not isinstance(query, str) or not query.strip():
            return json.dumps({"ok": False, "error": "pass query, the words to search issues for"})
        try:
            return json.dumps({"issues": client().search_issues(query.strip())})
        except Exception as error:
            return json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"})

    return handle
