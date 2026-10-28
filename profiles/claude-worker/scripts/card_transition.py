"""Move a Kanban card through the transitions that triage makes, in the order Hermes needs.

Usage: card_transition.py [--board <board>] <command> <task id> [options]

- `resume`: take a card out of `triage`. Hermes' `kanban specify` is the only command that leaves
  `triage`, and it rewrites the card's title and body and makes the card dispatchable. The card is
  unassigned first, because the dispatcher skips a card with no assignee, its kept title and body are
  restored, and only then is it assigned to `claude-worker`. That assignment is the handoff that lifts
  Hermes' hold on a card whose comments name a pull request. `specify` asks the model through the
  proxy, and Hermes strips the proxy token from the triage agent's commands, so `specify` alone gets
  the token, from the gateway's broker, in the variable `claude_session.auth_token_env` names.
- `triage-reply`: resume a card on the `triage` board after a person replied: a `blocked` card is
  unassigned and unblocked, a `triage` card is resumed without the assignment. `dispatch-triage`
  assigns it when a triage slot is free.
- `unblock --repo <r> --branch <b> --reason <text>`: unblock a card. When the branch has a pull
  request, Hermes holds the `ready` card for 24 hours after a comment that names it, and only the
  review reopen lifts the hold, so the card is unblocked, sent to review, and reopened.
- `reopen --reason <text>`: hand a `review` card back to its worker.
- `stop`: block a card whatever it is doing. Hermes blocks only a `ready` or `running` card, so a
  `review` card is reopened first.
- `merged --pr <url> --linear-state <stateType or unreadable>`: finish a card whose pull request
  merged. A refused completion is a refused merge when the newest `pr_acceptance` event is for this
  pull request at its head and names a check that did not pass, or names no required checks. A
  merged pull request never runs its checks again, so the card then waits for a person and is
  archived once the Linear issue is completed. The card is never assigned once `specify` has
  succeeded, because an assignment lifts the `active_pr` hold and a worker would start on a merged
  pull request.

Every command prints one JSON line. It exits 0 with `{"action", "post", ...}`, or 1 with
`{"error": reason}`. The actions:

| action | meaning | extra keys |
| --- | --- | --- |
| `none` | the card's status called for no change | `status` |
| `resumed` | the card left `triage` with its own title and body | `assigned` |
| `unblocked` | the card is ready for its worker again | `pull_request` |
| `reopened` | the `review` card went back to its worker | |
| `stopped` | the card is blocked, or was already | `status` |
| `completed` | Hermes accepted the merged pull request and the card is `done` | |
| `commented` | the merge left the card as it was, with `reason` commented on it | `reason` |
| `archived` | a refused merge whose Linear issue is completed | |
| `held` | a refused merge waits for a person: a `ready` card is blocked, a `review` or `blocked` card stays | `reason` |

`post` names the Linear agent activity the caller still posts: `response` after `completed`,
`elicitation` with `reason` as its body after a `held` that Hermes' block hook did not post, and
null otherwise. A card comment names a pull request as `<owner>/<name>#<number>`, never by URL,
because Hermes treats a card whose comments hold a pull request URL as already having one.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Callable, NamedTuple

import factory_settings
import merged_checks
from archive_card import Failed, archive, kanban_command, last_line, run_kanban

WORKER = "claude-worker"
STOPPED = "stopped"
PULL_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/pull/(\d+)(?:[/#?][^\s).,]*)?")
NOT_REFUSED = frozenset({"success", "pending"})


def pull_ref(text: str) -> str:
    return PULL_URL.sub(lambda match: f"{match.group(1)}#{match.group(2)}", text)


def result(action: str, post: str | None = None, **extra) -> dict:
    return {"action": action, "post": post, **extra}


class SpecifyFailed(Failed):
    pass


class Card:
    """One card on one board, with the `hermes kanban` calls triage makes on it."""

    def __init__(self, board: str | None, task_id: str):
        self.kanban = kanban_command(board)
        self.id = task_id

    def show(self) -> dict:
        return json.loads(self.call("show", "--json"))

    def status(self) -> str:
        return self.show()["task"]["status"]

    def call(self, *args: str, env: dict | None = None) -> str:
        try:
            return run_kanban(*self.kanban, args[0], self.id, *args[1:], env=env)
        except Failed as failure:
            raise Failed(f"hermes kanban {args[0]} failed: {last_line(failure.stderr)}", failure.stderr) from failure

    def comment(self, text: str) -> str:
        text = pull_ref(text)
        self.call("comment", text)
        return text

    def archive(self) -> None:
        archive(self.kanban, self.id)


def specify_environment() -> dict:
    """This environment with the proxy token, which Hermes strips from the triage agent's commands."""
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    sys.path.insert(0, str(Path(home) / "plugins" / "linear-agent-session"))
    import broker_client

    try:
        variable = factory_settings.token_variables(factory_settings.read(raw=False))[0]
    except factory_settings.SettingsError as error:
        raise SpecifyFailed(f"specify needs the proxy token: {error}") from None
    if not variable:
        raise SpecifyFailed("specify needs the proxy token, and claude_session.auth_token_env is not set")
    try:
        token = broker_client.request(broker_client.socket_path(os.environ), "proxy_token")["token"]
    except broker_client.BrokerError as error:
        raise SpecifyFailed(f"specify needs the proxy token: {error}") from None
    return {**os.environ, variable: token}


def leave_triage(card: Card) -> None:
    """Run `specify` with the card unassigned and restore its title and body; the card stays unassigned."""
    task = card.show()["task"]
    if task["status"] != "triage":
        raise Failed(f"card {card.id} is {task['status']}, not triage")
    env = specify_environment()
    card.call("assign", "none")
    try:
        card.call("specify", env=env)
    except Failed as failure:
        card.call("assign", WORKER)
        raise SpecifyFailed(f"{failure}; the card stays in triage") from failure
    restore = ("edit", "--title", task["title"], "--body", task["body"])
    try:
        card.call(*restore)
    except Failed:
        try:
            card.call(*restore)
        except Failed as failure:
            raise Failed(f"{failure}; the card is unassigned and holds the title and body specify wrote") from failure


def resume(card: Card, assign: bool = True) -> dict:
    leave_triage(card)
    if assign:
        card.call("assign", WORKER)
    return result("resumed", assigned=assign)


def triage_reply(card: Card) -> dict:
    status = card.status()
    if status == "blocked":
        card.call("assign", "none")
        card.call("unblock")
        return result("unblocked", pull_request=False)
    if status == "triage":
        return resume(card, assign=False)
    return result("none", status=status)


def has_pull_request(repo: str, branch: str) -> bool:
    try:
        proc = subprocess.run(["gh", "pr", "view", branch, "-R", repo, "--json", "state"], capture_output=True, text=True)
    except OSError as failure:
        raise Failed(f"gh could not run: {failure.strerror or failure}") from failure
    if proc.returncode == 0:
        return True
    if "no pull requests found" in proc.stderr:
        return False
    raise Failed(f"gh pr view failed: {last_line(proc.stderr)}")


def unblock(card: Card, repo: str, branch: str, reason: str) -> dict:
    pull_request = has_pull_request(repo, branch)
    card.call("unblock")
    if pull_request:
        card.call("request-review")
        card.call("reopen-review", "--reason", reason)
    return result("unblocked", pull_request=pull_request)


def reopen(card: Card, reason: str) -> dict:
    card.call("reopen-review", "--reason", reason)
    return result("reopened")


def stop(card: Card) -> dict:
    status = card.status()
    if status == "review":
        card.call("reopen-review", "--reason", STOPPED)
        status = card.status()
    if status in ("ready", "running", "todo"):
        card.call("block", STOPPED)
        status = card.status()
    return result("stopped", status=status)


class Case(Enum):
    """Why Hermes refused a merged pull request: its card comment marker and the reason a person reads."""

    REAL = (
        "Merge refused by Hermes: required checks failed: {names}.",
        "Pull request {ref} merged, but required checks failed: {names}. Is the work done? If it is, mark the "
        "Linear issue Done, and the card is archived.",
    )
    SUPERSEDED = (
        "Merge refused by Hermes: Hermes counted failed runs that newer passing runs replaced: {names}.",
        "Pull request {ref} merged and its required checks passed on their newest runs. Hermes counted failed "
        "runs that newer passing runs replaced: {names}, so it cannot finish the card. Mark the Linear issue "
        "Done, and the card is archived.",
    )
    NO_REQUIRED_CHECKS = (
        "Merge refused by Hermes: the base branch has no required checks.",
        "Pull request {ref} merged, but its base branch has no required checks, so Hermes cannot finish the "
        "card. Is the work done? If it is, mark the Linear issue Done, and the card is archived.",
    )

    def __init__(self, marker: str, reason: str):
        self.marker = marker
        self.reason = reason


class Merge(NamedTuple):
    url: str
    ref: str
    linear_state: str


def read_pull_request(url: str) -> dict:
    try:
        return merged_checks.read_pull_request(url)
    except (merged_checks.Failed, ValueError):
        return merged_checks.read_pull_request(url)


def commented(card: Card, reason: str) -> dict:
    return result("commented", reason=card.comment(reason))


def refusal(card: Card, merge: Merge, error: str) -> dict:
    return commented(card, f"Pull request {merge.ref} merged, but Hermes refused to complete the card: {error}")


def refused_merge(card: Card, merge: Merge, error: str) -> dict:
    try:
        pull = read_pull_request(merge.url)
    except (merged_checks.Failed, ValueError) as failure:
        return commented(card, f"Pull request {merge.ref} merged, but its checks could not be read: {failure}")
    events = [event for event in card.show()["events"] if event["kind"] == "pr_acceptance"]
    receipt = events[-1]["payload"] if events else {}
    if receipt.get("pr_url") != merge.url or receipt.get("head_sha") != pull["headRefOid"]:
        return refusal(card, merge, error)
    checks = receipt.get("checks") or []
    refused = [check["name"] for check in checks if check["classification"] not in NOT_REFUSED]
    if receipt.get("classification") == "missing" and not checks:
        case, names = Case.NO_REQUIRED_CHECKS, []
    elif refused:
        sorted_checks = merged_checks.sort_checks(pull["statusCheckRollup"] or [], refused)
        case = Case.REAL if sorted_checks["real"] else Case.SUPERSEDED
        names = sorted_checks["real"] or sorted_checks["superseded"]
    else:
        return refusal(card, merge, error)
    marker = f"{case.marker.format(names=', '.join(names))} {merge.ref}"
    if merge.linear_state == "completed":
        card.comment(marker)
        card.archive()
        return result("archived")
    if merge.linear_state == "unreadable":
        marker += " The Linear issue could not be read, so the card was not archived."
    card.comment(marker)
    return hold(card, case.reason.format(ref=merge.ref, names=", ".join(names)))


def hold(card: Card, reason: str) -> dict:
    card.call("assign", "none")
    status = card.status()
    if status == "ready":
        try:
            card.call("block", reason, "--kind", "needs_input")
        except Failed:
            return result("held", "elicitation", reason=reason)
        return result("held", reason=reason)
    if status == "blocked":
        # Hermes types a blocked card's block only while it has no kind and refuses otherwise. Either way the
        # card stays blocked and Hermes posts nothing, and `show --json` does not report the kind.
        try:
            card.call("block", reason, "--kind", "needs_input")
        except Failed:
            pass
        return result("held", "elicitation", reason=reason)
    if status == "review":
        return result("held", "elicitation", reason=reason)
    raise Failed(f"card {card.id} is {status} after Hermes refused the merge")


def complete(card: Card, merge: Merge) -> dict:
    metadata = json.dumps({"published_pr": merge.url})
    try:
        card.call("complete", "--summary", f"Pull request {merge.ref} merged.", "--metadata", metadata)
    except Failed as failure:
        return refused_merge(card, merge, str(failure))
    return result("completed", "response")


def promote_then_complete(card: Card, merge: Merge) -> dict:
    try:
        card.call("promote", "pull", "request", "merged")
    except Failed as failure:
        return commented(card, f"Pull request {merge.ref} merged, but Hermes refused to promote the card: {failure}")
    return complete(card, merge)


def unpark_then_complete(card: Card, merge: Merge) -> dict:
    try:
        leave_triage(card)
    except SpecifyFailed as failure:
        return commented(card, f"Pull request {merge.ref} merged, but the card could not leave triage: {failure}")
    if card.status() == "todo":
        return promote_then_complete(card, merge)
    return complete(card, merge)


MERGED_BY_STATUS: dict[str, Callable[[Card, Merge], dict]] = {
    "review": complete,
    "blocked": complete,
    "ready": complete,
    "todo": promote_then_complete,
    "triage": unpark_then_complete,
}


def merged(card: Card, url: str, linear_state: str) -> dict:
    match = PULL_URL.fullmatch(url)
    if not match:
        raise Failed(f"{url} is not a GitHub pull request URL")
    status = card.status()
    if status not in MERGED_BY_STATUS:
        # A running card's worker completes it, and a done or archived card needs nothing.
        return result("none", status=status)
    return MERGED_BY_STATUS[status](card, Merge(url, pull_ref(url), linear_state))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root.add_argument("--board", help="the card's board, when it is not the current one")
    commands = root.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.add_argument("task_id")
        return sub

    command("resume")
    command("triage-reply")
    command("stop")
    sub = command("unblock")
    sub.add_argument("--repo", required=True)
    sub.add_argument("--branch", required=True)
    sub.add_argument("--reason", required=True)
    command("reopen").add_argument("--reason", required=True)
    sub = command("merged")
    sub.add_argument("--pr", required=True, help="the merged pull request's URL")
    sub.add_argument("--linear-state", required=True, help="the Linear issue's stateType, or unreadable")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    card = Card(args.board, args.task_id)
    commands = {
        "resume": lambda: resume(card),
        "triage-reply": lambda: triage_reply(card),
        "unblock": lambda: unblock(card, args.repo, args.branch, args.reason),
        "reopen": lambda: reopen(card, args.reason),
        "stop": lambda: stop(card),
        "merged": lambda: merged(card, args.pr, args.linear_state),
    }
    try:
        output = commands[args.command]()
    except (Failed, KeyError, ValueError) as failure:
        print(json.dumps({"error": pull_ref(str(failure))}))
        return 1
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
