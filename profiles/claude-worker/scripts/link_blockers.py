"""Order a card behind the cards of the Linear issues that block its issue.

Linear's `blockedBy` and `blocks` relations become Hermes parent links. Hermes holds a
child card in `todo` until every parent card is `done` or `archived`, and a card is
`done` once its pull request merges, so a chain of delegated issues runs in order with
no worker run asking a person what to do.

Standard input is the issue as the `linear_issue` tool prints it. The command reads only its
`blockedBy` and `blocks`, each a list of issues with `id`, `identifier`, `gitBranchName`,
and `stateType`.

`--parents` is for a card that does not exist yet. It prints
`{"parents": [task id, ...], "skipped": [...], "unassigned": bool}` for the `--parent` flags of
`hermes kanban create`, which links the card before the dispatcher can claim it. `unassigned` is
true when a blocker is delegated to the same agent but has no card yet. Create the card with no
assignee then, because the dispatcher skips an unassigned `ready` card and the blocker's own
`--task` run, which links it and assigns it, is what lets it start.

`--task <id>` links the existing card both ways: the cards of its blockers become its
parents, and it becomes the parent of the cards of the issues it blocks. It prints
`{"linked", "skipped", "cycle", "released"}` and exits 0. Run it again after a failure: a link that
exists is not made twice. Each issue is skipped with a reason when it has no live card
on the board (never delegated, or archived), is completed or canceled, or when the
child card has left `triage`, `todo`, and `ready`, because a started worker has already
read its issue and Hermes refuses to link a running card. When a link would close a
cycle, with the links the board already has or with the links this run would make, the command
links nothing, removes nothing, and prints the issues in `cycle`. Each card that was waiting
unassigned, the card itself or the card of an issue it blocks, gets the worker assigned and is
listed in `released`, so a cycle or a missing blocker is left to the worker's blocker gate. It
exits 1 with `{"error": reason}` when Hermes fails.

`--release` is for an issue that will have no card, such as one that is not ready for the agent or
is no longer delegated. It assigns the waiting cards of the issues in `blocks` and prints
`{"released": [task id, ...]}`.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

LINKABLE_CHILD_STATUSES = ("triage", "todo", "ready")
RELEASABLE_CARD_STATUSES = ("todo", "ready")
FINISHED_ISSUE_STATES = ("completed", "canceled")
ASSIGNEE = "claude-worker"


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

    def reaches(self, from_id: str, to_id: str, planned: list[tuple[str, str]]) -> bool:
        """Whether parent-to-child links, the board's and the planned ones, lead from one card down to another."""
        stack = [from_id]
        seen = set()
        while stack:
            node = stack.pop()
            if node == to_id:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend([*self.show(node)["children"], *(child for parent, child in planned if parent == node)])
        return False

    def release(self, task_id: str) -> list[str]:
        """Assign the worker to a card that waits unassigned, so the dispatcher can start it."""
        task = self.show(task_id)["task"]
        if task["assignee"] is not None or task["status"] not in RELEASABLE_CARD_STATUSES:
            return []
        run(*self.kanban, "assign", task_id, ASSIGNEE)
        self.forget(task_id)
        return [task_id]


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


def waits_for_card(board: Board, issues: list[dict], delegate: str) -> bool:
    """Whether an issue delegated to the same agent is still to get its card, which will link this one."""
    return any(
        issue["delegate"] == delegate and issue["stateType"] not in FINISHED_ISSUE_STATES and board.card_for(issue) is None
        for issue in issues
    )


def link_all(board: Board, task_id: str, relations: dict) -> dict:
    result: dict = {"linked": [], "skipped": [], "cycle": None, "released": []}
    pairs = [(issue, parent, task_id) for issue, parent in blocker_cards(board, relations["blockedBy"], result["skipped"])]
    waiting = []
    for issue in relations["blocks"]:
        card = live_card(board, issue, result["skipped"])
        if card is not None:
            pairs.append((issue, task_id, card["id"]))
            waiting.append(card["id"])
    planned: list[tuple[str, str]] = []
    cycle: list[str] = []
    for issue, parent, child in pairs:
        if parent == child or child in board.show(parent)["children"]:
            continue
        child_status = board.show(child)["task"]["status"]
        if child_status not in LINKABLE_CHILD_STATUSES:
            result["skipped"].append({"issue": issue["identifier"], "reason": f"the child card is {child_status}"})
        elif board.reaches(child, parent, planned):
            cycle.append(issue["identifier"])
        else:
            planned.append((parent, child))
            result["linked"].append({"issue": issue["identifier"], "parent": parent, "child": child})
    if cycle:
        result["linked"] = []
        result["cycle"] = cycle
    else:
        for link in result["linked"]:
            run(*board.kanban, "link", link["parent"], link["child"])
            board.forget(link["parent"])
            board.forget(link["child"])
    if not waits_for_card(board, relations["blockedBy"], relations["delegate"]):
        result["released"] += board.release(task_id)
    for child in waiting:
        result["released"] += board.release(child)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--parents", action="store_true", help="print the parent cards for a card to be created")
    mode.add_argument("--task", help="link this existing card both ways")
    mode.add_argument("--release", action="store_true", help="assign the waiting cards of the issues this issue blocks")
    args = parser.parse_args(argv)
    kanban = [os.environ.get("HERMES_BIN", "hermes"), "kanban"]
    try:
        relations = json.load(sys.stdin)
        relations = {key: relations[key] for key in ("blockedBy", "blocks", "delegate")}
        board = Board(kanban)
        if args.parents:
            skipped: list[dict] = []
            parents = [card_id for _, card_id in blocker_cards(board, relations["blockedBy"], skipped)]
            result = {"parents": parents, "skipped": skipped,
                      "unassigned": waits_for_card(board, relations["blockedBy"], relations["delegate"])}
        elif args.release:
            released = []
            for issue in relations["blocks"]:
                card = board.card_for(issue)
                if card is not None:
                    released += board.release(card["id"])
            result = {"released": released}
        else:
            result = link_all(board, args.task, relations)
    except (Failed, KeyError, ValueError, TypeError) as failure:
        print(json.dumps({"error": str(failure)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
