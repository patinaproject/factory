"""Hermes fires `on_session_end` only for a turn that finishes through its normal path, and a turn that exhausts its
retries on a rate limit returns early. So `api_request_error` marks the failure, and `on_session_end` clears an
entry whose turn finished after all, such as one a fallback provider rescued.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from . import queue_store

logger = logging.getLogger(__name__)

# Hermes leaves a secret like this verbatim when its variable is unset; linear-agent-session drops such a delivery.
UNRESOLVED_REFERENCE = re.compile(r"^\$\{(?:env:)?[A-Za-z_][A-Za-z0-9_]*\}$")
GITHUB_EVENT_LINE = re.compile(r"^- Event: (\S+), action (\S+)$", re.MULTILINE)
SUMMARY_LIMIT = 120
ERROR_LIMIT = 300
REPORT_VARIABLE = "WEBHOOK_RETRY_REPORT"

_session_store: Any = None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", record_delivery)
    ctx.register_hook("api_request_error", note_request_error)
    ctx.register_hook("on_session_end", clear_finished_turn)


def hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def load_config() -> Mapping[str, Any]:
    from hermes_cli.config import load_config_readonly

    return load_config_readonly()


def record_delivery(event: Any = None, session_store: Any = None, **_: Any) -> None:
    global _session_store
    try:
        if session_store is not None:
            _session_store = session_store
        source = event.source
        if source.platform.value != "webhook" or not source.user_id.startswith("webhook:"):
            return None
        route = source.user_id.removeprefix("webhook:")
        key = source.chat_id
        decoded = queue_store.decode_key(key)
        if decoded is None or secret_is_unresolved(route):
            return None
        profile, route, delivery = decoded
        fields = {
            "key": key,
            "profile": profile,
            "route": route,
            "delivery": delivery,
            "delivered_at": queue_store.stamp(delivered_at(event)),
            "summary": summarize(event),
            "prompt": event.text,
            "attempts": 0,
            "last_error": "",
            "next_try_at": None,
            "reported": False,
        }
        queue_store.mutate(
            queue_store.queue_dir(hermes_home()), lambda entries: queue_store.apply(entries, key, "dispatch", **fields)
        )
    except Exception:
        logger.exception("webhook-retry: could not record a delivery")
    return None


def note_request_error(
    session_id: str = "", reason: Optional[str] = None, error: Any = None, **_: Any
) -> None:
    try:
        if reason not in queue_store.USAGE_LIMIT_REASONS:
            return None
        message = queue_store.truncate((error or {}).get("message", ""), ERROR_LIMIT)
        reset = reset_delay(message)
        report_path = os.environ.get(REPORT_VARIABLE)
        if report_path:
            Path(report_path).write_text(json.dumps({"reason": reason, "message": message, "reset_seconds": reset}))
            return None
        key = session_key(session_id)
        if key is None:
            return None
        delay = max(reset or 0, queue_store.MIN_DELAY_SECONDS)
        next_try = queue_store.now() + datetime.timedelta(seconds=delay)
        queue_store.mutate(
            queue_store.queue_dir(hermes_home()),
            lambda entries: queue_store.apply(
                entries, key, "usage_limit", last_error=message, next_try_at=queue_store.stamp(next_try)
            ),
        )
    except Exception:
        logger.exception("webhook-retry: could not queue a delivery")
    return None


def clear_finished_turn(session_id: str = "", **_: Any) -> None:
    try:
        key = session_key(session_id)
        if key is None:
            return None
        queue_store.mutate(
            queue_store.queue_dir(hermes_home()), lambda entries: queue_store.apply(entries, key, "turn_ended")
        )
    except Exception:
        logger.exception("webhook-retry: could not clear a finished delivery")
    return None


def session_key(session_id: str) -> Optional[str]:
    if _session_store is None or not session_id:
        return None
    entry = _session_store.lookup_by_session_id(session_id)
    origin = getattr(entry, "origin", None)
    key = getattr(origin, "chat_id", None)
    return key if queue_store.decode_key(key) is not None else None


def reset_delay(message: str) -> Optional[float]:
    try:
        from agent.retry_utils import reset_delay_from_message

        delay = reset_delay_from_message(message)
        if delay is not None:
            return delay
    except Exception:
        logger.debug("webhook-retry: Hermes could not parse the reset delay", exc_info=True)
    return queue_store.reset_delay_seconds(message)


def secret_is_unresolved(route: str) -> bool:
    extra = ((load_config().get("platforms") or {}).get("webhook") or {}).get("extra") or {}
    route_config = (extra.get("routes") or {}).get(route) or {}
    secret = route_config.get("secret", extra.get("secret", ""))
    return isinstance(secret, str) and UNRESOLVED_REFERENCE.match(secret) is not None


def delivered_at(event: Any):
    payload = event.raw_message if isinstance(event.raw_message, dict) else {}
    millis = payload.get("webhookTimestamp")
    if isinstance(millis, (int, float)):
        return datetime.datetime.fromtimestamp(millis / 1000, datetime.timezone.utc)
    return event.timestamp


def summarize(event: Any) -> dict[str, str]:
    payload = event.raw_message if isinstance(event.raw_message, dict) else {}
    line = GITHUB_EVENT_LINE.search(event.text or "")
    kind = payload.get("type") or (line.group(1) if line else "")
    action = payload.get("action") or (line.group(2) if line else "")
    summary = {"event": kind, "action": action, "subject": subject(payload)}
    return {name: queue_store.truncate(value, SUMMARY_LIMIT) if isinstance(value, str) else "" for name, value in summary.items()}


def subject(payload: dict) -> str:
    for path in (("data", "identifier"), ("agentSession", "issue", "identifier"), ("data", "issue", "identifier")):
        value = dig(payload, path)
        if isinstance(value, str):
            return value
    repository = dig(payload, ("repository", "full_name"))
    if isinstance(repository, str):
        for holder in (payload.get("pull_request"), payload.get("issue"), first(dig(payload, ("check_run", "pull_requests")))):
            number = holder.get("number") if isinstance(holder, dict) else None
            if isinstance(number, int):
                return f"{repository}#{number}"
    return ""


def dig(value: Any, path: tuple) -> Any:
    for name in path:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    return value


def first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else None
