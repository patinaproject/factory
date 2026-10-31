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
run and from every resume, and turns it into the card's one board change for that result. The worker's steps
differ by run, so no step of its prompt is what makes that change happen: the tool result is.

- On the `triage` board it parks the card in `scheduled`, with the result and a last `comments_seen` line from
  the report as the reason, and adds `triage_round` to the result. Hermes counts a second block of the same
  kind after an unblock as a loop and parks the card in its own `triage` status, so a triage round never
  blocks the card. `card_transition.py` releases it when a reply lands past that count.
- On every other board it blocks the card with kind `needs_input` and that result as the reason, and adds
  `needs_input_block`.

A report with `"precondition": "gh_login"` is a refusal because the worker's gh login is missing or stale. On
every board the hook parks the card in `scheduled` with a reason that starts with `result_line.GH_LOGIN_PARK`,
which never starts with `Needs input:`, so no reply releases it and no triage round posts it. The hook blocks
nothing, makes no Linear activity call, and writes no `NOT_POSTED` comment. `card_transition.py` releases the
card once the login has at least 15 minutes left. A report with `"precondition": "install_skew"` is a refusal
because the install differs from the commit auto-deploy recorded as deployed. The hook parks it the same way,
with a reason that starts with `result_line.INSTALL_SKEW_PARK`, and `card_transition.py` releases the card
once the install matches that commit. Hermes refuses to schedule a `goal_mode` card, and that
card keeps the transient block the worker's prompt gives a `"transient": true` refusal.

After any successful park on the `triage` board whose reason starts with `Needs input:`, its own or one the
worker made with `kanban_schedule`, the hook posts the round once per task and run through
`linear-agent-session`'s `linear_triage_round` tool, which no model sees. It posts after the park, so nobody is
told a card waits when it does not. A post that `failed` or whose outcome is `unknown` is commented on the card,
starting `NOT_POSTED`, and never posted again by the model.

`result_line.py` defines the report line, which `claude-session` writes and this hook reads.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from . import result_line

# Hermes renames the `process` tool to `process_manage` before it runs hooks (`_LEGACY_TOOL_ALIASES` in
# `model_tools.py`), so a hook sees `process_manage`. Older Hermes passes `process`.
# `check-install` reads both lists and reports when Hermes registers none of PROCESS_TOOLS or no REQUIRED_TOOLS.
PROCESS_TOOLS = ("process_manage", "process")
SCHEDULE_TOOL = "kanban_schedule"
REQUIRED_TOOLS = ("terminal", SCHEDULE_TOOL)
WATCHED_TOOLS = (*PROCESS_TOOLS, "terminal")
TRIAGE_BOARD = "triage"
ROUND_TOOL = "linear_triage_round"
POSTED, FAILED, UNKNOWN = "posted", "failed", "unknown"
# `check-install` reports a waiting triage card with a comment that starts with this after its newest park.
NOT_POSTED = "Triage round not posted:"
CYCLE_REFUSAL = "would create a cycle"
GOAL_MODE_REFUSAL = "goal_mode tasks cannot be scheduled"
# Hermes' turn-end guard looks for a kanban_block call in the transcript, not on the board. Without this,
# it would push the worker to block a card that this hook already moved to todo.
STOP_NUDGE = "HERMES_KANBAN_STOP_NUDGE"
# A worker can read the same process output twice, with `wait` and then `log`.
_handled: dict[tuple, str] = {}
# Each run posts its round once, whether the hook or the worker parked the card.
_posted: dict[tuple, str] = {}

Dispatch = Callable[..., str]


def tool_output(result: Any) -> Optional[dict]:
    """The tool result's JSON object, when it has a string `output`."""
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("output"), str) else None


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


# The result key and the worker's instruction for each precondition's park.
PRECONDITION_PARKED = {
    result_line.GH_LOGIN: (
        "gh_login_park",
        "The worker's gh login is missing or stale, so this card waits in scheduled and goes back to the queue "
        "once the login is fresh.",
    ),
    result_line.INSTALL_SKEW: (
        "install_skew_park",
        "The factory's install differs from the deployed commit, so this card waits in scheduled for a factory "
        "deploy and goes back to the queue once the install matches.",
    ),
}


def park_for_precondition(precondition: str, error: str, dispatch: Dispatch) -> Optional[dict]:
    """None when Hermes refuses to schedule a `goal_mode` card, which the worker blocks as it always did."""
    reason = f"{result_line.PRECONDITION_PARKS[precondition]} {error}"
    answer = _call(dispatch, SCHEDULE_TOOL, {"reason": reason})
    if not answer.get("ok"):
        if GOAL_MODE_REFUSAL in str(answer.get("error")):
            return None
        return {"parked": False, "instruction": (
            f"The blocker-order hook could not park this card: {answer.get('error')}. Call kanban_schedule with "
            f"exactly this reason, then stop: {json.dumps(reason)}. Do not call kanban_block and do not run "
            "claude-session again."
        )}
    os.environ[STOP_NUDGE] = "off"
    return {"parked": True, "instruction": (
        f"{PRECONDITION_PARKED[precondition][1]} Stop now: call no other tool, do not call kanban_block, do not "
        "post an activity, and do not run claude-session again."
    )}


def park_round(task: str, run: Optional[str], reason: str, dispatch: Dispatch) -> dict:
    """Park first, then post, so nobody is told the card waits when it does not."""
    answer = _call(dispatch, SCHEDULE_TOOL, {"reason": reason})
    if not answer.get("ok"):
        return {"parked": False, "post": None, "instruction": (
            f"The blocker-order hook could not park this card: {answer.get('error')}. Call kanban_schedule with "
            f"exactly this reason, then stop: {json.dumps(reason)}. Do not call kanban_block, do not post the "
            "questions yourself, and do not run claude-session again."
        )}
    os.environ[STOP_NUDGE] = "off"
    return parked(post_round(task, run, reason, dispatch))


def parked(post: str) -> dict:
    told = {
        POSTED: "posted its questions on the Linear agent session",
        FAILED: "could not post its questions, which it recorded on the card",
        UNKNOWN: "may not have posted its questions, which it recorded on the card",
    }[post]
    return {"parked": True, "post": post, "instruction": (
        f"Claude needs input, so this card waits in scheduled until a person replies, and the blocker-order hook "
        f"{told}. Stop now: call no other tool, do not post the questions yourself, and do not run claude-session "
        "again."
    )}


def post_round(task: str, run: Optional[str], reason: str, dispatch: Dispatch) -> str:
    key = (task, run)
    if key not in _posted:
        questions, _ = result_line.read_comments_seen(reason)
        answer = _call(dispatch, ROUND_TOOL, {"task_id": task, "board": TRIAGE_BOARD, "reason": questions})
        post = answer.get("outcome") if answer.get("outcome") in (POSTED, FAILED, UNKNOWN) else FAILED
        if post != POSTED:
            error = answer.get("error")
            _call(dispatch, "kanban_comment", {
                "task_id": task, "body": f"{NOT_POSTED} {post}" + (f": {error}" if error else ""),
            })
        _posted[key] = post
    return _posted[key]


def worker_park(task: str, run: Optional[str], args: Any, result: Any, dispatch: Dispatch) -> Optional[str]:
    """The worker's own `kanban_schedule` of a triage round, which the hook told it to make when its park failed."""
    reason = args.get("reason") if isinstance(args, dict) else None
    if not isinstance(reason, str):
        return None
    if reason.startswith(result_line.MACHINE_PARKS):
        if landed(result):
            os.environ[STOP_NUDGE] = "off"
        return None
    if os.environ.get("HERMES_KANBAN_BOARD") != TRIAGE_BOARD:
        return None
    if not reason.lstrip().startswith(result_line.NEEDS_INPUT):
        return None
    answer = landed(result)
    if answer is None:
        return None
    os.environ[STOP_NUDGE] = "off"
    return json.dumps({**answer, "triage_round": parked(post_round(task, run, reason, dispatch))})


def landed(result: Any) -> Optional[dict]:
    """A tool result that answered `ok`."""
    try:
        answer = json.loads(result)
    except (TypeError, ValueError):
        return None
    return answer if isinstance(answer, dict) and answer.get("ok") else None


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
        run = os.environ.get("HERMES_KANBAN_RUN_ID")
        if task and tool_name == SCHEDULE_TOOL:
            return worker_park(task, run, args, result, ctx.dispatch_tool)
        payload = tool_output(result) if tool_name in WATCHED_TOOLS and task else None
        if payload is None:
            return None
        report = result_line.last(whole_output(payload, tool_name, args, ctx.dispatch_tool))
        reason = result_line.needs_input(report)
        if reason is not None:
            key = (task, run, reason)
            if key not in _handled and os.environ.get("HERMES_KANBAN_BOARD") == TRIAGE_BOARD:
                marked = result_line.mark_comments_seen(reason, report.session.comments_seen)
                _handled[key] = json.dumps({**payload, "triage_round": park_round(task, run, marked, ctx.dispatch_tool)})
            elif key not in _handled:
                _handled[key] = json.dumps({**payload, "needs_input_block": block_for_input(reason, ctx.dispatch_tool)})
            return _handled[key]
        refusal = result_line.precondition_refusal(report)
        if refusal is not None:
            key = (task, run, refusal.error)
            if key not in _handled:
                parked = park_for_precondition(refusal.precondition, refusal.error, ctx.dispatch_tool)
                if parked is None:
                    return None
                _handled[key] = json.dumps({**payload, PRECONDITION_PARKED[refusal.precondition][0]: parked})
            return _handled[key]
        cards = result_line.waiting_on(report)
        if cards is None:
            return None
        key = (task, run, tuple(cards))
        if key not in _handled:
            _handled[key] = json.dumps({**payload, "blocker_order": order(cards, task, ctx.dispatch_tool)})
        return _handled[key]

    return on_tool_result


def register(ctx: Any) -> None:
    ctx.register_hook("transform_tool_result", make_hook(ctx))
