"""Order a card behind the cards of the Linear issues that block its issue.

Linear's `blockedBy` and `blocks` relations become Hermes parent links. Hermes holds a
child card in `todo` until every parent card is `done` or `archived`, and a card is
`done` once its pull request merges, so a chain of delegated issues runs in order with
no worker run asking a person what to do.

Standard input is the issue as the `linear_issue` tool prints it. The command reads only its
`blockedBy` and `blocks`, each a list of issues with `id`, `identifier`, `gitBranchName`,
and `stateType`.

`--parents` is for a card that does not exist yet. It prints
`{"parents": [task id, ...], "skipped": [...]}` for the `--parent` flags of
`hermes kanban create`, which links the card before the dispatcher can claim it.

`--task <id>` links the existing card both ways: the cards of its blockers become its
parents, and it becomes the parent of the cards of the issues it blocks. It prints
`{"linked", "skipped", "cycle"}` and exits 0. Run it again after a failure: a link that
exists is not made twice. Each issue is skipped with a reason when it has no live card
on the board (never delegated, or archived), is completed or canceled, or when the
child card has left `triage`, `todo`, and `ready`, because a started worker has already
read its issue and Hermes refuses to link a running card. When the blockers form a cycle
the command links nothing, removes the links that closed the cycle, and prints the
issues in `cycle`. It exits 1 with `{"error": reason}` when Hermes fails.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

LINKABLE_CHILD_STATUSES = ("triage", "todo", "ready")
FINISHED_ISSUE_STATES = ("completed", "canceled")


class Failed(Exception):
    pass


def run(*command: str) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True)
    except OSError as failure:
        raise Failed(f"{command[0]} could not run: {failure.strerror or failure}") from failure
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no error output"
        raise Failed(f"{' '.join(command[:3])} failed: {detail}")
    return proc.stdout


class Board:
    def __init__(self, kanban: list[str]) -> None:
        self.kanban = kanban
        self.cards = json.loads(run(*kanban, "list", "--json"))
        self.shown: dict[str, dict] = {}

    def show(self, task_id: str) -> dict:
        if task_id not in self.shown:
            self.shown[task_id] = json.loads(run(*self.kanban, "show", task_id, "--json"))
        return self.shown[task_id]

    def forget(self, task_id: str) -> None:
        self.shown.pop(task_id, None)

    def card_for(self, issue: dict) -> dict | None:
        """The live card of an issue, found by the keys `triage-linear.md` lists, in that order."""
        keys = (
            lambda card: card["branch_name"] == issue["gitBranchName"],
            lambda card: card["branch_name"] == f"{issue['gitBranchName']}-takeover",
            lambda card: f"linear_issue_id: {issue['id']}" in (card["body"] or "").splitlines(),
            lambda card: (card["title"] or "").startswith(f"{issue['identifier']}:"),
        )
        for key in keys:
            for card in self.cards:
                if key(card):
                    return card
        return None

    def path(self, from_id: str, to_id: str) -> list[tuple[str, str]] | None:
        """The parent-to-child links that lead from one card down to another, or None."""
        stack = [(from_id, [])]
        seen = set()
        while stack:
            node, edges = stack.pop()
            if node == to_id:
                return edges
            if node in seen:
                continue
            seen.add(node)
            stack.extend((child, [*edges, (node, child)]) for child in self.show(node)["children"])
        return None


def live_card(board: Board, issue: dict, skipped: list[dict]) -> dict | None:
    if issue["stateType"] in FINISHED_ISSUE_STATES:
        skipped.append({"issue": issue["identifier"], "reason": f"the issue is {issue['stateType']}"})
        return None
    card = board.card_for(issue)
    if card is None:
        skipped.append({"issue": issue["identifier"], "reason": "no live card"})
    return card


def blocker_cards(board: Board, issues: list[dict], skipped: list[dict]) -> list[tuple[dict, str]]:
    found = []
    for issue in issues:
        card = live_card(board, issue, skipped)
        if card is not None and card["status"] == "done":
            skipped.append({"issue": issue["identifier"], "reason": "its card is done"})
        elif card is not None:
            found.append((issue, card["id"]))
    return found


def link_all(board: Board, task_id: str, relations: dict) -> dict:
    result: dict = {"linked": [], "skipped": [], "cycle": None}
    pairs = [(issue, parent, task_id) for issue, parent in blocker_cards(board, relations["blockedBy"], result["skipped"])]
    for issue in relations["blocks"]:
        card = live_card(board, issue, result["skipped"])
        if card is not None:
            pairs.append((issue, task_id, card["id"]))
    cycle: list[str] = []
    for issue, parent, child in pairs:
        if parent == child or child in board.show(parent)["children"]:
            continue
        child_status = board.show(child)["task"]["status"]
        if child_status not in LINKABLE_CHILD_STATUSES:
            result["skipped"].append({"issue": issue["identifier"], "reason": f"the child card is {child_status}"})
            continue
        closing = board.path(child, parent)
        if closing is not None:
            cycle.append(issue["identifier"])
            for edge in closing:
                run(*board.kanban, "unlink", *edge)
                board.forget(edge[0])
                board.forget(edge[1])
            continue
        run(*board.kanban, "link", parent, child)
        board.forget(parent)
        board.forget(child)
        result["linked"].append({"issue": issue["identifier"], "parent": parent, "child": child})
    if cycle:
        for link in result["linked"]:
            run(*board.kanban, "unlink", link["parent"], link["child"])
        result["linked"] = []
        result["cycle"] = cycle
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--parents", action="store_true", help="print the parent cards for a card to be created")
    mode.add_argument("--task", help="link this existing card both ways")
    args = parser.parse_args(argv)
    kanban = [os.environ.get("HERMES_BIN", "hermes"), "kanban"]
    try:
        relations = json.load(sys.stdin)
        relations = {"blockedBy": list(relations["blockedBy"]), "blocks": list(relations["blocks"])}
        board = Board(kanban)
        if args.parents:
            skipped: list[dict] = []
            parents = [card_id for _, card_id in blocker_cards(board, relations["blockedBy"], skipped)]
            result = {"parents": parents, "skipped": skipped}
        else:
            result = link_all(board, args.task, relations)
    except (Failed, KeyError, ValueError, TypeError) as failure:
        print(json.dumps({"error": str(failure)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
