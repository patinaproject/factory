"""Find a person's reply in an agent session that the gateway never received, so webhook-retry can queue it.

The dispatch hook and this sweep share one once-key per prompt activity. Whichever claims it first answers the
prompt, and the other leaves it alone.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Callable, Mapping, Optional

from . import idempotency, linear, triage_intent

ONCE_NAMESPACE = "session_prompt"
MARKER_NAME = "missed-prompts.json"

IntentComputer = Callable[[dict, dt.datetime], triage_intent.Intent]


class SweepError(Exception):
    pass


def claim(directory: Optional[str], activity_id: str) -> bool:
    """True only when this call created the activity's record. A record that already exists, even a reserved one, is False."""
    created = []
    try:
        idempotency.once(directory, ONCE_NAMESPACE, activity_id, {}, lambda: created.append(True) or {"recorded": True})
    except linear.LinearError:
        if directory is None or not idempotency.recorded(directory, activity_id):
            raise
        return False
    return bool(created)


def marker_path(environ: Mapping[str, str]) -> Optional[str]:
    if environ.get("HERMES_HOME"):
        return os.path.join(environ["HERMES_HOME"], "plugin-data", "linear-agent-session", MARKER_NAME)
    return None


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _read_marker(path: str) -> Optional[dt.datetime]:
    try:
        with open(path) as handle:
            return triage_intent.parse_time(json.load(handle)["since"])
    except FileNotFoundError:
        return None


def _write_marker(path: str, moment: dt.datetime) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as handle:
        json.dump({"since": _iso(moment)}, handle)
    os.replace(path + ".tmp", path)


def payload_of(node: Mapping[str, Any], app_user_id: str) -> dict:
    """The webhook payload Linear would have sent for the prompt, from the fields the API returns."""
    created = triage_intent.parse_time(node["createdAt"])
    session = node["agentSession"]
    issue = session["issue"]
    return {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "appUserId": app_user_id,
        "createdAt": node["createdAt"],
        "webhookTimestamp": int(created.timestamp() * 1000),
        "agentSession": {"id": session["id"], "issue": {"id": issue["id"], "identifier": issue["identifier"]}},
        "agentActivity": {
            "id": node["id"],
            "signal": node["signal"],
            "userId": node["promptAuthor"]["id"],
            "content": {"body": node["content"]["body"]},
        },
    }


def _candidate(node: Mapping[str, Any], app_user_id: str, start: dt.datetime, cutoff: dt.datetime, store: Optional[str]) -> bool:
    author = node["promptAuthor"]
    created = triage_intent.parse_time(node["createdAt"])
    return (
        node["agentSession"]["appUser"]["id"] == app_user_id
        and node["agentSession"]["issue"] is not None
        and not node["ephemeral"]
        and node["signal"] != "stop"
        and author is not None
        and author["app"] is False
        and start < created <= cutoff
        and not (store is not None and idempotency.recorded(store, node["id"]))
    )


def sweep(
    *,
    client: Any,
    app_user_id: str,
    compute_intent: IntentComputer,
    store: Optional[str],
    marker: Optional[str],
    now: dt.datetime,
    window: dt.timedelta,
    grace: dt.timedelta,
) -> list[dict]:
    """The payloads of the prompts nobody has answered or queued, oldest first, each now claimed by this call.

    The first sweep after a deploy writes the marker and returns nothing, so prompts from before it are never
    answered late. Every Linear read and intent finishes before the first claim, so a failure claims nothing."""
    if not app_user_id:
        raise SweepError("the app_user_id setting is empty")
    if marker is None:
        raise SweepError("HERMES_HOME is not set, so there is no sweep marker")
    since = _read_marker(marker)
    if since is None:
        _write_marker(marker, now)
        return []
    start = max(since, now - window)
    ready = []
    for node in client.recent_prompts(_iso(start)):
        if not _candidate(node, app_user_id, start, now - grace, store):
            continue
        if client.newest_activity_id(node["agentSession"]["id"]) != node["id"]:
            continue
        payload = payload_of(node, app_user_id)
        intent = compute_intent(payload, triage_intent.parse_time(node["createdAt"]))
        if intent.action == "error":
            raise SweepError(f"the intent for prompt {node['id']} failed: {intent.error}")
        if intent.authorized_as is None or triage_intent.needs_no_turn(intent, payload):
            continue
        ready.append(payload)
    ready.sort(key=lambda payload: payload["createdAt"])
    return [payload for payload in ready if claim(store, payload["agentActivity"]["id"])]
