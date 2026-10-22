"""The cards a card waits for before its first Claude run: the live cards of the Linear issues that block its issue.

`claude-session` asks before Claude starts and prints `{"waiting_on": [task id, ...]}` when the list is
not empty. It cannot change the board, so the `blocker-order` plugin in the worker process links each
card as this card's parent and blocks this card with kind `dependency`. Hermes then holds the card in
`todo` until every parent is `done` or `archived`, so a chain of delegated issues runs in order.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable

FINISHED_ISSUE_STATES = ("completed", "canceled")
FINISHED_CARD_STATUSES = ("done", "archived")
# A blocker delegated together with this card can still be in triage, with no card yet. The wait runs
# inside this card's claim, which Hermes leases for 15 minutes, so it stays well under that.
WAIT_SECONDS = 300
POLL_SECONDS = 45


def card_for(cards: list[dict], issue: dict) -> dict | None:
    """The issue's live card, found by the keys `templates/triage-linear.md` lists, in that order."""
    keys = (
        lambda card: card["branch_name"] == issue["gitBranchName"],
        lambda card: card["branch_name"] == f"{issue['gitBranchName']}-takeover",
        lambda card: f"linear_issue_id: {issue['id']}" in (card["body"] or "").splitlines(),
        lambda card: (card["title"] or "").startswith(f"{issue['identifier']}:"),
    )
    for key in keys:
        for card in cards:
            if key(card):
                return card
    return None


def blocker_cards(issues: list[dict], cards: list[dict], ignored: frozenset[str]) -> tuple[list[str], bool]:
    """The live cards of unfinished blocking issues, and whether one delegated to this app has no card yet."""
    waiting, expected = [], False
    for issue in issues:
        card = card_for(cards, issue)
        if card is None:
            expected = expected or issue["delegatedToSelf"]
        elif card["status"] not in FINISHED_CARD_STATUSES and card["id"] not in ignored:
            waiting.append(card["id"])
    return waiting, expected


def waiting_on(
    read_blockers: Callable[[], list[dict]],
    read_cards: Callable[[], list[dict]],
    ignored: Iterable[str] = (),
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Re-reads Linear and the board while a delegated blocker has no card, until one has or WAIT_SECONDS pass.

    The board is read only for an issue that has an unfinished blocker."""
    ignored = frozenset(ignored)
    deadline = clock() + WAIT_SECONDS
    while True:
        issues = [issue for issue in read_blockers() if issue["stateType"] not in FINISHED_ISSUE_STATES]
        if not issues:
            return []
        waiting, expected = blocker_cards(issues, read_cards(), ignored)
        left = deadline - clock()
        if waiting or not expected or left <= 0:
            return waiting
        sleep(min(POLL_SECONDS, left))
