from __future__ import annotations

import json
from typing import Callable

from .linear import ACTIVITY_TYPES, LinearClient, LinearError, activity_content

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

# Hermes defers plugin tools and lists each one by the first 60 characters of its
# description, so a model calls it without the schema. Each issue tool's description
# opens with its argument, and the handlers also take the names models guess instead.
# No "required" here: Hermes would refuse a guessed name before the handler runs.
ISSUE_PARAMETERS = {
    "type": "object",
    "properties": {"issue": {"type": "string", "description": "Required. Issue UUID or identifier such as ABC-123."}},
}
ISSUE_ARGUMENT_NAMES = ("issue", "id", "issue_id")

ISSUE_SCHEMA = {
    "name": "linear_issue",
    "description": (
        "Read the Linear issue named by `issue` (UUID or ABC-123). Reads as the agent app and returns "
        "id, identifier, title, url, gitBranchName, priority, priorityName, "
        "cardPriority (the Kanban card priority for this issue), "
        "state, stateType, delegate (the delegate's user ID, or null), labels (names), attachments, and agentSessions "
        "(id, status, createdAt, appUserId; newest first)."
    ),
    "parameters": ISSUE_PARAMETERS,
}


SESSION_CREATE_SCHEMA = {
    "name": "linear_agent_session_create",
    "description": (
        "Open an agent session on `issue` (UUID or ABC-123). Acts as the agent app, for a delegation "
        "that arrived without a session. Returns the agent_session_id."
    ),
    "parameters": ISSUE_PARAMETERS,
}


DELEGATED_ISSUES_SCHEMA = {
    "name": "linear_delegated_issues",
    "description": (
        "List the Linear issues delegated to the agent app, open ones only unless include_closed is set: "
        "id, identifier, title, url, gitBranchName, state, stateType, and attachments (pull requests "
        "appear here)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_closed": {"type": "boolean", "description": "Also list completed and canceled issues."}
        },
    },
}

UNDELEGATE_SCHEMA = {
    "name": "linear_issue_undelegate",
    "description": (
        "Undelegate the agent app from `issue` (UUID or ABC-123). Refuses an issue delegated to a "
        "different agent. Returns removed: false when the issue had no delegate."
    ),
    "parameters": ISSUE_PARAMETERS,
}


TO_TODO_SCHEMA = {
    "name": "linear_issue_to_todo",
    "description": (
        "Move `issue` (UUID or ABC-123) from the Triage status to its team's Todo status. Leaves an issue in "
        "any other status as it is. Returns identifier, moved, and the issue's state."
    ),
    "parameters": ISSUE_PARAMETERS,
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

    return _tool_result_or_error(run)


def run_session_create(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(
        lambda: {"agent_session_id": client_factory().create_session_on_issue(_issue_argument(args))}
    )


def run_issue(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(lambda: client_factory().get_issue(_issue_argument(args)))


def run_delegated_issues(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(
        lambda: {"issues": client_factory().list_delegated_issues(include_closed=bool(args.get("include_closed")))}
    )


def run_undelegate(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(lambda: client_factory().undelegate_self(_issue_argument(args)))


def run_to_todo(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(lambda: client_factory().move_to_todo(_issue_argument(args)))


def _issue_argument(args: dict) -> str:
    for name in ISSUE_ARGUMENT_NAMES:
        if args.get(name):
            return args[name]
    raise LinearError("pass issue: the issue UUID or identifier such as ABC-123")


def _tool_result_or_error(run: Callable[[], dict]) -> str:
    try:
        return json.dumps(run())
    except KeyError as e:
        return json.dumps({"error": f"missing field {e.args[0]!r}"})
    except Exception as e:
        return json.dumps({"error": str(e) or type(e).__name__})
