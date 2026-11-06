"""`email_record_outcome`: the mail run's one write. It writes one Linear outcome, records it in the ledger, and
completes the card, in that order, and refuses a second call for the same card."""
from __future__ import annotations

import json
import re
from pathlib import Path
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
    attachments_dir: Callable[[str], Path]
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


def email_text(delivery, raw: Path) -> str:
    """Everything the run could copy from: the headers, the text, and the full MIME message on disk."""
    message = raw.read_text(encoding="utf-8", errors="replace") if raw.is_file() else ""
    return "\n".join([*(str(value) for value in delivery.headers.values()), delivery.text, message])


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
    lm = context.ledger_module
    card_id = context.card_id()
    if not card_id:
        raise Refused("a mail run must run as a Kanban card")
    row = context.ledger.by_card(card_id)
    if row is None:
        raise Refused(f"card {card_id} has no email in the ledger")
    if row.outcome is not None:
        if not row.outcome.startswith(lm.PENDING):
            # The outcome landed but the card did not finish; finishing it again writes nothing to Linear.
            context.complete(card_id, row.outcome)
        raise Refused(f"this email's outcome is already recorded: {row.outcome}")
    if row.state != lm.READY:
        raise Refused(f"this email's files are not ready; its fetch state is {row.state}")
    kind, write, texts = plan(args if isinstance(args, dict) else {})
    if kind == lm.NEW_ISSUE and not context.team_key:
        raise Refused("plugins.entries.email-outcome.settings.team_key is not set, so no issue can be filed")
    try:
        final = lm.format_outcome(kind, texts[0]) if kind == lm.NO_ACTION else None
    except ValueError as error:
        raise Refused(str(error)) from None
    source = email_text(row.delivery, Path(context.attachments_dir(card_id)) / lm.RAW_FILE)
    for text in texts:
        reason = context.secret_refusal(text, source)
        if reason is not None:
            raise Refused(f"the write was refused because {reason}. Leave that value out and call again.")
    if kind == lm.DUPLICATE:
        _, body, issue = write
        issue = context.client().get_issue(issue)["identifier"]
    pending = lm.PENDING + kind
    if not context.ledger.set_outcome(card_id, None, pending):
        raise Refused("this email's outcome is already recorded")
    try:
        if kind == lm.NEW_ISSUE:
            title, body, _ = write
            final = lm.format_outcome(kind, context.client().create_issue(context.team_key, title, body, state_type="triage")["identifier"])
        elif kind == lm.DUPLICATE:
            context.client().comment_on_issue(issue, body)
            final = lm.format_outcome(kind, issue)
    except Exception as error:
        # Linear may have taken the write before the error, so a retry could write twice. A person checks instead.
        raise Refused(
            f"the Linear write failed ({type(error).__name__}: {error}) and may have landed. The outcome stays "
            f"{pending} for a person to check; block the card with kanban_block and say so."
        ) from None
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
