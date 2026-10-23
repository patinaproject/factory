from __future__ import annotations

import collections
import json
import logging
import re
import threading
from typing import Any, Callable, Optional

from . import blocks, linear

logger = logging.getLogger(__name__)

# post_llm_call is bounded by nothing here, so the post carries its own bound, as in blocks.py.
POST_TIMEOUT_SECONDS = blocks.POST_TIMEOUT_SECONDS
RESPONSE_LIMIT = 4000
# One entry per Hermes session with a post in its current turn. The turn-end hook clears it.
RECORD_LIMIT = 1024
NO_ANSWER = "Your reply arrived, but no answer was produced. Please send it again."


def _event_line(name: str) -> "re.Pattern[str]":
    return re.compile(rf"^- {re.escape(name)}: (.*?)[ \t]*$", re.MULTILINE)


TYPE_LINE = _event_line("Type")
ACTION_LINE = _event_line("Action")
SESSION_LINE = _event_line("Agent session")
SIGNAL_LINE = _event_line("Activity signal")


class PostRecord:
    """The agent sessions that each Hermes session posted a visible activity to this turn."""

    def __init__(self, limit: int = RECORD_LIMIT) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._posted: "collections.OrderedDict[str, set[str]]" = collections.OrderedDict()

    def add(self, hermes_session: str, agent_session: str) -> None:
        with self._lock:
            self._posted.setdefault(hermes_session, set()).add(agent_session)
            self._posted.move_to_end(hermes_session)
            while len(self._posted) > self._limit:
                self._posted.popitem(last=False)

    def take(self, hermes_session: str) -> set[str]:
        with self._lock:
            return self._posted.pop(hermes_session, set())


def make_activity_handler(
    record: PostRecord, post: Callable[[dict], str]
) -> Callable[..., str]:
    def handler(args: dict, session_id: Optional[str] = None, **_: Any) -> str:
        result = post(args)
        try:
            ok = json.loads(result).get("ok") is True
        except ValueError:
            ok = False
        if ok and session_id and not args.get("ephemeral") and args.get("agent_session_id"):
            record.add(session_id, args["agent_session_id"])
        return result

    return handler


def reply_session(user_message: Optional[str]) -> Optional[str]:
    """The agent session of a prompted, non-stop event, read from the first lines of the prompt's event block."""
    text = user_message if isinstance(user_message, str) else ""
    first = lambda pattern: (m.group(1) if (m := pattern.search(text)) else None)
    if first(TYPE_LINE) != "AgentSessionEvent" or first(ACTION_LINE) != "prompted":
        return None
    if first(SIGNAL_LINE) == "stop":
        return None
    session = first(SESSION_LINE)
    return session if session and not session.startswith("{") else None


def make_turn_hook(
    record: PostRecord,
    get_client: Callable[[], linear.LinearClient],
    redact: Callable[[str], str] = blocks.hermes_redact,
) -> Callable[..., None]:
    def post(agent_session: str, text: str) -> None:
        try:
            # Redact before clipping: a cut can split a secret into a piece no pattern recognizes.
            body = blocks._clip(redact(text).strip(), RESPONSE_LIMIT) if text and text.strip() else NO_ANSWER
            get_client().create_activity(agent_session, linear.activity_content("response", body))
        except Exception as failure:
            logger.error(
                "linear-agent-session: answering a reply in session %s failed: %s",
                agent_session,
                redact(f"{type(failure).__name__}: {failure}"),
            )

    def on_turn_end(
        session_id: Optional[str] = None,
        user_message: Optional[str] = None,
        assistant_response: Optional[str] = None,
        **_: Any,
    ) -> None:
        try:
            posted = record.take(session_id) if session_id else set()
            agent_session = reply_session(user_message)
            if agent_session is None or agent_session in posted:
                return
            worker = threading.Thread(target=post, args=(agent_session, assistant_response or ""), daemon=False)
            worker.start()
            worker.join(POST_TIMEOUT_SECONDS)
            if worker.is_alive():
                logger.warning("linear-agent-session: answering a reply in session %s is still running; moving on", agent_session)
        except Exception:
            logger.exception("linear-agent-session: the reply guarantee failed")

    return on_turn_end
