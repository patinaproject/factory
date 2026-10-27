from __future__ import annotations

import collections
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import blocks, linear

logger = logging.getLogger(__name__)

# post_llm_call is bounded by nothing here, so the post carries its own bound, as in blocks.py.
POST_TIMEOUT_SECONDS = blocks.POST_TIMEOUT_SECONDS
# One entry per Hermes session with a post in its current turn. The turn-end hook clears it.
RECORD_LIMIT = 1024
ASK_AGAIN = (
    "Your reply reached the factory, but its agent did not answer it. "
    "Please send it again, and add anything it needs to act on it."
)
REASK_FIRST_SENTENCE = "This is a replay of a reply that a person sent in this Linear agent session at "
REASK_PREAMBLE = (
    REASK_FIRST_SENTENCE + "{sent}. "
    "The turn that handled it then posted nothing to the session, so the person never got an answer.\n"
    "A `prompted` event is always a person's reply, whoever opened the session. For this event, ignore any rule below "
    "that says to post no activity: before you stop, post exactly one activity on this agent session. Answer the reply "
    "in a `response`, from the reply, the issue, and its current state. When you need something from the person to go "
    "on, ask for it in an `elicitation` instead. If the issue has changed since the reply so that it no longer needs "
    "an answer, say so briefly in the `response`. Change cards only as the rules below allow.\n\n"
)
REASK_SOURCE = "linear-reply-reask"


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


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def route_toolsets(config: dict, route: str) -> str:
    """The comma-joined toolsets of a webhook route, as Hermes' config holds them."""
    value = config["platforms"]["webhook"]["extra"]["routes"][route].get("toolsets") or ""
    return ",".join(value) if isinstance(value, list) else str(value)


def find_hermes() -> str:
    candidates = [os.environ.get("HERMES_BIN"), shutil.which("hermes"), str(Path.home() / ".local" / "bin" / "hermes")]
    for candidate in filter(None, candidates):
        if os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("hermes is not on PATH or in ~/.local/bin; set HERMES_BIN")


def hermes_home() -> str:
    if os.environ.get("HERMES_HOME"):
        return os.environ["HERMES_HOME"]
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


# The outer shell exits at once and the inner one is reparented, so the re-ask outlives a parent that exits first,
# such as a one-shot replay, leaves no zombie behind, and still removes its query once Hermes exits.
DETACH = '("$@"; rm -rf "$0") </dev/null >/dev/null 2>&1 &'


def spawn_reask(query: str, toolsets: str, redact: Callable[[str], str] = blocks.hermes_redact) -> None:
    """Start a fresh agent turn on the query in its own detached Hermes process."""
    directory: Optional[str] = None
    try:
        command = [find_hermes(), "chat", "-Q", "--oneshot", "--query-file"]
        directory = tempfile.mkdtemp(prefix="linear-reask-")
        path = os.path.join(directory, "query.md")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(query)
        command.append(path)
        if toolsets:
            command += ["-t", toolsets]
        command += ["--source", REASK_SOURCE]
        subprocess.run(
            ["/bin/sh", "-c", DETACH, directory, *command],
            env={**os.environ, "HERMES_HOME": hermes_home()},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except Exception as failure:
        logger.error("linear-agent-session: the reply re-ask failed: %s", redact(f"{type(failure).__name__}: {failure}"))
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


def make_turn_hook(
    record: PostRecord,
    get_client: Callable[[], linear.LinearClient],
    reask: Callable[[str, str], Any],
    toolsets: Callable[[], str],
    redact: Callable[[str], str] = blocks.hermes_redact,
) -> Callable[..., None]:
    def ask_again(agent_session: str) -> None:
        try:
            get_client().create_activity(agent_session, linear.activity_content("elicitation", ASK_AGAIN))
        except Exception as failure:
            logger.error(
                "linear-agent-session: asking for a reply again in session %s failed: %s",
                agent_session,
                redact(f"{type(failure).__name__}: {failure}"),
            )

    def on_turn_end(
        session_id: Optional[str] = None,
        user_message: Optional[str] = None,
        **_: Any,
    ) -> None:
        try:
            posted = record.take(session_id) if session_id else set()
            agent_session = reply_session(user_message)
            if agent_session is None or agent_session in posted:
                return
            if not user_message.startswith(REASK_FIRST_SENTENCE):
                reask(REASK_PREAMBLE.format(sent=utc_now()) + user_message, toolsets())
                return
            worker = threading.Thread(target=ask_again, args=(agent_session,), daemon=False)
            worker.start()
            worker.join(POST_TIMEOUT_SECONDS)
            if worker.is_alive():
                logger.warning("linear-agent-session: asking for a reply again in session %s is still running; moving on", agent_session)
        except Exception:
            logger.exception("linear-agent-session: the reply guarantee failed")

    return on_turn_end
