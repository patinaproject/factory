"""A file-backed mock issue tracker that a trial reaches as a command, never over the network.

The harness copies this file into each trial's services folder and points
TRACKER_STATE at that trial's own state file. Every write is appended to the
state's `writes` log with the actor from TRACKER_ACTOR, so a grader can tell an
agent's write from the person's history the fixture started with. Stdlib only,
because it runs inside the sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PRIORITIES = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracker")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("issue")
    priority = commands.add_parser("set-priority")
    priority.add_argument("issue")
    priority.add_argument("value", type=int, choices=sorted(PRIORITIES))
    comment = commands.add_parser("comment")
    comment.add_argument("issue")
    comment.add_argument("--body", required=True)
    args = parser.parse_args(argv)

    path = Path(os.environ["TRACKER_STATE"])
    actor = os.environ.get("TRACKER_ACTOR", "agent")
    state = load(path)
    issue = state["issues"].get(args.issue)
    if issue is None:
        print(f"no issue {args.issue}", file=sys.stderr)
        return 1
    if args.command == "show":
        print(json.dumps(issue, indent=2, sort_keys=True))
        return 0
    if args.command == "set-priority":
        issue["priority"] = args.value
        issue["priority_history"].append({"actor": actor, "value": args.value})
        state["writes"].append({"op": "set-priority", "issue": args.issue, "actor": actor, "value": args.value})
    else:
        issue["comments"].append({"author": actor, "body": args.body})
        state["writes"].append({"op": "comment", "issue": args.issue, "actor": actor, "body": args.body})
    save(path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
