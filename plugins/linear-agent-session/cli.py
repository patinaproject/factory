from __future__ import annotations

import argparse
import datetime as dt
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
        help="add to the session's external URLs; repeatable",
    )

    issue = commands.add_parser("issue", help="read a Linear issue")
    issue.add_argument("issue")

    session_read = commands.add_parser("session", help="read a Linear agent session and every activity in it")
    session_read.add_argument("session", help="the session's URL, UUID, or slugId")

    session = commands.add_parser("session-create", help="open a new agent session on an issue")
    session.add_argument("issue")
    session.add_argument("--purpose", required=True, choices=["triage", "work"])

    delegated = commands.add_parser("delegated", help="list issues delegated to the agent app")
    delegated.add_argument("--include-closed", action="store_true")

    undelegate = commands.add_parser("undelegate", help="remove the agent app as an issue's delegate")
    undelegate.add_argument("issue")

    exit_check = commands.add_parser(
        "triage-exit", help="archive the triage card of an issue whose queued update only moved it out of Triage"
    )
    exit_check.add_argument("--payload-file", required=True, help="the Linear webhook payload, as JSON")

    teammate = commands.add_parser(
        "teammate-reply", help="say whether a Linear webhook payload is a session reply addressed to a teammate"
    )
    teammate.add_argument("--payload-file", required=True, help="the Linear webhook payload, as JSON")

    comment_skip = commands.add_parser(
        "comment-skip", help="say whether a Linear Comment payload resumes no card, so a replay needs no turn"
    )
    comment_skip.add_argument("--payload-file", required=True, help="the Linear webhook payload, as JSON")

    reask_exit = commands.add_parser(
        "reask-exit", help="ask the person again for a reply re-ask whose Hermes exited before its turn-end hook"
    )
    reask_exit.add_argument("--agent-session-id", required=True)

    intent = commands.add_parser("triage-intent", help="print the computed triage intent block for a Linear webhook payload")
    intent.add_argument("--payload-file", required=True, help="the Linear webhook payload, as JSON")
    intent.add_argument("--now", help="the delivery time as ISO 8601, for a replay; default is the payload's timestamp")

    commands.add_parser(
        "missed-prompts",
        help="print a person's session replies that no webhook delivered and nobody has answered, as JSON, and claim them",
    )

    owner = commands.add_parser("review-owner", help="name who reviews an issue's pull request and their GitHub user id")
    owner.add_argument("--issue", required=True, help="the issue's UUID or identifier")

    authority = commands.add_parser(
        "pr-authority", help="say whether a GitHub sender is the assignee, the delegator, or a configured operator of the Linear issue a card works"
    )
    authority.add_argument("--task", required=True, help="the card's task id")
    authority.add_argument("--sender-id", required=True, help="the GitHub user id of the event's sender")

    args = parser.parse_args(argv)
    if args.command == "triage-exit":
        return _triage_exit(plugin, args.payload_file)
    if args.command == "teammate-reply":
        return _teammate_reply(plugin, args.payload_file)
    if args.command == "comment-skip":
        return _comment_skip(plugin, args.payload_file)
    if args.command == "reask-exit":
        replies = plugin.replies
        log = Path(replies.hermes_home()) / "logs" / replies.EXIT_LOG
        replies.reask_exit(plugin.linear.client_from_env, args.agent_session_id, log)
        return 0
    if args.command == "missed-prompts":
        return _missed_prompts(plugin)
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
    elif args.command == "session":
        output = plugin.tools.run_session_read(plugin.linear.client_from_env, {"session": args.session})
    elif args.command == "session-create":
        output = plugin.tools.run_session_create(
            plugin.linear.client_from_env,
            {"issue": args.issue, "purpose": args.purpose},
            plugin.opened_sessions.store_dir(os.environ),
        )
    elif args.command == "delegated":
        output = plugin.tools.run_delegated_issues(
            plugin.linear.client_from_env, {"include_closed": args.include_closed}
        )
    elif args.command == "review-owner":
        output = plugin.tools.run_review_owner(plugin.linear.client_from_env, {"issue": args.issue})
    elif args.command == "pr-authority":
        output = plugin.tools.run_pr_authority(
            plugin.linear.client_from_env,
            plugin.triage_exit.card_body,
            plugin.triage_exit.configured_app_user_id,
            plugin.triage_exit.configured_operator_user_ids,
            {"task": args.task, "sender_id": args.sender_id},
        )
    elif args.command == "undelegate":
        output = plugin.tools.run_undelegate(plugin.linear.client_from_env, {"issue": args.issue})
    else:
        output = plugin.tools.run_issue(plugin.linear.client_from_env, lambda: [], {"issue": args.issue})
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


def _teammate_reply(plugin, payload_file: str) -> int:
    with open(payload_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    print(json.dumps({"addressed": plugin.addressed_to_teammate(payload)}))
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
            operator_user_ids=triage_exit.configured_operator_user_ids,
            client=plugin.linear.client_from_env,
            cards=triage_exit.listed_cards,
            history=triage_exit.listed_history,
            now=moment,
            opened_by_factory=lambda sid: plugin.opened_sessions.recorded(plugin.opened_sessions.store_dir(os.environ), sid),
        )
    except Exception as failure:
        intent = triage_intent.error_intent(type(failure).__name__)
    try:
        plugin.forwards.receive(plugin.forwards.store_dir(os.environ), intent)
    except Exception as failure:
        print(f"cli.py: reply {intent.reply_key} was not recorded, so its forward will refuse: {type(failure).__name__}", file=sys.stderr)
    print(triage_intent.render(intent), end="")
    return 0


def _positive_setting(triage_exit, name: str, default: float) -> float:
    value = triage_exit.configured_setting(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _missed_prompts(plugin) -> int:
    """Print the claimed prompts as one JSON object. Any failure prints its reason to stderr and nothing to stdout."""
    triage_exit, triage_intent = plugin.triage_exit, plugin.triage_intent
    try:
        window = dt.timedelta(hours=_positive_setting(triage_exit, "missed_prompt_window_hours", 24))
        grace = dt.timedelta(minutes=_positive_setting(triage_exit, "missed_prompt_grace_minutes", 10))
        prompts = plugin.missed_prompts.sweep(
            client=plugin.linear.client_from_env(),
            app_user_id=triage_exit.configured_app_user_id(),
            compute_intent=lambda payload, moment: triage_intent.compute(
                payload,
                app_user_id=triage_exit.configured_app_user_id,
                operator_user_ids=triage_exit.configured_operator_user_ids,
                client=plugin.linear.client_from_env,
                cards=triage_exit.listed_cards,
                history=triage_exit.listed_history,
                now=moment,
                opened_by_factory=lambda sid: plugin.opened_sessions.recorded(plugin.opened_sessions.store_dir(os.environ), sid),
            ),
            store=plugin.idempotency.store_dir(os.environ),
            marker=plugin.missed_prompts.marker_path(os.environ),
            now=dt.datetime.now(dt.timezone.utc),
            window=window,
            grace=grace,
        )
    except Exception as failure:
        print(plugin.blocks.hermes_redact(f"{type(failure).__name__}: {failure}"), file=sys.stderr)
        return 1
    print(json.dumps({"prompts": prompts}))
    return 0


def _comment_skip(plugin, payload_file: str) -> int:
    triage_exit, triage_intent = plugin.triage_exit, plugin.triage_intent
    with open(payload_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    intent = triage_intent.compute(
        payload,
        app_user_id=triage_exit.configured_app_user_id,
        operator_user_ids=triage_exit.configured_operator_user_ids,
        client=plugin.linear.client_from_env,
        cards=triage_exit.listed_cards,
        history=triage_exit.listed_history,
    )
    print(json.dumps({"skip": intent.action != "comment_resume"}))
    return 0


def _external_url(value: str) -> dict:
    label, sep, url = value.partition("=")
    if not sep or not label or not url:
        raise SystemExit(f"--external-url expects LABEL=URL, got {value!r}")
    return {"label": label, "url": url}


if __name__ == "__main__":
    sys.exit(main())
