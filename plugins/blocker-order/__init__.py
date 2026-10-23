"""Orders a Kanban card behind the cards of the Linear issues that block its issue.

`claude-session` runs fenced from the board. When the issue's blockers have live cards, it starts no
Claude run and prints one line, `{"waiting_on": [task id, ...]}`. This hook reads that line in the
worker's `terminal` tool result and in the `output` of its process tool's `wait` and `log` results. It
reads nothing from `poll`, whose `output_preview` it ignores, and nothing from the `notify: true`
completion message, so a worker that never calls `wait` or `log` shows the hook no result.

The hook runs in the worker process, which holds the card's identity, so it can link each card as this card's parent and then block this card with kind
`dependency`. Hermes holds the card in `todo` until every parent is `done` or `archived`. The hook then
rewrites the result so the worker stops.

Hermes refuses a link that would close a cycle. The hook links nothing for that card. When it linked
no card, the result tells the worker to post one notice and run `claude-session` again with
`--ignore-blockers`, so the card starts as it did before.

The hook also reads each `claude-session` report whose `result` starts with `Needs input:`, from the first
run and from every resume, and blocks the card with kind `needs_input` and that result as the reason. The
worker's steps differ by run, so no step of its prompt is what makes that block happen: the tool result is.

`result_line.py` defines the report line, which `claude-session` writes and this hook reads.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from . import result_line

# Hermes renames the `process` tool to `process_manage` before it runs hooks (`_LEGACY_TOOL_ALIASES` in
# `model_tools.py`), so a hook sees `process_manage`. Older Hermes passes `process`.
# `check-install` reads both lists and alerts when Hermes registers none of PROCESS_TOOLS or no REQUIRED_TOOLS.
PROCESS_TOOLS = ("process_manage", "process")
REQUIRED_TOOLS = ("terminal",)
WATCHED_TOOLS = (*PROCESS_TOOLS, *REQUIRED_TOOLS)
CYCLE_REFUSAL = "would create a cycle"
# Hermes' turn-end guard looks for a kanban_block call in the transcript, not on the board. Without this,
# it would push the worker to block a card that this hook already moved to todo.
STOP_NUDGE = "HERMES_KANBAN_STOP_NUDGE"
# A worker can read the same process output twice, with `wait` and then `log`.
_handled: dict[tuple, str] = {}

Dispatch = Callable[..., str]


def tool_output(result: Any) -> Optional[dict]:
    """The tool result's JSON object, when it has a string `output`."""
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("output"), str) else None


def waiting_on(output: str) -> Optional[list[str]]:
    """The cards in the `waiting_on` line, when it is the last JSON line of the tool's output."""
    report = result_line.last(output)
    return list(report.waiting_on) if isinstance(report, result_line.Waiting) else None


def needs_input(output: str) -> Optional[str]:
    """The `result` of the last JSON line, a `claude-session` report, when it starts with `Needs input:`."""
    return result_line.needs_input(result_line.last(output))


def whole_output(payload: dict, tool_name: str, args: Any, dispatch: Dispatch) -> str:
    """`process wait` keeps only the last 2000 characters of the output and says so in `output_cut`,
    so a long report loses its start. The log has the whole report line, so read that instead."""
    session = args.get("session_id") if isinstance(args, dict) else None
    if not payload.get("output_cut") or tool_name not in PROCESS_TOOLS or not session:
        return payload["output"]
    log = _call(dispatch, tool_name, {"action": "log", "session_id": session})
    return log["output"] if isinstance(log.get("output"), str) else payload["output"]


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
        payload = tool_output(result) if tool_name in WATCHED_TOOLS and task else None
        if payload is None:
            return None
        output = whole_output(payload, tool_name, args, ctx.dispatch_tool)
        run = os.environ.get("HERMES_KANBAN_RUN_ID")
        reason = needs_input(output)
        if reason is not None:
            key = (task, run, reason)
            if key not in _handled:
                _handled[key] = json.dumps({**payload, "needs_input_block": block_for_input(reason, ctx.dispatch_tool)})
            return _handled[key]
        cards = waiting_on(output)
        if cards is None:
            return None
        key = (task, run, tuple(cards))
        if key not in _handled:
            _handled[key] = json.dumps({**payload, "blocker_order": order(cards, task, ctx.dispatch_tool)})
        return _handled[key]

    return on_tool_result


def register(ctx: Any) -> None:
    ctx.register_hook("transform_tool_result", make_hook(ctx))
