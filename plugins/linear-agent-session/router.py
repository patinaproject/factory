"""Shadow webhook router: one routing decision for every webhook delivery and every retry, recorded and never acted on.

Records are JSON Lines, appended to `<gateway home>/webhook-router/decisions/<UTC date>.jsonl` (files 0600, directories
0700). A file older than `RETENTION_DAYS` days is pruned. Version 1 records hold exactly these fields:

- `version`: 1.
- `recorded_at`: when the record was written, UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
- `chat_id`: the delivery's `event.source.chat_id`, which is also its `webhook-retry` queue key.
- `profile`, `route`, `delivery`: the parts of `chat_id` that `queue_store.decode_key` returns, or null for all three
  when it does not decode.
- `attempt`: for a hook record, how many hook records this `chat_id` has had, counting this one, so a redelivery is
  one higher. For a retry record, the queue entry's `attempts` plus one.
- `retry`: true when `retry.py` wrote the record.
- `session_id`: the Hermes session a retry's replay started, or null. A hook record is null: its session is the one
  whose `sessions.chat_id` is `chat_id`.
- `action`: a key of `ACTIONS`.
- `then`: the action the decision leads to, or null. For a Linear intent, the intent's `then`. For a question action,
  the action a yes answer takes. For a replay, the replayed intent's action.
- `rule`: a short stable name of the code branch that chose `action`.
- `outcome`: for a hook record, what the hook returned: `skip`, `rewrite`, or `pass` (None). For a retry record, the
  queue outcome: `handled`, `succeeded`, `failed`, `rate_limited`, `gave_up`, `interrupted`, `waiting`, or `dropped`.

A record carries ids and event and action names only, never payload text.
"""
from __future__ import annotations

import base64
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from . import triage_intent

logger = logging.getLogger(__name__)

VERSION = 1
DIRECTORY = Path("webhook-router")
RETENTION_DAYS = 14
KEY_PREFIX = "webhook:v2:"
WARNING_INTERVAL_SECONDS = 60.0
MAX_PENDING = 1000
EVENT_LINE = re.compile(r"^- Event: (\S+), action (\S+)$", re.MULTILINE)
PUSH_KEYS = ("ref", "before", "after", "pusher")

LINEAR_ACTIVITY = "linear_activity"
CARD_COMMENT = "card_comment"
CARD_CREATE = "card_create"
CARD_EDIT = "card_edit"
CARD_TRANSITION = "card_transition"
CARD_ARCHIVE = "card_archive"
CARD_REPLY = "card_reply"
ISSUE_EDIT = "issue_edit"
ISSUE_CREATE = "issue_create"
ISSUE_COMMENT = "issue_comment"
SESSION_CREATE = "session_create"
EFFECTS = frozenset({
    LINEAR_ACTIVITY, CARD_COMMENT, CARD_CREATE, CARD_EDIT, CARD_TRANSITION, CARD_ARCHIVE, CARD_REPLY, ISSUE_EDIT,
    ISSUE_CREATE, ISSUE_COMMENT, SESSION_CREATE,
})


class Action(NamedTuple):
    expects: frozenset
    turn: bool
    kind: str = "decision"
    yes: Optional[str] = None


def effects(*names: str) -> frozenset:
    return frozenset(names)


INTENT_EXPECTS = {
    "ignore_factory_session": effects(),
    "answer_mention": effects(LINEAR_ACTIVITY),
    "gate_ready_for_human": effects(LINEAR_ACTIVITY),
    "gate_needs_brief": effects(CARD_CREATE, LINEAR_ACTIVITY),
    "gate_start_triage": effects(CARD_CREATE, LINEAR_ACTIVITY),
    "gate_not_ready": effects(LINEAR_ACTIVITY),
    "foreign_cards": effects(LINEAR_ACTIVITY),
    "create_card": effects(CARD_CREATE, LINEAR_ACTIVITY),
    "reuse_card": effects(CARD_COMMENT, CARD_TRANSITION, LINEAR_ACTIVITY),
    "card_reply": effects(CARD_REPLY, LINEAR_ACTIVITY),
    "comment_resume": effects(CARD_REPLY, LINEAR_ACTIVITY),
    "skip_comment": effects(),
    "card_reply_not_assignee": effects(CARD_COMMENT, LINEAR_ACTIVITY),
    "triage_card_reply": effects(CARD_REPLY, LINEAR_ACTIVITY),
    "start_triage_with_reply": effects(CARD_CREATE, CARD_REPLY, LINEAR_ACTIVITY),
    "answer_reply": effects(LINEAR_ACTIVITY),
    "stop_triage_card": effects(CARD_TRANSITION, LINEAR_ACTIVITY),
    "stop_card": effects(CARD_TRANSITION, LINEAR_ACTIVITY),
    "nothing_to_stop": effects(LINEAR_ACTIVITY),
    "issue_closed": effects(CARD_ARCHIVE),
    "undelegated": effects(CARD_ARCHIVE),
    "session_created_handles_it": effects(),
    "use_session": effects(CARD_CREATE, LINEAR_ACTIVITY),
    "open_session": effects(SESSION_CREATE, CARD_CREATE, LINEAR_ACTIVITY),
    "error": effects(LINEAR_ACTIVITY),
}
HOOK_EXITS = {
    "paused": Action(effects(), False),
    "unresolved_secret": Action(effects(), False),
    "teammate_reply": Action(effects(), False),
    "duplicate_comment": Action(effects(), False),
    "triage_exit_handled": Action(effects(), False),
    "prompt_already_claimed": Action(effects(), False),
    "no_intent": Action(effects(), True),
}
GITHUB_ACTIONS = {
    "auto_deploy": Action(effects(), False),
    "complete_merged_card": Action(effects(CARD_COMMENT, CARD_TRANSITION, LINEAR_ACTIVITY), True),
    "act_on_change_request": Action(effects(CARD_COMMENT, CARD_TRANSITION), True),
    "resume_approved_card": Action(effects(CARD_COMMENT, CARD_TRANSITION), True),
    "act_on_feedback": Action(effects(CARD_COMMENT, CARD_TRANSITION), True),
    "card_comment_only": Action(effects(CARD_COMMENT), True),
    "needs_sender_authority": Action(effects(CARD_COMMENT), True, "question"),
    "needs_change_request_judgment": Action(effects(CARD_COMMENT), True, "question", yes="act_on_feedback"),
}
RETRY_ACTIONS = {
    "retry_triage_exit_handled": Action(effects(), False),
    "retry_teammate_reply": Action(effects(), False),
    "retry_comment_skipped": Action(effects(), False),
    "replay_succeeded": Action(effects(), True, "replay"),
    "replay_failed": Action(effects(), True),
    "replay_rate_limited": Action(effects(), True),
    "retry_gave_up": Action(effects(), False),
    "retry_interrupted": Action(effects(), False),
    "retry_paused": Action(effects(), False),
    "retry_dropped": Action(effects(), False),
}
ACTIONS: dict[str, Action] = {
    **{name: Action(INTENT_EXPECTS.get(name, effects()), name != "skip_comment") for name in triage_intent.ACTIONS},
    **HOOK_EXITS,
    **GITHUB_ACTIONS,
    **RETRY_ACTIONS,
}


def acceptable(action: str, then: Optional[str]) -> Optional[set]:
    entry = ACTIONS[action]
    if entry.kind == "replay":
        return acceptable(then, None) if then in ACTIONS else None
    sets = {entry.expects}
    follow = then or entry.yes
    if entry.kind == "question" and follow in ACTIONS:
        sets |= acceptable(follow, None) or set()
    return sets


@dataclasses.dataclass
class Trace:
    home: Optional[Path] = None
    route: Optional[str] = None
    rule: Optional[str] = None
    intent: Optional[triage_intent.Intent] = None


class Decision(NamedTuple):
    action: str
    then: Optional[str]
    rule: str


def decode_key(key: Any) -> Optional[tuple]:
    if not isinstance(key, str) or not key.startswith(KEY_PREFIX):
        return None
    body = key[len(KEY_PREFIX):]
    try:
        parts = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if not (isinstance(parts, list) and len(parts) == 3 and all(isinstance(part, str) for part in parts)):
        return None
    return parts[0], parts[1], parts[2]


def is_push(payload: Any) -> bool:
    return isinstance(payload, dict) and "action" not in payload and all(key in payload for key in PUSH_KEYS)


def github_decision(payload: Any, text: Any) -> Decision:
    if is_push(payload):
        return Decision("auto_deploy", None, "push")
    payload = payload if isinstance(payload, dict) else {}
    line = EVENT_LINE.search(text) if isinstance(text, str) else None
    event = line.group(1) if line else ""
    action = payload.get("action")
    person = (payload.get("sender") or {}).get("type") != "Bot"
    if event == "pull_request" and action == "closed" and (payload.get("pull_request") or {}).get("merged") is True:
        return Decision("complete_merged_card", None, "github_merged")
    if event == "pull_request_review" and action == "submitted":
        state = str((payload.get("review") or {}).get("state") or "").lower()
        if state == "changes_requested":
            if person:
                return Decision("needs_sender_authority", "act_on_change_request", "github_change_request_person")
            return Decision("act_on_change_request", None, "github_change_request_bot")
        if state == "approved" and person:
            return Decision("needs_sender_authority", "resume_approved_card", "github_approval_person")
        if state == "commented" and person:
            return Decision("needs_sender_authority", "needs_change_request_judgment", "github_review_comment_person")
    on_pull_request = event == "issue_comment" and bool((payload.get("issue") or {}).get("pull_request"))
    if (event == "pull_request_review_comment" or on_pull_request) and person:
        return Decision("needs_sender_authority", "needs_change_request_judgment", "github_comment_person")
    return Decision("card_comment_only", None, "github_comment_only")


def decide(trace: Trace, payload: Any, text: Any) -> Decision:
    if trace.rule in HOOK_EXITS:
        return Decision(trace.rule, None, trace.rule)
    if trace.intent is not None:
        return Decision(trace.intent.action, trace.intent.then, trace.rule or "intent")
    return github_decision(payload, text)


def outcome_of(result: Any) -> str:
    return result["action"] if isinstance(result, dict) and result.get("action") in ("skip", "rewrite") else "pass"


def stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def next_attempt(root: Path, chat_id: str) -> int:
    attempts = root / "attempts"
    ensure_directory(attempts)
    path = attempts / hashlib.sha256(chat_id.encode()).hexdigest()
    try:
        attempt = int(path.read_text()) + 1
    except (FileNotFoundError, ValueError):
        attempt = 1
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(str(attempt))
    return attempt


def prune(root: Path, today: dt.date) -> None:
    oldest = today - dt.timedelta(days=RETENTION_DAYS)
    for path in (root / "decisions").glob("*.jsonl"):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < oldest:
            path.unlink(missing_ok=True)
    cutoff = time.time() - RETENTION_DAYS * 86400
    for path in (root / "attempts").glob("*"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def append(root: Path, record: dict, moment: dt.datetime) -> None:
    decisions = root / "decisions"
    ensure_directory(decisions)
    fd = os.open(decisions / f"{moment.date().isoformat()}.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def record(trace: Trace, chat_id: Any, payload: Any, text: Any, result: Any) -> None:
    if trace.home is None:
        raise RuntimeError("the gateway home was not resolved")
    root = trace.home / DIRECTORY
    ensure_directory(root)
    decision = decide(trace, payload, text)
    profile, route, delivery = decode_key(chat_id) or (None, None, None)
    moment = dt.datetime.now(dt.timezone.utc)
    append(root, {
        "version": VERSION,
        "recorded_at": stamp(moment),
        "chat_id": chat_id,
        "profile": profile,
        "route": route,
        "delivery": delivery,
        "attempt": next_attempt(root, str(chat_id)),
        "retry": False,
        "session_id": None,
        "action": decision.action,
        "then": decision.then,
        "rule": decision.rule,
        "outcome": outcome_of(result),
    }, moment)
    prune(root, moment.date())


_executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="webhook-router")
_lock = threading.Lock()
_pending = 0
_last_warning = -WARNING_INTERVAL_SECONDS


def warn(message: str) -> None:
    global _last_warning
    with _lock:
        now = time.monotonic()
        if now - _last_warning < WARNING_INTERVAL_SECONDS:
            return
        _last_warning = now
    logger.warning("linear-agent-session: the shadow router recorded no decision: %s", message)


def _run(trace: Trace, chat_id: Any, payload: Any, text: Any, result: Any) -> None:
    global _pending
    try:
        record(trace, chat_id, payload, text, result)
    except Exception as failure:
        warn(f"{type(failure).__name__}: {failure}")
    finally:
        with _lock:
            _pending -= 1


def submit(enabled: Callable[[], bool], trace: Trace, event: Any, result: Any) -> None:
    global _pending
    try:
        if trace.route is None or not enabled():
            return
    except Exception as failure:
        warn(f"the router_shadow setting could not be read: {type(failure).__name__}")
        return
    with _lock:
        full = _pending >= MAX_PENDING
        if not full:
            _pending += 1
    if full:
        warn(f"{MAX_PENDING} decisions are already waiting for the worker")
        return
    try:
        _executor.submit(_run, trace, event.source.chat_id, event.raw_message, event.text, result)
    except Exception as failure:
        with _lock:
            _pending -= 1
        warn(f"{type(failure).__name__}: {failure}")


def drain() -> None:
    _executor.submit(lambda: None).result()
