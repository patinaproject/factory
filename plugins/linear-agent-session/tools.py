"""The linear_agent_activity and linear_issue tools, shared by the Hermes plugin and cli.py."""

from __future__ import annotations

import json
from typing import Callable

from .linear import ACTIVITY_TYPES, LinearClient, activity_content

ClientFactory = Callable[[], LinearClient]

ACTIVITY_SCHEMA = {
    "name": "linear_agent_activity",
    "description": (
        "Post an activity to a Linear agent session as the agent app, and optionally replace the "
        "session's external URLs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_session_id": {"type": "string", "description": "Linear agent session UUID."},
            "type": {"type": "string", "enum": list(ACTIVITY_TYPES)},
            "body": {"type": "string", "description": "Markdown body for thought, response, elicitation, or error."},
            "action": {"type": "string", "description": "Action name, for type action."},
            "parameter": {"type": "string", "description": "Action parameter, for type action."},
            "result": {"type": "string", "description": "Optional action result, for type action."},
            "ephemeral": {
                "type": "boolean",
                "description": "Replace this activity with the next one. Linear allows it only for thought and action.",
            },
            "external_urls": {
                "type": "array",
                "description": "Replaces the session's external URLs, for example the Kanban card or pull request.",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "url": {"type": "string"}},
                    "required": ["label", "url"],
                },
            },
        },
        "required": ["agent_session_id", "type"],
    },
}

ISSUE_SCHEMA = {
    "name": "linear_issue",
    "description": (
        "Read a Linear issue as the agent app: id, identifier, title, url, gitBranchName, priority, "
        "state, stateType, delegate (the delegate's user ID, or null), and attachments."
    ),
    "parameters": {
        "type": "object",
        "properties": {"issue": {"type": "string", "description": "Issue UUID or identifier such as ABC-123."}},
        "required": ["issue"],
    },
}


SESSION_CREATE_SCHEMA = {
    "name": "linear_agent_session_create",
    "description": (
        "Open a new Linear agent session on an issue as the agent app, for a delegation that arrived "
        "without one. Returns the agent_session_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {"issue": {"type": "string", "description": "Issue UUID or identifier such as ABC-123."}},
        "required": ["issue"],
    },
}


def run_activity(client_factory: ClientFactory, args: dict) -> str:
    def run() -> dict:
        content = activity_content(
            args["type"],
            body=args.get("body"),
            action=args.get("action"),
            parameter=args.get("parameter"),
            result=args.get("result"),
        )
        client = client_factory()
        session_id = args["agent_session_id"]
        activity_id = client.create_activity(session_id, content, ephemeral=bool(args.get("ephemeral")))
        result = {"ok": True, "agent_activity_id": activity_id}
        if args.get("external_urls"):
            client.set_external_urls(session_id, args["external_urls"])
            result["external_urls"] = [u["url"] for u in args["external_urls"]]
        return result

    return _as_json(run)


def run_session_create(client_factory: ClientFactory, args: dict) -> str:
    return _as_json(lambda: {"agent_session_id": client_factory().create_session_on_issue(args["issue"])})


def run_issue(client_factory: ClientFactory, args: dict) -> str:
    return _as_json(lambda: client_factory().get_issue(args["issue"]))


def _as_json(run: Callable[[], dict]) -> str:
    # Tool handlers are the boundary with Hermes: every failure becomes a result the model can read.
    try:
        return json.dumps(run())
    except KeyError as e:
        return json.dumps({"error": f"missing field {e.args[0]!r}"})
    except Exception as e:
        return json.dumps({"error": str(e) or type(e).__name__})
