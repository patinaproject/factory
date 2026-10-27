from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Optional

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_hyphenated_plugin_dir_as_package():
    spec = importlib.util.spec_from_file_location(
        "linear_agent_session", os.path.join(PLUGIN_DIR, "__init__.py"), submodule_search_locations=[PLUGIN_DIR]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Optional[list] = None) -> int:
    plugin = _load_hyphenated_plugin_dir_as_package()
    parser = argparse.ArgumentParser(prog="cli.py")
    commands = parser.add_subparsers(dest="command", required=True)

    activity = commands.add_parser("activity", help="post an agent activity")
    activity.add_argument("--agent-session-id", required=True)
    activity.add_argument("--type", required=True, choices=plugin.linear.ACTIVITY_TYPES)
    activity.add_argument("--body")
    activity.add_argument("--action")
    activity.add_argument("--parameter")
    activity.add_argument("--result")
    activity.add_argument("--ephemeral", action="store_true")
    activity.add_argument(
        "--external-url",
        action="append",
        default=[],
        metavar="LABEL=URL",
        help="replace the session's external URLs; repeatable",
    )

    issue = commands.add_parser("issue", help="read a Linear issue")
    issue.add_argument("issue")

    session = commands.add_parser("session-create", help="open a new agent session on an issue")
    session.add_argument("issue")

    delegated = commands.add_parser("delegated", help="list issues delegated to the agent app")
    delegated.add_argument("--include-closed", action="store_true")

    undelegate = commands.add_parser("undelegate", help="remove the agent app as an issue's delegate")
    undelegate.add_argument("issue")

    exit_check = commands.add_parser(
        "triage-exit", help="archive the triage card of an issue whose queued update only moved it out of Triage"
    )
    exit_check.add_argument("--payload-file", required=True, help="the Linear webhook payload, as JSON")

    intent = commands.add_parser("triage-intent", help="print the computed triage intent block for a Linear webhook payload")
    intent.add_argument("--payload-file", required=True, help="the Linear webhook payload, as JSON")
    intent.add_argument("--now", help="the delivery time as ISO 8601, for a replay; default is the payload's timestamp")

    args = parser.parse_args(argv)
    if args.command == "triage-exit":
        return _triage_exit(plugin, args.payload_file)
    if args.command == "triage-intent":
        return _triage_intent(plugin, args.payload_file, args.now)
    if args.command == "activity":
        tool_args = {
            "agent_session_id": args.agent_session_id,
            "type": args.type,
            "body": args.body,
            "action": args.action,
            "parameter": args.parameter,
            "result": args.result,
            "ephemeral": args.ephemeral,
            "external_urls": [_external_url(value) for value in args.external_url],
        }
        output = plugin.tools.run_activity(plugin.linear.client_from_env, tool_args)
    elif args.command == "session-create":
        output = plugin.tools.run_session_create(plugin.linear.client_from_env, {"issue": args.issue})
    elif args.command == "delegated":
        output = plugin.tools.run_delegated_issues(
            plugin.linear.client_from_env, {"include_closed": args.include_closed}
        )
    elif args.command == "undelegate":
        output = plugin.tools.run_undelegate(plugin.linear.client_from_env, {"issue": args.issue})
    else:
        output = plugin.tools.run_issue(plugin.linear.client_from_env, {"issue": args.issue})
    print(output)
    return 1 if "error" in json.loads(output) else 0


def _triage_exit(plugin, payload_file: str) -> int:
    """Print `handled` true only when the card was archived or left to its worker, so the caller skips the turn.

    webhook-retry runs this from a cron script, outside the Hermes process, so it reaches Hermes through its CLI.
    The reason stays out of the output, because an archive error can quote a command's output.
    """
    triage_exit = plugin.triage_exit
    try:
        with open(payload_file, encoding="utf-8") as handle:
            payload = json.load(handle)
        verdict = triage_exit.handler(
            triage_exit.configured_app_user_id,
            plugin.linear.client_from_env,
            triage_exit.listed_cards,
            lambda: Path(os.environ["HERMES_HOME"]),
        )(payload)
    except Exception as failure:
        verdict = triage_exit.Verdict("failed", type(failure).__name__)
    print(json.dumps({"handled": verdict.handled, "action": verdict.action, "task_id": verdict.task_id}))
    return 0


def _triage_intent(plugin, payload_file: str, now: Optional[str]) -> int:
    triage_exit, triage_intent = plugin.triage_exit, plugin.triage_intent
    try:
        with open(payload_file, encoding="utf-8") as handle:
            payload = json.load(handle)
        moment = triage_intent.parse_time(now) if now else None
        intent = triage_intent.compute(
            payload,
            app_user_id=triage_exit.configured_app_user_id,
            client=plugin.linear.client_from_env,
            cards=triage_exit.listed_cards,
            now=moment,
        )
    except Exception as failure:
        intent = triage_intent.error_intent(type(failure).__name__)
    print(triage_intent.render(intent), end="")
    return 0


def _external_url(value: str) -> dict:
    label, sep, url = value.partition("=")
    if not sep or not label or not url:
        raise SystemExit(f"--external-url expects LABEL=URL, got {value!r}")
    return {"label": label, "url": url}


if __name__ == "__main__":
    sys.exit(main())
