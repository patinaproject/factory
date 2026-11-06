"""Forward a person's Linear reply to its Kanban card with no model-written shell command.

Hermes flags a reply piped through a heredoc as dangerous, so a single-query turn refuses it and a gateway webhook
turn waits for an approval. Instead, the dispatch hook, and `cli.py triage-intent` for a `webhook-retry` replay,
record the reply of each forwarding intent under its `reply_key`: the prompt's agent activity id, or the comment's id.
The `linear_card_reply` tool then runs `card_transition.py` from an argv list with no shell, the stored reply on its
standard input, so the model never writes the reply into a command.

This store alone decides whether a reply was applied. The webhook-retry queue and the comment-resume record do not.

The recovery rule: an attempt is counted before `card_transition.py` runs, so a crash leaves the record `received`
with that attempt spent, and the next call for the key, from a redelivery, a replay, or a re-ask, runs it again until
FORWARD_ATTEMPTS are spent. A second run after a partial one is safe, because `card_transition.py` converges: it writes
a reply it already wrote once, and a release of a released card is a no-op. Once a run succeeds the record is
`applied`, and every later call returns that run's result without running the `blocked` and `triage` branches again,
which would move the card a second time.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Union

from . import blocks, linear, replies

logger = logging.getLogger(__name__)

NAME = "linear_card_reply"
FORWARD_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
# A `triage` card's resume runs Hermes' `kanban specify`, which asks the model.
TRANSITION_TIMEOUT_SECONDS = 300
TRIAGE_BOARD = "triage"
RECEIVED, APPLIED = "received", "applied"
STAGES = {
    "record": "the factory holds no recorded reply for this delivery",
    "task": "the call named no card, or a card that is not the reply's",
    "attempts": f"the reply's {FORWARD_ATTEMPTS} attempts are spent",
    "transition": "card_transition.py failed",
}
RECOVERY = {
    "prompt": "Send the reply again in this session. A new reply is forwarded on its own.",
    "comment": "Add the comment again on the issue. A new comment is forwarded on its own.",
}

SCHEMA = {
    "name": NAME,
    "description": (
        "Forward the reply that the computed triage intent's reply_key names to its Kanban card. The factory stored "
        "the reply's text when the delivery arrived, so pass only the key and the card's task_id. It writes one "
        "`Linear reply:` comment and releases, unblocks, or resumes the card, and returns card_transition.py's result "
        "(action unblocked, resumed, or none, with waits_for or status). A repeated call returns the first result. On "
        "error it has already posted the error on the agent session, or on the card when there is no session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reply_key": {"type": "string", "description": "Required. The computed triage intent's reply_key."},
            "task_id": {
                "type": "string",
                "description": "Required. The card's task id: the triage card, the implementation card, or the new triage card.",
            },
            "agent_session_id": {
                "type": "string",
                "description": "The intent's agent_session_id, where the error goes when the factory holds no reply.",
            },
        },
        "required": ["reply_key", "task_id"],
    },
}


@dataclass(frozen=True)
class Forward:
    key: str
    text: str
    operation: str
    board: Optional[str]
    task_id: Optional[str]
    agent_session_id: Optional[str]
    issue: Optional[str]
    source: str
    received_at: str
    state: str = RECEIVED
    attempts: int = 0
    result: Optional[dict] = None
    last_error: Optional[str] = None


@dataclass(frozen=True)
class Applied:
    forward: Forward
    repeat: bool


@dataclass(frozen=True)
class Refused:
    stage: str
    reason: str
    forward: Optional[Forward]


class TransitionFailed(Exception):
    pass


Runner = Callable[[Forward], dict]


def store_dir(environ: Mapping[str, str]) -> Optional[str]:
    if environ.get("HERMES_HOME"):
        return os.path.join(environ["HERMES_HOME"], "plugin-data", "linear-agent-session", "forwards")
    return None


def forward_for(intent: Any, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> Optional[Forward]:
    """The record a forwarding intent receives, or None when the intent forwards no reply."""
    if intent.reply_key is None or intent.reply is None:
        return None
    if intent.action == "triage_card_reply" and intent.reply_author_kind == "person":
        target = (TRIAGE_BOARD, "triage-reply", intent.triage_card["id"], "prompt")
    elif intent.action == "comment_resume" and intent.triage_card is not None:
        target = (TRIAGE_BOARD, "triage-reply", intent.triage_card["id"], "comment")
    elif intent.action == "card_reply" and intent.card is not None and intent.card["status"] == "scheduled":
        target = (None, "implementation-reply", intent.card["id"], "prompt")
    elif intent.action == "start_triage_with_reply":
        target = (TRIAGE_BOARD, "triage-reply", None, "prompt")
    else:
        return None
    board, operation, task_id, source = target
    return Forward(
        key=intent.reply_key,
        text=intent.reply,
        operation=operation,
        board=board,
        task_id=task_id,
        agent_session_id=intent.agent_session_id,
        issue=(intent.issue or {}).get("identifier"),
        source=source,
        received_at=now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _path(directory: str, key: str, suffix: str) -> str:
    return os.path.join(directory, hashlib.sha256(key.encode()).hexdigest() + suffix)


def read(directory: str, key: str) -> Optional[Forward]:
    try:
        with open(_path(directory, key, ".json"), encoding="utf-8") as handle:
            return Forward(**json.load(handle))
    except FileNotFoundError:
        return None


def _write(directory: str, forward: Forward) -> None:
    path = _path(directory, forward.key, ".json")
    fd = os.open(path + ".tmp", os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(forward), handle)
    os.replace(path + ".tmp", path)


def receive(directory: Optional[str], intent: Any) -> Optional[Forward]:
    """Record the intent's reply when it forwards one. An existing record is kept as it is, so a redelivery or a
    replay never resets its attempts or state. Takes no lock, so it never waits behind a running forward."""
    forward = forward_for(intent)
    if forward is None:
        return None
    if directory is None:
        raise linear.LinearError("HERMES_HOME is not set, so there is no reply store")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = _path(directory, forward.key, ".json")
    staged = f"{path}.{os.getpid()}.new"
    fd = os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(forward), handle)
        os.link(staged, path)
    except FileExistsError:
        return read(directory, forward.key)
    finally:
        os.unlink(staged)
    return forward


def pending(directory: Optional[str], key: Optional[str]) -> bool:
    """True when a reply is recorded under the key and not yet applied."""
    if directory is None or key is None:
        return False
    forward = read(directory, key)
    return forward is not None and forward.state != APPLIED


@contextlib.contextmanager
def _locked(directory: str, key: str) -> Iterator[None]:
    fd = os.open(_path(directory, key, ".lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def apply(
    directory: Optional[str], key: str, task_id: Optional[str], run: Runner, sleep: Callable[[float], None] = time.sleep
) -> Union[Applied, Refused]:
    if directory is None:
        return Refused("record", "HERMES_HOME is not set, so there is no reply store", None)
    if not os.path.isdir(directory):
        return Refused("record", f"no reply is recorded under {key}", None)
    with _locked(directory, key):
        forward = read(directory, key)
        if forward is None:
            return Refused("record", f"no reply is recorded under {key}", None)
        if forward.state == APPLIED:
            return Applied(forward, repeat=True)
        if not task_id:
            return Refused("task", "the call named no task_id", forward)
        if forward.task_id is None:
            forward = replace(forward, task_id=task_id)
            _write(directory, forward)
        elif forward.task_id != task_id:
            return Refused("task", f"the reply belongs to card {forward.task_id}, not {task_id}", forward)
        if forward.attempts >= FORWARD_ATTEMPTS:
            return Refused("attempts", f"all {FORWARD_ATTEMPTS} attempts are spent; the last failed: {forward.last_error}", forward)
        while forward.attempts < FORWARD_ATTEMPTS:
            forward = replace(forward, attempts=forward.attempts + 1)
            _write(directory, forward)
            try:
                result = run(forward)
            except TransitionFailed as failure:
                forward = replace(forward, last_error=str(failure))
                _write(directory, forward)
                if forward.attempts < FORWARD_ATTEMPTS:
                    sleep(RETRY_DELAY_SECONDS)
                continue
            forward = replace(forward, state=APPLIED, result=result, last_error=None)
            _write(directory, forward)
            return Applied(forward, repeat=False)
        return Refused("transition", forward.last_error or "card_transition.py failed", forward)


def card_transition(home: str) -> Runner:
    script = str(Path(home) / "profiles" / "claude-worker" / "scripts" / "card_transition.py")

    def run(forward: Forward) -> dict:
        board = ["--board", forward.board] if forward.board else []
        try:
            proc = subprocess.run(
                [sys.executable or "python3", script, *board, forward.operation, forward.task_id],
                input=forward.text,
                capture_output=True,
                text=True,
                shell=False,
                timeout=TRANSITION_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise TransitionFailed(f"card_transition.py ran longer than {TRANSITION_TIMEOUT_SECONDS} s") from None
        except OSError as failure:
            raise TransitionFailed(f"card_transition.py did not start: {failure}") from None
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        try:
            output = json.loads(lines[-1]) if lines else None
        except ValueError:
            output = None
        if not isinstance(output, dict):
            stderr = [line for line in proc.stderr.splitlines() if line.strip()]
            raise TransitionFailed(f"card_transition.py exited {proc.returncode}: {stderr[-1] if stderr else 'no output'}")
        if proc.returncode != 0 or "error" in output:
            raise TransitionFailed(str(output.get("error") or f"card_transition.py exited {proc.returncode}"))
        return output

    return run


def comment_on_card(board: Optional[str], task_id: str, body: str) -> None:
    kanban = ["--board", board] if board else []
    subprocess.run(
        [replies.find_hermes(), "kanban", *kanban, "comment", task_id, body],
        capture_output=True,
        text=True,
        shell=False,
        check=True,
        timeout=blocks.POST_TIMEOUT_SECONDS,
    )


def error_body(refused: Refused, task_id: Optional[str], redact: Callable[[str], str]) -> str:
    forward = refused.forward
    card = (forward.task_id if forward else None) or task_id
    where = f"card {card}" if card else "its card"
    recovery = RECOVERY[forward.source if forward else "prompt"]
    return (
        f"The reply did not reach {where}, so the card did not resume. "
        f"Stage `{refused.stage}`: {STAGES[refused.stage]}: {redact(refused.reason)}. {recovery}"
    )


def make_handler(
    get_client: Callable[[], linear.LinearClient],
    posts: replies.PostRecord,
    environ: Mapping[str, str] = os.environ,
    run: Optional[Runner] = None,
    comment: Callable[[Optional[str], str, str], None] = comment_on_card,
    sleep: Callable[[float], None] = time.sleep,
    redact: Callable[[str], str] = blocks.hermes_redact,
) -> Callable[..., str]:
    def report(refused: Refused, key: str, args: dict, session_id: Optional[str]) -> str:
        logger.warning(
            "linear-agent-session: reply forward %s failed at %s: %s", key, refused.stage, redact(refused.reason)
        )
        forward = refused.forward
        agent_session = forward.agent_session_id if forward else args.get("agent_session_id")
        task_id = (forward.task_id if forward else None) or args.get("task_id")
        body = error_body(refused, args.get("task_id"), redact)
        posted = None
        try:
            if agent_session:
                get_client().create_activity(agent_session, linear.activity_content("error", body))
                replies.mark_handled(redact)
                if session_id:
                    posts.add(session_id, agent_session)
                posted = f"agent session {agent_session}"
            elif task_id:
                comment(forward.board if forward else TRIAGE_BOARD, task_id, body)
                posted = f"card {task_id}"
        except Exception as failure:
            logger.error(
                "linear-agent-session: reply forward %s: posting the failure failed: %s",
                key,
                redact(f"{type(failure).__name__}: {failure}"),
            )
        return json.dumps({
            "error": body,
            "stage": refused.stage,
            "posted": posted,
            "next": (
                f"The error is already posted on {posted}. Post nothing that says the card resumed, and stop."
                if posted
                else "The error could not be posted. Post it as an `error` activity yourself, say nothing resumed, and stop."
            ),
        })

    def handler(args: dict, session_id: Optional[str] = None, **_: Any) -> str:
        key = args.get("reply_key")
        if not isinstance(key, str) or not key:
            return report(Refused("record", "the call named no reply_key", None), str(key), args, session_id)
        directory = store_dir(environ)
        runner = run or card_transition(environ.get("HERMES_HOME", ""))
        outcome = apply(directory, key, args.get("task_id"), runner, sleep)
        if isinstance(outcome, Refused):
            return report(outcome, key, args, session_id)
        return json.dumps({**outcome.forward.result, "task_id": outcome.forward.task_id, "repeat": outcome.repeat})

    return handler
