from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from . import github_issue
from .linear import ACTIVITY_TYPES, CLIENT_ERRORS, SESSION_VERBS, LinearClient, LinearError, activity_content

logger = logging.getLogger(__name__)

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
        "description, githubIssues (the URLs of the GitHub issues attached to it), "
        "state, stateType, delegate (the delegate's user ID, or null), labels (names), role_labels (the live label name of each triage role the issue's team and workspace "
        "have, keyed needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix; name a label to people "
        "by it, never by the role), attachments, "
        "blockedBy (each blocking issue's id, identifier, gitBranchName, stateType, delegate), and agentSessions "
        "(id, status, createdAt, appUserId, creatorId (null when no person opened it); newest first)."
    ),
    "parameters": ISSUE_PARAMETERS,
}

ACTIVITY_ID_NAMES = ("agent_activity_id", "activity_id", "id")
SESSION_ID_NAMES = ("agent_session_id", "session_id")

ACTIVITY_READ_SCHEMA = {
    "name": "linear_agent_activity_read",
    "description": (
        "Read the prompt `agent_activity_id`, or else the newest prompt in `agent_session_id` that is not a "
        "stop. Reads as the agent app and returns found, id, createdAt, signal, and body (the person's "
        "reply text), or found: false when Linear has no such prompt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_activity_id": {"type": "string", "description": "Linear agent activity UUID of the prompt."},
            "agent_session_id": {
                "type": "string",
                "description": "Linear agent session UUID, used when the activity id is unknown.",
            },
        },
    },
}


SESSION_CREATE_SCHEMA = {
    "name": "linear_agent_session_create",
    "description": (
        "Open an agent session on `issue` (UUID or ABC-123). Acts as the agent app, for a delegation "
        "that arrived without a session, titled by `purpose`. Returns the agent_session_id and session_title."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **ISSUE_PARAMETERS["properties"],
            "purpose": {
                "type": "string",
                "enum": list(SESSION_VERBS),
                "description": "Required. triage for a triage card's session, work for an implementation card's.",
            },
        },
        "required": ["purpose"],
    },
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


EDIT_SCHEMA = {
    "name": "linear_issue_edit",
    "description": (
        "Edit `issue` (UUID or ABC-123), any issue the agent app can read: title, description (the full new "
        "text), add_labels, remove_labels, and state, labels and status by name, matched whatever their case and "
        "punctuation. An added label replaces the issue's other label in its group. Creates no label or status, "
        "and never changes assignee or delegate. Returns identifier, changed, title, labels (names), and state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **ISSUE_PARAMETERS["properties"],
            "title": {"type": "string", "description": "The issue's new title."},
            "description": {"type": "string", "description": "The issue's full new description, in Markdown."},
            "add_labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to add."},
            "remove_labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to remove."},
            "state": {"type": "string", "description": "Status name to move the issue to, such as Canceled."},
        },
    },
}

RELATE_SCHEMA = {
    "name": "linear_issue_relate",
    "description": (
        "Relate `issue` (UUID or ABC-123) to another Linear issue: pass duplicate_of to mark `issue` a duplicate "
        "of it, or blocked_by to block `issue` on it, exactly one of the two. A duplicate moves to the team's "
        "duplicate status and loses its related relation to the original, and no other relation changes. "
        "Refuses a conflicting relation and more than 50 relations, changing nothing. Never changes assignee "
        "or delegate. Returns identifier, related_issue, relation, created, removed_related, and state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **ISSUE_PARAMETERS["properties"],
            "duplicate_of": {"type": "string", "description": "The issue that `issue` duplicates: UUID or ABC-123."},
            "blocked_by": {"type": "string", "description": "The issue that blocks `issue`: UUID or ABC-123."},
        },
    },
}

GITHUB_EDIT_SCHEMA = {
    "name": "github_issue_edit",
    "description": (
        "Edit the GitHub issue at `url` as the factory's GitHub App, with one PATCH: title, body (the full new "
        "text), add_labels and remove_labels by name, matched whatever their case and punctuation, and state "
        "(Done, Canceled, or Todo, Backlog, or Triage to reopen). Adds only labels the repository has. Never "
        "changes assignees. Returns url, changed, title, labels, state, and state_reason."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Required. https://github.com/<owner>/<repo>/issues/<number>."},
            "title": {"type": "string", "description": "The issue's new title."},
            "body": {"type": "string", "description": "The issue's full new body, in Markdown."},
            "add_labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to add."},
            "remove_labels": {"type": "array", "items": {"type": "string"}, "description": "Label names to remove."},
            "state": {"type": "string", "description": "Done, Canceled, or a status that reopens, such as Todo."},
        },
    },
}

SEARCH_ARGUMENT_NAMES = ("query", "term", "text")

SEARCH_SCHEMA = {
    "name": "linear_issue_search",
    "description": (
        "Search `query` (text) across the Linear issues the agent app can read. Returns issues, up to 10, "
        "each with identifier, title, url, team, and state."
    ),
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Required. Words to search issues for."}},
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
        if not args.get("ephemeral"):
            try:
                result["closed_sessions"] = client.close_older_sessions(session_id)
            except Exception as error:
                logger.warning("linear-agent-session: closing older sessions failed: %s", type(error).__name__)
                result["close_error"] = type(error).__name__
        return result

    return _tool_result_or_error(run)


def run_activity_read(client_factory: ClientFactory, args: dict) -> str:
    def run() -> dict:
        activity_id = _first_argument(args, ACTIVITY_ID_NAMES)
        session_id = _first_argument(args, SESSION_ID_NAMES)
        if not activity_id and not session_id:
            raise LinearError("pass agent_activity_id, or agent_session_id when the activity id is unknown")
        return client_factory().read_prompt(agent_activity_id=activity_id, agent_session_id=session_id)

    return _tool_result_or_error(run)


def run_session_create(client_factory: ClientFactory, args: dict) -> str:
    def run() -> dict:
        issue = _issue_argument(args)
        purpose = args.get("purpose")
        if purpose not in SESSION_VERBS:
            raise LinearError(f"pass purpose: one of {', '.join(SESSION_VERBS)}")
        client = client_factory()
        session_id = client.create_session_on_issue(issue)
        try:
            title = {"title": client.title_session(session_id, issue, purpose)}
        except (*CLIENT_ERRORS, OSError) as error:
            title = {"error": f"titling the agent session failed: {error}"}
        return {"agent_session_id": session_id, "session_title": title}

    return _tool_result_or_error(run)


def run_issue(client_factory: ClientFactory, args: dict) -> str:
    def run() -> dict:
        client = client_factory()
        issue = client.get_issue(_issue_argument(args))
        try:
            role_labels = client.role_labels(issue["id"])
        except CLIENT_ERRORS:
            # Every route decision reads the issue; the label names only word messages to people.
            role_labels = {}
        return {**issue, "role_labels": role_labels}

    return _tool_result_or_error(run)


def run_review_owner(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(lambda: {"owner": client_factory().review_owner(_issue_argument(args))})


def run_delegated_issues(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(
        lambda: {"issues": client_factory().list_delegated_issues(include_closed=bool(args.get("include_closed")))}
    )


def run_undelegate(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(lambda: client_factory().undelegate_self(_issue_argument(args)))


def run_to_todo(client_factory: ClientFactory, args: dict) -> str:
    return _tool_result_or_error(lambda: client_factory().move_to_todo(_issue_argument(args)))


def run_edit(client_factory: ClientFactory, args: dict) -> str:
    def run() -> dict:
        _refuse_people(args)
        issue = _issue_argument(args)
        add, remove, state = _names(args.get("add_labels")), _names(args.get("remove_labels")), args.get("state")
        title, description = _title(args), args.get("description")
        if not (add or remove or state or title or description is not None):
            raise LinearError("pass title, description, add_labels, remove_labels, or state")
        return client_factory().edit_issue(
            issue, add_labels=add, remove_labels=remove, state=state, title=title, description=description
        )

    return _tool_result_or_error(run)


def run_relate(client_factory: ClientFactory, args: dict) -> str:
    def run() -> dict:
        _refuse_people(args)
        issue = _issue_argument(args)
        asked = {kind: args[name] for name, kind in (("duplicate_of", "duplicate"), ("blocked_by", "blocks")) if args.get(name)}
        if len(asked) != 1:
            raise LinearError("pass exactly one of duplicate_of or blocked_by")
        ((kind, other),) = asked.items()
        return client_factory().relate_issues(issue, other, kind)

    return _tool_result_or_error(run)


def run_github_edit(args: dict, gh: github_issue.Gh = github_issue.run_gh) -> str:
    def run() -> dict:
        _refuse_people(args)
        url = _first_argument(args, ("url", "issue", "issue_url"))
        if not url:
            raise LinearError("pass url, the GitHub issue's URL")
        add, remove, state = _names(args.get("add_labels")), _names(args.get("remove_labels")), args.get("state")
        title, body = _title(args), _first_present(args, ("body", "description"))
        if not (add or remove or state or title or body is not None):
            raise LinearError("pass title, body, add_labels, remove_labels, or state")
        return github_issue.edit_issue(
            url, title=title, body=body, add_labels=add, remove_labels=remove, state=state, gh=gh
        )

    return _tool_result_or_error(run)


def _refuse_people(args: dict) -> None:
    refused = sorted(k for k in args if "assignee" in k.casefold() or "delegate" in k.casefold())
    if refused:
        raise LinearError(f"this tool never changes assignee or delegate; drop {', '.join(refused)}")


def _title(args: dict) -> Optional[str]:
    title = args.get("title")
    if title is not None and not title.strip():
        raise LinearError("a title cannot be blank")
    return title


def _first_present(args: dict, names: tuple) -> Optional[str]:
    return next((args[name] for name in names if args.get(name) is not None), None)


def run_search(client_factory: ClientFactory, args: dict) -> str:
    def run() -> dict:
        term = _first_argument(args, SEARCH_ARGUMENT_NAMES)
        if not term:
            raise LinearError("pass query, the words to search issues for")
        return {"issues": client_factory().search_issues(term)}

    return _tool_result_or_error(run)


def _names(value) -> list:
    return [value] if isinstance(value, str) else list(value or [])


def _issue_argument(args: dict) -> str:
    issue = _first_argument(args, ISSUE_ARGUMENT_NAMES)
    if issue is None:
        raise LinearError("pass issue: the issue UUID or identifier such as ABC-123")
    return issue


def _first_argument(args: dict, names: tuple) -> Optional[str]:
    return next((args[name] for name in names if args.get(name)), None)


def _tool_result_or_error(run: Callable[[], dict]) -> str:
    try:
        return json.dumps(run())
    except KeyError as e:
        return json.dumps({"error": f"missing field {e.args[0]!r}"})
    except Exception as e:
        return json.dumps({"error": str(e) or type(e).__name__})
