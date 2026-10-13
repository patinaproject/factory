from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import linear

logger = logging.getLogger(__name__)

# kanban_task_blocked is not one of Hermes' timeout-bounded hooks: it runs on the thread that blocks the
# card, with no cap. This bound is the only one, and it stays under the 10 s Linear request timeout.
POST_TIMEOUT_SECONDS = 8.0
REASON_LIMIT = 500
SESSION_LINE = re.compile(r"^linear_agent_session_id:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Card:
    status: str
    block_kind: Optional[str]
    body: Optional[str]


def load_card(task_id: str, board: Optional[str]) -> Card:
    from hermes_cli import kanban_db, kanban_db_connect

    with kanban_db_connect.connect_closing(board=board) as conn:
        task = kanban_db.get_task(conn, task_id)
    return Card(task.status, task.block_kind, task.body)


def hermes_redact(text: str) -> str:
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text(text, force=True)


def session_id(body: Optional[str]) -> Optional[str]:
    match = SESSION_LINE.search(body or "")
    return match.group(1) if match else None


def activity_for(task_id: str, card: Card, reason: Optional[str], redact: Callable[[str], str]) -> Optional[dict]:
    """The activity that tells a person about a block, or None when the block needs nobody.

    The hook reports the card's committed state. A dependency wait fires the hook before its transaction
    commits, so its card still reads as running and falls through to None, as it should.
    """
    if card.status == "triage":
        cause = f" ({card.block_kind})" if card.block_kind else ""
        return _activity(
            "elicitation",
            f"Card {task_id} is parked in triage. It blocked twice in a row for the same reason{cause}, "
            "so Hermes stopped retrying it.",
            reason,
            "Reply here to resume it.",
            redact,
        )
    if card.status == "blocked" and card.block_kind == "needs_input":
        return _activity(
            "elicitation",
            f"Card {task_id} is blocked and needs a person to decide.",
            reason,
            "Reply here with your answer to resume it.",
            redact,
        )
    if card.status == "blocked" and card.block_kind == "capability":
        return _activity(
            "error",
            f"Card {task_id} is blocked: the worker lacks something it needs.",
            reason,
            "Fix the cause, then reply here to resume it.",
            redact,
        )
    return None


def _activity(type: str, headline: str, reason: Optional[str], resume: str, redact: Callable[[str], str]) -> dict:
    # Redact before truncating: a cut can split a secret into a piece no pattern recognizes.
    text = " ".join(redact(reason or "").split()) or "no reason given"
    if len(text) > REASON_LIMIT:
        text = text[:REASON_LIMIT].rstrip() + "..."
    return linear.activity_content(type, f"{headline}\n\nReason: {text}\n\n{resume}")


def make_blocked_hook(
    get_client: Callable[[], linear.LinearClient],
    load: Callable[[str, Optional[str]], Card] = load_card,
    redact: Callable[[str], str] = hermes_redact,
) -> Callable[..., None]:
    def post(task_id: str, board: Optional[str], reason: Optional[str]) -> None:
        try:
            card = load(task_id, board)
            content = activity_for(task_id, card, reason, redact)
            if content is None:
                return
            agent_session = session_id(card.body)
            if agent_session is None:
                logger.warning("linear-agent-session: card %s has no linear_agent_session_id; block not reported", task_id)
                return
            get_client().create_activity(agent_session, content)
        except Exception:
            logger.exception("linear-agent-session: reporting the block of card %s failed", task_id)

    def on_blocked(task_id: str, board: Optional[str] = None, reason: Optional[str] = None, **_: Any) -> None:
        worker = threading.Thread(target=post, args=(task_id, board, reason), daemon=True)
        worker.start()
        worker.join(POST_TIMEOUT_SECONDS)
        if worker.is_alive():
            logger.warning("linear-agent-session: reporting the block of card %s is still running; moving on", task_id)

    return on_blocked
