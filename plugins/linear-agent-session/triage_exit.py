"""Archive the triage card of an issue that left Triage, or leave it, without a triage agent turn.

This applies step 2 of "Triage inbox" in `templates/triage-inbox.md` before dispatch. It acts only when that
step is the whole turn: the issue is not delegated to the agent app, the update changed only its status, and no
card on the default board could need a decision. A `scheduled` card waits for a person's answer to its questions, so
it stays unless the issue was completed, canceled, or closed as a duplicate. Every other delivery, and every doubt or
error, reaches the triage agent as before.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple, Optional

TRIAGE_BOARD = "triage"
ARCHIVE_TIMEOUT_SECONDS = 120.0
HERMES_TIMEOUT_SECONDS = 60.0
TAKEOVER_SUFFIX = "-takeover"
CLOSED_STATE_TYPES = ("completed", "canceled", "duplicate")
LINEAR_ISSUE_ID = re.compile(r"^linear_issue_id:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Card:
    id: str
    status: str
    title: str
    body: str
    branch_name: Optional[str] = None


class Verdict(NamedTuple):
    """`archive` and `leave` replace the turn. `pass` and `failed` send the delivery to triage."""

    action: str
    reason: str
    task_id: Optional[str] = None

    @property
    def handled(self) -> bool:
        return self.action in ("archive", "leave")


def passed(reason: str) -> Verdict:
    return Verdict("pass", reason)


def payload_passthrough(payload: Any) -> Optional[str]:
    """Why the payload alone cannot be a plain Triage exit, or None when it might be. Reads no board and no Linear."""
    if not isinstance(payload, Mapping) or payload.get("type") != "Issue" or payload.get("action") != "update":
        return "not an Issue status change"
    previous = payload.get("updatedFrom")
    if not (isinstance(previous, Mapping) and "stateId" in previous):
        return "not an Issue status change"
    if {"delegateId", "labelIds"} & set(previous):
        return "the update also changed the delegate or labels"
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return "the payload names no issue"
    state = data.get("state")
    state_type = state.get("type") if isinstance(state, Mapping) else None
    if not isinstance(state_type, str) or state_type == "triage":
        return "the issue did not leave Triage"
    issue_id, identifier = data.get("id"), data.get("identifier")
    if not (isinstance(issue_id, str) and issue_id and isinstance(identifier, str) and identifier):
        return "the payload names no issue"
    return None


def decide(payload: Any, app_user_id: str, triage_cards: list[Card], work_cards: list[Card]) -> Verdict:
    reason = payload_passthrough(payload)
    if reason is not None:
        return passed(reason)
    data = payload["data"]
    if not app_user_id:
        return passed("app_user_id is not set")
    if _delegate(data) == app_user_id:
        return passed("the issue is delegated to the agent app")
    issue_id, identifier = data["id"], data["identifier"]
    if find_card(work_cards, issue_id, identifier, None):
        return passed("the issue has a card on the default board")
    found = [card for card in triage_cards if names_issue(card, issue_id)]
    if len(found) != 1:
        return passed(f"the issue has {len(found)} triage cards")
    card = found[0]
    if card.status == "running":
        return Verdict("leave", f"left running triage card {card.id} to its worker: {identifier} left Triage", card.id)
    if card.status == "scheduled" and data["state"]["type"] not in CLOSED_STATE_TYPES:
        return Verdict("leave", f"left scheduled triage card {card.id} waiting for a reply: {identifier} left Triage", card.id)
    return Verdict("archive", f"archived triage card {card.id}: {identifier} left Triage", card.id)


def handle(
    payload: Any,
    *,
    app_user_id: Callable[[], str],
    cards: Callable[[Optional[str]], list[Card]],
    issue_status: Callable[[str], Mapping[str, Any]],
    archive: Callable[[str], Optional[str]],
) -> Verdict:
    """Decide from the payload and the boards, confirm against the live issue, then archive.

    The payload check runs first, so a delivery that cannot be a plain exit costs no `hermes` call.
    The live read catches a delivery that is out of date, such as a replay after the issue went back to Triage.
    It also gives the issue's branch, which finds a default-board card that Hermes rewrote.
    """
    reason = payload_passthrough(payload)
    if reason is not None:
        return passed(reason)
    app = app_user_id()
    work_cards = cards(None)
    verdict = decide(payload, app, cards(TRIAGE_BOARD), work_cards)
    if not verdict.handled:
        return verdict
    live = issue_status(payload["data"]["id"])
    if live["stateType"] == "triage":
        return passed("the issue is back in Triage")
    if live["delegate"] == app:
        return passed("the issue is now delegated to the agent app")
    branch = live["branch"]
    if not branch:
        return passed("the live issue has no branch")
    if find_card(work_cards, payload["data"]["id"], payload["data"]["identifier"], branch):
        return passed("the issue has a card on its branch on the default board")
    if verdict.action == "archive":
        error = archive(verdict.task_id)
        if error is not None:
            return Verdict("failed", f"archiving triage card {verdict.task_id} failed: {error}", verdict.task_id)
    return verdict


def handler(
    app_user_id: Callable[[], str],
    client: Callable[[], Any],
    cards: Callable[[Optional[str]], list[Card]],
    home: Callable[[], Path],
) -> Callable[[Any], Verdict]:
    """Wire `handle` to a Hermes home: its Kanban boards, the plugin's Linear client, and its `archive_card.py`."""

    def run(payload: Any) -> Verdict:
        return handle(
            payload,
            app_user_id=app_user_id,
            cards=cards,
            issue_status=lambda issue_id: client().issue_status(issue_id),
            archive=lambda task_id: archive_card(home(), task_id),
        )

    return run


def gateway_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def kanban_cards(board: Optional[str]) -> list[Card]:
    """Read a board in the gateway process. A script outside Hermes cannot import it and uses `listed_cards`."""
    from hermes_cli import kanban_db, kanban_db_connect

    with kanban_db_connect.connect_closing(board=board) as conn:
        tasks = kanban_db.list_tasks(conn)
    return [Card(task.id, task.status, task.title or "", task.body or "", task.branch_name) for task in tasks]


def listed_cards(board: Optional[str]) -> list[Card]:
    pinned = ["--board", board] if board else []
    tasks = json.loads(_hermes("kanban", *pinned, "list", "--json"))
    return [
        Card(task["id"], task["status"], task.get("title") or "", task.get("body") or "", task.get("branch_name"))
        for task in tasks
    ]


def card_body(task_id: str) -> str:
    return json.loads(_hermes("kanban", "show", task_id, "--json"))["task"]["body"] or ""


def configured_app_user_id() -> str:
    value = json.loads(_hermes("config", "get", "plugins.entries.linear-agent-session.settings.app_user_id", "--json"))
    return value if isinstance(value, str) else ""


def _hermes(*args: str) -> str:
    hermes = os.environ.get("HERMES_BIN") or shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")
    proc = subprocess.run([hermes, *args], capture_output=True, text=True, timeout=HERMES_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise RuntimeError(f"hermes {' '.join(args[:3])} exited {proc.returncode}")
    return proc.stdout


def archive_card(home: Path, task_id: str) -> Optional[str]:
    """Run the archive step the triage prompt runs. Return None on success, or the reason it failed."""
    script = home / "profiles" / "claude-worker" / "scripts" / "archive_card.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--board", TRIAGE_BOARD, task_id],
            capture_output=True,
            text=True,
            timeout=ARCHIVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"archive_card.py did not finish in {ARCHIVE_TIMEOUT_SECONDS:.0f} s"
    except OSError as failure:
        return f"archive_card.py could not run: {failure.strerror or failure}"
    lines = proc.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        return f"archive_card.py exited {proc.returncode} without a JSON line"
    if proc.returncode == 0 and isinstance(result, dict) and result.get("archived") == task_id:
        return None
    return str(result.get("error") if isinstance(result, dict) else result)


def _delegate(data: Mapping[str, Any]) -> Optional[str]:
    delegate = data.get("delegate")
    return data.get("delegateId") or (delegate.get("id") if isinstance(delegate, Mapping) else None)


def names_issue(card: Card, issue_id: str) -> bool:
    return issue_id in LINEAR_ISSUE_ID.findall(card.body)


def find_card(
    cards: list[Card], issue_id: str, identifier: str, branch: Optional[str]
) -> Optional[tuple[Card, str]]:
    """The issue's implementation card and the key that found it, or None. Archived cards are never found.

    The branch survives when Hermes rewrites a card's title and body, so it comes first. A card on the factory's own
    pull request branch has no branch key, so the body line and the title find it. `branch` None skips the branch keys.
    """
    live = [card for card in cards if card.status != "archived"]
    keys = (
        ("branch", lambda card: bool(branch) and card.branch_name == branch),
        ("takeover_branch", lambda card: bool(branch) and card.branch_name == branch + TAKEOVER_SUFFIX),
        ("linear_issue_id", lambda card: names_issue(card, issue_id)),
        ("title", lambda card: card.title.startswith(f"{identifier}:")),
    )
    for key, matches in keys:
        for card in live:
            if matches(card):
                return card, key
    return None
