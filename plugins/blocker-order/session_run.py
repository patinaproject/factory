"""The `claude_session_run` tool: run one `claude-session` to its end in a single tool call.

The worker's fixed steps for a run were a background `terminal` start, a loop of `process_manage` waits, and a
`log` re-read. All of that is mechanical, so the tool does it in code. It starts `claude-session` the way
Hermes starts a background terminal command (the login shell, the sanitized environment, its own session),
sends the output to a file, and returns when the process exits. Nothing in it calls a `kanban_*` tool: the
`blocker-order` hook reads this tool's result like a `process_manage` result and makes every board change.

Hermes ends a tool call at `timeouts.tools.sequential_call`, which the worker profile sets above
`LIMIT_SECONDS + GRACE_SECONDS`, so this tool's own limit is the one that fires first.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

TOOL = "claude_session_run"
TOOLSET = "claude_session"
# The longest `claude-session` run measured in 14 days was 22065 s.
LIMIT_SECONDS = 28800
# `claude-session` waits up to 25 s for Claude after SIGTERM before it prints its stop line.
GRACE_SECONDS = 30
TAIL_CHARS = 2000
TICK_SECONDS = 1.0
GH_SECONDS = 60
PULL_REQUEST_FIELDS = "url,state,isDraft,mergeStateStatus,headRefOid"
NO_PULL_REQUEST = "no pull requests found"
TRIAGE_BOARD = "triage"


class Outcome(Enum):
    EXITED = "exited"
    TIMEOUT = "timeout"
    INTERRUPT = "interrupt"


@dataclass(frozen=True)
class Host:
    """What the tool needs from Hermes. `system_host` fills it from Hermes; a test fills it with plain values."""

    wrap: Callable[[list[str]], list[str]]
    env: dict[str, str]
    interrupted: Callable[[], bool]
    limit: float = LIMIT_SECONDS
    grace: float = GRACE_SECONDS
    tick: float = TICK_SECONDS


@dataclass(frozen=True)
class Run:
    outcome: Outcome
    exit_code: int
    output: str


PARAMETERS = {
    "type": "object",
    "properties": {
        "issue_id": {"type": "string", "description": "The card's `linear_issue_id`."},
        "issue_identifier": {"type": "string", "description": "The card's `linear_issue_identifier`."},
        "issue_url": {"type": "string", "description": "The card's `linear_issue_url`."},
        "repo": {"type": "string", "description": "The card's `repo`, as <owner>/<name>."},
        "takeover_pr": {"type": "string", "description": "The card's `takeover_pr`, when it has one."},
        "message_file": {"type": "string", "description": "A message file to resume Claude with, when there is one."},
        "ignore_blockers": {
            "type": "string",
            "description": "Comma-separated blocker card ids, when the blocker-order hook said to ignore them.",
        },
    },
    "required": ["issue_id", "issue_identifier", "issue_url", "repo"],
}

SCHEMA = {
    "name": TOOL,
    "description": (
        "Run `claude-session` for this card to its end and return its output. One call covers the whole run, so "
        "never start `claude-session` any other way and never wait on it. The result holds `output` and "
        "`exit_code`: `output` ends with claude-session's JSON line, and the text before it is cut to its last "
        f"{TAIL_CHARS} characters. A run that reaches the tool's {LIMIT_SECONDS} second limit, or that Hermes "
        "interrupts, is stopped and returns `stopped` as `timeout` or `interrupt`. After a run with a pull request "
        "the result also holds `pull_request`: the output of `gh pr view` for the card's worktree, or null when "
        "there is none."
    ),
    "parameters": PARAMETERS,
}


def system_host(environ: dict[str, str]) -> Host:
    """Hermes' own imports stay here, so the rest of this file runs without Hermes."""
    from tools.environments.local import _find_shell, _sanitize_subprocess_env
    from tools.interrupt import is_interrupted

    env = _sanitize_subprocess_env(environ)
    env["PYTHONUNBUFFERED"] = "1"
    shell = _find_shell()
    return Host(wrap=lambda argv: [shell, "-lic", f"set +m; exec {shlex.join(argv)}"], env=env, interrupted=is_interrupted)


def argv_for(args: dict, environ: dict[str, str]) -> list[str]:
    argv = [
        str(Path(environ["HERMES_HOME"]) / "scripts" / "claude-session"),
        "--task-id", environ["HERMES_KANBAN_TASK"],
        "--issue-id", args["issue_id"],
        "--issue-identifier", args["issue_identifier"],
        "--issue-url", args["issue_url"],
        "--repo", args["repo"],
    ]
    for key, flag in (("takeover_pr", "--takeover-pr"), ("message_file", "--message-file"), ("ignore_blockers", "--ignore-blockers")):
        if args.get(key):
            argv += [flag, args[key]]
    return argv


def run_session(argv: list[str], cwd: str, host: Host) -> Run:
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(
            host.wrap(argv), cwd=cwd, env=host.env, stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            outcome = wait(proc, host)
        finally:
            if proc.poll() is None:
                stop(proc, host.grace)
        out.seek(0)
        return Run(outcome, proc.returncode, out.read().decode("utf-8", errors="replace"))


def wait(proc: subprocess.Popen, host: Host) -> Outcome:
    deadline = time.monotonic() + host.limit
    while True:
        try:
            proc.wait(timeout=host.tick)
            return Outcome.EXITED
        except subprocess.TimeoutExpired:
            pass
        if host.interrupted():
            return Outcome.INTERRUPT
        if time.monotonic() >= deadline:
            return Outcome.TIMEOUT


def stop(proc: subprocess.Popen, grace: float) -> None:
    """SIGTERM the process group, which lets `claude-session` end Claude and print its stop line, then SIGKILL
    whatever is left of the group."""
    kill_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    kill_group(proc.pid, signal.SIGKILL)
    proc.wait()


def kill_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def split_output(output: str) -> tuple[str, Optional[str]]:
    """The text before the last JSON line, and that line. `result_line.last` finds the same line."""
    lines = output.splitlines(keepends=True)
    for index in range(len(lines) - 1, -1, -1):
        try:
            json.loads(lines[index])
        except ValueError:
            continue
        return "".join(lines[:index]), lines[index]
    return output, None


def bounded(output: str) -> tuple[str, Optional[dict]]:
    before, final = split_output(output)
    tail = before[-TAIL_CHARS:]
    if final is None:
        return tail, None
    report = json.loads(final)
    return tail + final.rstrip("\n"), report if isinstance(report, dict) else None


def pull_request(worktree: str, host: Host) -> Optional[dict]:
    argv = host.wrap(["gh", "pr", "view", "--json", PULL_REQUEST_FIELDS])
    try:
        done = subprocess.run(
            argv, cwd=worktree, env=host.env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=GH_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"gh pr view timed out after {GH_SECONDS} s"}
    if done.returncode == 0:
        return json.loads(done.stdout)
    if NO_PULL_REQUEST in done.stderr.lower():
        return None
    return {"error": done.stderr.strip() or f"gh pr view exited {done.returncode}"}


def is_success(report: Optional[dict]) -> bool:
    return report is not None and "branch" in report and "worktree" in report and "error" not in report


def run_tool(args: dict, environ: dict[str, str], host: Host) -> str:
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE", "HERMES_HOME"):
        if not environ.get(name):
            return json.dumps({"error": f"{name} is not set, so {TOOL} cannot run claude-session for this card"})
    finished = run_session(argv_for(args, environ), environ["HERMES_KANBAN_WORKSPACE"], host)
    output, report = bounded(finished.output)
    result: dict[str, Any] = {"output": output, "exit_code": finished.exit_code}
    if finished.outcome is Outcome.TIMEOUT:
        result["stopped"] = "timeout"
        result["error"] = f"{TOOL} stopped claude-session at its limit of {host.limit:g} seconds"
    elif finished.outcome is Outcome.INTERRUPT:
        result["stopped"] = "interrupt"
    elif is_success(report) and environ.get("HERMES_KANBAN_BOARD") != TRIAGE_BOARD:
        result["pull_request"] = pull_request(report["worktree"], host)
    return json.dumps(result)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name=TOOL,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=lambda args, **_: run_tool(args, dict(os.environ), system_host(dict(os.environ))),
        description=SCHEMA["description"],
    )
