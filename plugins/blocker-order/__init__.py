"""Orders a Kanban card behind the cards of the Linear issues that block its issue.

`claude-session` runs fenced from the board. When the issue's blockers have live cards, it starts no
Claude run and prints one line, `{"waiting_on": [task id, ...]}`. This hook reads that line in the
worker's `process` or `terminal` tool result. It runs in the worker process, which holds the card's
identity, so it can link each card as this card's parent and then block this card with kind
`dependency`. Hermes holds the card in `todo` until every parent is `done` or `archived`. The hook then
rewrites the result so the worker stops.

Hermes refuses a link that would close a cycle. The hook links nothing for that card. When it linked
no card, the result tells the worker to post one notice and run `claude-session` again with
`--ignore-blockers`, so the card starts as it did before.

The hook also reads each `claude-session` report whose `result` starts with `Needs input:`, from the first
run and from every resume, and blocks the card with kind `needs_input` and that result as the reason. The
worker's steps differ by run, so no step of its prompt is what makes that block happen: the tool result is.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

WATCHED_TOOLS = ("process", "terminal")
CYCLE_REFUSAL = "would create a cycle"
# Hermes' turn-end guard looks for a kanban_block call in the transcript, not on the board. Without this,
# it would push the worker to block a card that this hook already moved to todo.
STOP_NUDGE = "HERMES_KANBAN_STOP_NUDGE"
NEEDS_INPUT = "Needs input:"
# A worker can read the same process output twice, with `wait` and then `log`.
_handled: dict[tuple, str] = {}

Dispatch = Callable[..., str]


def waiting_on(result: Any) -> Optional[list[str]]:
    """The cards in the `waiting_on` line, when it is the last JSON line of the tool's output."""
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, str):
        return None
    for line in reversed(output.splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        cards = report.get("waiting_on") if isinstance(report, dict) and set(report) == {"waiting_on"} else None
        if isinstance(cards, list) and cards and all(isinstance(card, str) and card for card in cards):
            return cards
        return None
    return None


def needs_input(result: Any) -> Optional[str]:
    """The `result` of the last JSON line, a `claude-session` report, when it starts with `Needs input:`."""
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, str):
        return None
    for line in reversed(output.splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        text = report.get("result") if isinstance(report, dict) and "session_id" in report else None
        return text if isinstance(text, str) and text.startswith(NEEDS_INPUT) else None
    return None


def block_for_input(reason: str, dispatch: Dispatch) -> dict:
    answer = _call(dispatch, "kanban_block", {"reason": reason, "kind": "needs_input"})
    if not answer.get("ok"):
        return {"blocked": False, "instruction": (
            f"The blocker-order hook could not block this card: {answer.get('error')}. Call kanban_block "
            "with kind needs_input and the whole result as the reason, then stop. Do not run claude-session again."
        )}
    os.environ[STOP_NUDGE] = "off"
    return {"blocked": True, "instruction": (
        "Claude needs input, so the blocker-order hook blocked this card with kind needs_input and the whole "
        "result as the reason. Stop now: call no other tool and do not run claude-session again."
    )}


def _call(dispatch: Dispatch, tool: str, args: dict) -> dict:
    try:
        answer = json.loads(dispatch(tool, args))
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}
    return answer if isinstance(answer, dict) else {"error": f"{tool} answered {answer!r}"}


def order(cards: list[str], task: str, dispatch: Dispatch) -> dict:
    linked, not_linked = [], {}
    for card in cards:
        answer = _call(dispatch, "kanban_link", {"parent_id": card, "child_id": task})
        if answer.get("ok"):
            linked.append(card)
        else:
            not_linked[card] = str(answer.get("error"))
    if not linked:
        return {"linked": [], "not_linked": not_linked, "blocked": False, "ignore": list(not_linked),
                "instruction": _start_instruction(not_linked)}
    names = ", ".join(linked)
    reason = f"waits for its blocker cards {names}"
    answer = _call(dispatch, "kanban_block", {"reason": reason, "kind": "dependency"})
    outcome = {"linked": linked, "not_linked": not_linked, "blocked": bool(answer.get("ok")), "ignore": []}
    if not answer.get("ok"):
        return {**outcome, "instruction": (
            f"The blocker-order hook linked this card to its blocker cards {names} but could not block it: "
            f"{answer.get('error')}. Call kanban_block with kind dependency and the reason `{reason}`, then stop."
        )}
    os.environ[STOP_NUDGE] = "off"
    if answer.get("block_kind") != "dependency":
        return {**outcome, "instruction": (
            f"Hermes recorded this card's block as {answer.get('block_kind')}, not dependency, because its "
            f"blocker cards {names} finished meanwhile. Post one error activity on the Linear agent session "
            "saying a person must unblock the card, then stop."
        )}
    return {**outcome, "instruction": (
        f"This card now waits in todo for its blocker cards {names}: the blocker-order hook linked them as its "
        "parents and blocked it with kind dependency. Hermes starts it again once they are done. Stop now: "
        "call no other tool and do not run claude-session again."
    )}


def _start_instruction(not_linked: dict) -> str:
    cycle = [card for card, error in not_linked.items() if CYCLE_REFUSAL in error]
    reasons = [f"linking {', '.join(cycle)} would close a cycle of blockers"] if cycle else []
    reasons += [f"linking {card} failed: {error}" for card, error in not_linked.items() if card not in cycle]
    cards = ", ".join(not_linked)
    return (
        f"This card does not wait for blocker cards {cards}, because Hermes refused to link them: "
        f"{'; '.join(reasons)}. Post one thought on the Linear agent session that names the issue of each of "
        "those cards (kanban_show gives a card's title, which starts with its issue identifier) and says why "
        "this card starts without waiting for them. Then run claude-session again as before, adding "
        f"`--ignore-blockers {','.join(not_linked)}` to it and to every later run of this card."
    )


def make_hook(ctx: Any):
    def on_tool_result(tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> Optional[str]:
        task = os.environ.get("HERMES_KANBAN_TASK")
        if tool_name not in WATCHED_TOOLS or not task:
            return None
        reason = needs_input(result)
        if reason is not None:
            key = (task, os.environ.get("HERMES_KANBAN_RUN_ID"), reason)
            if key not in _handled:
                rewritten = json.loads(result)
                rewritten["needs_input_block"] = block_for_input(reason, ctx.dispatch_tool)
                _handled[key] = json.dumps(rewritten)
            return _handled[key]
        cards = waiting_on(result)
        if cards is None:
            return None
        key = (task, os.environ.get("HERMES_KANBAN_RUN_ID"), tuple(cards))
        if key not in _handled:
            rewritten = json.loads(result)
            rewritten["blocker_order"] = order(cards, task, ctx.dispatch_tool)
            _handled[key] = json.dumps(rewritten)
        return _handled[key]

    return on_tool_result


def register(ctx: Any) -> None:
    ctx.register_hook("transform_tool_result", make_hook(ctx))
