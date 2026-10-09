#!/usr/bin/env python3
"""Render the Hermes `linear` and `github` webhook routes from the factory settings.

Prints a JSON object for `platforms.webhook.extra.routes`. Settings come from
`hermes config get plugins.entries.linear-agent-session.settings --json`, or from
`--settings-json <file>`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

SETTINGS_KEY = "plugins.entries.linear-agent-session.settings"
TEMPLATE_DIR = Path(__file__).resolve().parent
GITHUB_ROUTE = "github"
ROUTINGS = ("default", "synced_github")
TOOLSETS = ["terminal", "kanban", "linear_agent_session"]
LINEAR_EVENTS = ["AgentSessionEvent", "Issue"]
GITHUB_EVENTS = [
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "workflow_run",
    "check_run",
    "check_suite",
]
GITHUB_CHECK_EVENTS = ["check_run", "check_suite", "workflow_run"]
FAILED_CONCLUSIONS = ["failure", "timed_out", "action_required", "startup_failure"]
LINEAR_SECRET = "${LINEAR_WEBHOOK_SECRET}"
GITHUB_SECRET = "${GITHUB_WEBHOOK_SECRET}"

FULL_NAME = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
ROUTE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
MARKER = re.compile(r"<<([a-z_]+)>>")
# The same token shape the Hermes webhook adapter substitutes from the payload.
HERMES_PLACEHOLDER = re.compile(r"\{[a-zA-Z0-9_.]+\}")


class SettingsError(Exception):
    pass


def load_settings(settings_json: Optional[str]) -> Any:
    command = f"hermes config get {SETTINGS_KEY} --json"
    try:
        if settings_json:
            return json.loads(Path(settings_json).read_text(encoding="utf-8"))
        proc = subprocess.run(command.split(), capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise SettingsError(f"`{command}` exited {proc.returncode}: {proc.stderr.strip()}")
        return json.loads(proc.stdout)
    except (OSError, ValueError) as exc:
        raise SettingsError(f"could not read settings from {settings_json or f'`{command}`'}: {exc}")


def _require_text(settings: dict, key: str) -> str:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{key} must be a non-empty string")
    return value.strip()


def validate(settings: Any) -> dict:
    if not isinstance(settings, dict):
        raise SettingsError(f"settings must be an object, got {type(settings).__name__}")
    app_user_id = _require_text(settings, "app_user_id")
    route = _require_text(settings, "webhook_route")
    if not ROUTE_NAME.match(route):
        raise SettingsError(f"webhook_route {route!r} may contain only letters, digits, '-' and '_'")
    if route == GITHUB_ROUTE:
        raise SettingsError(f"webhook_route must not be {GITHUB_ROUTE!r}, which is the GitHub route")

    repositories = settings.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise SettingsError("repositories must be a non-empty list")
    seen = set()
    for index, repo in enumerate(repositories):
        where = f"repositories[{index}]"
        if not isinstance(repo, dict):
            raise SettingsError(f"{where} must be an object")
        full_name = repo.get("full_name")
        if not isinstance(full_name, str) or not FULL_NAME.match(full_name):
            raise SettingsError(f"{where}.full_name {full_name!r} must look like owner/name")
        if full_name.lower() in seen:
            raise SettingsError(f"{where}.full_name {full_name!r} is listed more than once")
        seen.add(full_name.lower())
        path = repo.get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise SettingsError(f"{where}.path {path!r} must be an absolute path")
        if repo.get("routing") not in ROUTINGS:
            raise SettingsError(f"{where}.routing {repo.get('routing')!r} must be one of {', '.join(ROUTINGS)}")
    defaults = [repo["full_name"] for repo in repositories if repo["routing"] == "default"]
    if len(defaults) != 1:
        raise SettingsError(f"exactly one repository must have routing 'default', found {len(defaults)}")

    github = settings.get("github") or {}
    if not isinstance(github, dict):
        raise SettingsError("github must be an object")
    login = github.get("login", "")
    if not isinstance(login, str) or not re.match(r"^([A-Za-z0-9][A-Za-z0-9-]{0,38})?$", login):
        raise SettingsError(f"github.login {login!r} must be empty or a GitHub login")

    return {
        "app_user_id": app_user_id,
        "webhook_route": route,
        "repositories": repositories,
        "github_login": login or None,
    }


def _workspace_flag(repo: dict) -> str:
    # The `default` repository's worktrees anchor at the board's default_workdir.
    return "--workspace worktree" if repo["routing"] == "default" else f"--workspace worktree:{repo['path']}"


def repository_lines(repositories: list) -> str:
    return "\n".join(
        f"- `{repo['full_name']}`: routing `{repo['routing']}`, checkout `{repo['path']}`, "
        f"card flags `{_workspace_flag(repo)} --completion-contract {repo['full_name']}`"
        for repo in repositories
    )


def render_prompt(template: str, values: dict) -> str:
    def substitute(match: re.Match) -> str:
        name = match.group(1)
        if name not in values:
            raise SettingsError(f"template marker <<{name}>> has no value")
        return values[name]

    rendered = MARKER.sub(substitute, template)
    expected = Counter(HERMES_PLACEHOLDER.findall(template))
    found = Counter(HERMES_PLACEHOLDER.findall(rendered))
    if found != expected:
        added = sorted((found - expected).elements())
        raise SettingsError(f"settings values add Hermes payload placeholders to the prompt: {', '.join(added)}")
    if "{__raw__}" not in found:
        raise SettingsError("the prompt template must contain {__raw__}")
    stray = HERMES_PLACEHOLDER.sub("", rendered)
    if "{" in stray or "}" in stray:
        raise SettingsError("the rendered prompt contains a brace outside a Hermes payload placeholder")
    if "${" in rendered:
        raise SettingsError("the rendered prompt contains '${', which Hermes expands as an environment variable")
    return rendered


def _read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def build_routes(settings: Any) -> dict:
    config = validate(settings)
    repos = repository_lines(config["repositories"])
    full_names = [repo["full_name"] for repo in config["repositories"]]

    github_filters: list = [
        {"field": "repository.full_name", "in": full_names},
        {
            "any": [
                {"not": {"field": "event", "in": GITHUB_CHECK_EVENTS}},
                *({"field": f"{event}.conclusion", "in": FAILED_CONCLUSIONS} for event in GITHUB_CHECK_EVENTS),
            ]
        },
    ]
    if config["github_login"] is not None:
        # Events the factory account causes itself (pushes, PR updates, comments) would
        # otherwise loop back onto its own cards.
        github_filters.append({"not": {"field": "sender.login", "equals": config["github_login"]}})

    return {
        config["webhook_route"]: {
            "events": LINEAR_EVENTS,
            "secret": LINEAR_SECRET,
            "filters": [
                {
                    "any": [
                        {"field": "event", "equals": "AgentSessionEvent"},
                        {"field": "updatedFrom.delegateId", "exists": True},
                        {
                            "all": [
                                {"field": "updatedFrom.stateId", "exists": True},
                                {"field": "data.state.type", "in": ["completed", "canceled"]},
                            ]
                        },
                    ]
                }
            ],
            "toolsets": TOOLSETS,
            "prompt": render_prompt(
                _read_template("triage-linear.md"),
                {"app_user_id": config["app_user_id"], "repositories": repos},
            ),
        },
        GITHUB_ROUTE: {
            "events": GITHUB_EVENTS,
            "secret": GITHUB_SECRET,
            "filters": github_filters,
            "toolsets": TOOLSETS,
            "prompt": render_prompt(_read_template("triage-github.md"), {"repositories": repos}),
        },
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--settings-json", help="read settings from this JSON file instead of `hermes config get`")
    args = parser.parse_args(argv)
    try:
        routes = build_routes(load_settings(args.settings_json))
    except SettingsError as exc:
        print(f"render.py: invalid settings: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(routes, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
