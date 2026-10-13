#!/usr/bin/env python3
"""Render the factory's Hermes webhook config, the `linear` and `github` routes and the rate limit, from the settings."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

SETTINGS_KEY = "plugins.entries.linear-agent-session.settings"
ROUTES_KEY = "platforms.webhook.extra.routes"
RATE_LIMIT_KEY = "platforms.webhook.extra.rate_limit"
# Deliveries per route per minute. Hermes counts every signed delivery before its event and filter
# checks, so GitHub's check events use the budget, and it answers 429 to the rest, which GitHub never
# redelivers. The github route peaks near 110 a minute; Hermes' default of 30 dropped review events.
RATE_LIMIT = 600
TEMPLATE_DIR = Path(__file__).resolve().parent
GITHUB_ROUTE = "github"
ROUTINGS = ("default", "synced_github")
TOOLSETS = ["terminal", "kanban", "linear_agent_session"]
LINEAR_EVENTS = ["AgentSessionEvent", "Issue"]
# Hermes resolves a relative in_file path against the gateway's home.
OWNED_REFS_FILE = "owned-refs.json"
FAILED_CONCLUSIONS = ["failure", "timed_out", "action_required", "startup_failure"]
# A bot whose pull request comments repeat a result the route already keeps as a failed check_run.
CHECK_RUN_ECHO_BOTS = ["cloudflare-workers-and-pages[bot]"]
LINEAR_LINKBACK = r"\A<!-- linear-linkback -->"
LINEAR_SECRET = "${LINEAR_WEBHOOK_SECRET}"
GITHUB_SECRET = "${GITHUB_WEBHOOK_SECRET}"

FULL_NAME = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
ROUTE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
MARKER = re.compile(r"<<([a-z_]+)>>")
REFERENCE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


class GithubEvent(NamedTuple):
    """One event the `github` route delivers and triage acts on."""

    name: str
    actions: tuple
    only_when: str
    keep: Callable[[str], list]


def owned(field: str) -> dict:
    return {"field": field, "in_file": OWNED_REFS_FILE}


def failed(event: str) -> dict:
    return {"field": f"{event}.conclusion", "in": FAILED_CONCLUSIONS}


def keeps_pull_request(login: str) -> list:
    # Triage finds a pull request's card by its branch, so a branch on no card always ends in "no card matches".
    return [{"field": "pull_request.user.login", "equals": login}, owned("pull_request.head.ref")]


def keeps_issue_comment(login: str) -> list:
    # An issue_comment payload carries no branch, so a comment on the App's pull request is kept by author.
    # An issue that is not a pull request is found by its URL only. Linear's linkback comment asks for nothing.
    return [
        {"not": {"field": "comment.body", "regex": LINEAR_LINKBACK}},
        {"not": {"field": "comment.user.login", "in": CHECK_RUN_ECHO_BOTS}},
        {
            "any": [
                {"all": [{"field": "issue.pull_request", "exists": True}, {"field": "issue.user.login", "equals": login}]},
                owned("issue.html_url"),
            ]
        },
    ]


def keeps_workflow_run(login: str) -> list:
    return [
        {"field": "workflow_run.triggering_actor.login", "equals": login},
        owned("workflow_run.head_branch"),
        failed("workflow_run"),
    ]


def keeps_check_run(login: str) -> list:
    # GitHub Actions jobs arrive as workflow_run events, which name the actor who triggered them.
    return [
        owned("check_run.check_suite.head_branch"),
        {"field": "check_run.app.slug", "not_equals": "github-actions"},
        failed("check_run"),
    ]


GITHUB_EVENT_TABLE = (
    GithubEvent(
        "pull_request",
        ("closed", "reopened", "converted_to_draft"),
        "the App opened the pull request from a card's branch",
        keeps_pull_request,
    ),
    GithubEvent(
        "pull_request_review",
        ("submitted",),
        "the App opened the pull request from a card's branch",
        keeps_pull_request,
    ),
    GithubEvent(
        "pull_request_review_comment",
        ("created",),
        "the App opened the pull request from a card's branch",
        keeps_pull_request,
    ),
    GithubEvent(
        "issue_comment",
        ("created",),
        "the App opened the pull request or the issue is a card's GitHub issue, "
        "and the comment is neither Linear's linkback nor a Cloudflare deploy notice",
        keeps_issue_comment,
    ),
    GithubEvent(
        "workflow_run",
        ("completed",),
        "the App triggered the run on a card's branch and it failed",
        keeps_workflow_run,
    ),
    GithubEvent(
        "check_run",
        ("completed",),
        "it failed on a card's branch and is not a GitHub Actions job",
        keeps_check_run,
    ),
    GithubEvent(
        "issues", ("closed", "reopened", "edited"), "the issue is a card's GitHub issue", lambda login: [owned("issue.html_url")]
    ),
)
GITHUB_EVENTS = [event.name for event in GITHUB_EVENT_TABLE]
GITHUB_CHECK_EVENTS = ["check_run", "workflow_run"]


class SettingsError(Exception):
    pass


def load_settings(settings_json: Optional[str]) -> Any:
    command = f"hermes config get {SETTINGS_KEY} --json --raw"
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
    login = github.get("login")
    if not isinstance(login, str) or not login.strip().endswith("[bot]"):
        raise SettingsError(
            "github.login must be the App's login, such as my-app[bot]; "
            "the github route keeps only the App's own events"
        )

    kanban_url = settings.get("kanban_url", "")
    if not isinstance(kanban_url, str):
        raise SettingsError(f"kanban_url {kanban_url!r} must be a string")

    return {
        "app_user_id": app_user_id,
        "webhook_route": route,
        "repositories": repositories,
        "github_login": login.strip(),
        "kanban_url": kanban_url or None,
    }


def _workspace_flag(repo: dict) -> str:
    # The `default` repository's worktrees anchor at the board's default_workdir.
    return "--workspace worktree" if repo["routing"] == "default" else f"--workspace worktree:{repo['path']}"


def queued_link_sentence(kanban_url) -> str:
    if kanban_url is None:
        return "Leave `external_urls` unset."
    return f"Set `external_urls` to one link labeled `Kanban card <task id>` whose URL is `{kanban_url}`."


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def card_branch_command(home: Path, github_login: str) -> str:
    script = home / "profiles" / "claude-worker" / "scripts" / "card_branch.py"
    return f"python3 {shlex.quote(str(script))} --app {shlex.quote(github_login)}"


def _or_list(items: list) -> str:
    if len(items) <= 2:
        return " or ".join(items)
    return f"{', '.join(items[:-1])}, or {items[-1]}"


def events_that_matter(table) -> str:
    return "\n".join(
        f"- `{event.name}` with action {_or_list([f'`{action}`' for action in event.actions])}, "
        f"only when {event.only_when}."
        for event in table
    )


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

    return MARKER.sub(substitute, template)


def _read_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _exclude_events_sent_by(login: str) -> dict:
    # A check failure on the App's own push names the App as sender, and the worker still needs it.
    # A merge the App performs, such as auto-merge, names the App too, and its card still has to complete.
    return {
        "any": [
            {"field": "event", "in": GITHUB_CHECK_EVENTS},
            {
                "all": [
                    {"field": "event", "equals": "pull_request"},
                    {"field": "action", "equals": "closed"},
                    {"field": "pull_request.merged", "equals": True},
                ]
            },
            {"not": {"field": "sender.login", "equals": login}},
        ]
    }


def event_filter(event: GithubEvent, login: str) -> dict:
    return {
        "all": [
            {"field": "event", "equals": event.name},
            {"field": "action", "in": list(event.actions)},
            *event.keep(login),
        ]
    }


def build_routes(settings: Any) -> dict:
    config = validate(settings)
    repos = repository_lines(config["repositories"])
    full_names = [repo["full_name"] for repo in config["repositories"]]
    login = config["github_login"]

    github_filters = [
        {"field": "repository.full_name", "in": full_names},
        {"any": [event_filter(event, login) for event in GITHUB_EVENT_TABLE]},
        _exclude_events_sent_by(login),
    ]

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
                {
                    "app_user_id": config["app_user_id"],
                    "repositories": repos,
                    "queued_link": queued_link_sentence(config["kanban_url"]),
                    "card_branch": card_branch_command(hermes_home(), config["github_login"]),
                },
            ),
        },
        GITHUB_ROUTE: {
            "events": GITHUB_EVENTS,
            "secret": GITHUB_SECRET,
            "filters": github_filters,
            "toolsets": TOOLSETS,
            "prompt": render_prompt(
                _read_template("triage-github.md"),
                {"repositories": repos, "events_that_matter": events_that_matter(GITHUB_EVENT_TABLE)},
            ),
        },
    }


def build_config(routes: dict) -> dict:
    """Map each webhook config key the factory owns to the value `hermes config set` installs."""
    return {RATE_LIMIT_KEY: RATE_LIMIT, **{f"{ROUTES_KEY}.{name}": route for name, route in routes.items()}}


def check_rate_limit() -> tuple:
    proc = subprocess.run(["hermes", "config", "get", RATE_LIMIT_KEY, "--json"], capture_output=True, text=True)
    installed = json.loads(proc.stdout) if proc.returncode == 0 else None
    if installed == RATE_LIMIT:
        return True, f"rate_limit: matches {RATE_LIMIT}"
    if installed is None:
        return False, f"rate_limit: not installed, so Hermes allows 30 a minute; install {RATE_LIMIT}"
    return False, f"rate_limit: {json.dumps(installed)} differs from {RATE_LIMIT}"


def installed_route(name: str) -> Optional[dict]:
    # --raw expands ${VAR} references into the live secret. The output stays in this process
    # and only key names and fixed text reach the report, so the secret is never printed.
    proc = subprocess.run(
        ["hermes", "config", "get", f"{ROUTES_KEY}.{name}", "--json", "--raw"], capture_output=True, text=True
    )
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def secret_status(rendered: str, installed: Any, config_text: str) -> tuple:
    """Describe where the installed secret comes from without printing its value."""
    if not isinstance(installed, str) or not installed:
        return False, "secret is missing"
    if REFERENCE.match(installed):
        return False, f"secret is the reference {installed}, but Hermes has no value for it"
    if installed in config_text:
        return False, f"secret is a literal value in config.yaml, not the {rendered} reference"
    if rendered in config_text:
        return True, f"secret is the {rendered} reference"
    return False, f"secret does not come from config.yaml; set it to the {rendered} reference"


def check_routes(routes: dict, config_path: Path) -> list:
    config_text = config_path.read_text(encoding="utf-8")
    report = []
    for name, rendered in routes.items():
        installed = installed_route(name)
        if not isinstance(installed, dict):
            report.append((False, f"route {name}: not installed"))
            continue
        keys = sorted((rendered.keys() | installed.keys()) - {"secret"})
        differing = [key for key in keys if rendered.get(key) != installed.get(key)]
        secret_ok, secret = secret_status(rendered["secret"], installed.get("secret"), config_text)
        shape = f"differs in {', '.join(differing)}" if differing else "matches"
        report.append((secret_ok and not differing, f"route {name}: {shape}; {secret}"))
    return report


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--settings-json", help="read settings from this JSON file instead of `hermes config get`")
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the rendered config with the installed one; prints no secret value",
    )
    args = parser.parse_args(argv)
    try:
        routes = build_routes(load_settings(args.settings_json))
    except SettingsError as exc:
        print(f"render.py: invalid settings: {exc}", file=sys.stderr)
        return 1
    if not args.check:
        print(json.dumps(build_config(routes), indent=2))
        return 0
    report = [*check_routes(routes, hermes_home() / "config.yaml"), check_rate_limit()]
    for _, line in report:
        print(line)
    return 0 if all(ok for ok, _ in report) else 1


if __name__ == "__main__":
    sys.exit(main())
