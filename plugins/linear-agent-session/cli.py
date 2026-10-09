"""Shell entry for the plugin's tools: same arguments and JSON output as linear_agent_activity and linear_issue."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from typing import Optional

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_plugin():
    # The plugin directory name has a hyphen, so load it as a package by path to keep its relative imports.
    spec = importlib.util.spec_from_file_location(
        "linear_agent_session", os.path.join(PLUGIN_DIR, "__init__.py"), submodule_search_locations=[PLUGIN_DIR]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Optional[list] = None) -> int:
    plugin = _load_plugin()
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

    args = parser.parse_args(argv)
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
    else:
        output = plugin.tools.run_issue(plugin.linear.client_from_env, {"issue": args.issue})
    print(output)
    return 1 if "error" in json.loads(output) else 0


def _external_url(value: str) -> dict:
    label, sep, url = value.partition("=")
    if not sep or not label or not url:
        raise SystemExit(f"--external-url expects LABEL=URL, got {value!r}")
    return {"label": label, "url": url}


if __name__ == "__main__":
    sys.exit(main())
