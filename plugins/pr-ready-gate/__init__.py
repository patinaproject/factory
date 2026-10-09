from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Optional

DEFAULT_GATED_PROFILES = ["claude-worker"]
PR_FIELDS = "url,isDraft,mergeStateStatus,headRefOid,state"
PR_URL = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/pull/\d+/?$")
# Hermes kills a pre_tool_call hook after 30s; up to three calls must fit inside that.
GIT_TIMEOUT_SECONDS = 3
GH_TIMEOUT_SECONDS = 20

MERGE_STATE_REMEDIES = {
    "BEHIND": "update the branch from the base branch and push",
    "BLOCKED": "required checks or reviews are not satisfied; wait for checks to pass and address reviews",
    "DIRTY": "resolve the merge conflicts with the base branch and push",
    "UNSTABLE": "a check is pending or failing; wait for checks to finish and fix any failures",
    "UNKNOWN": "GitHub has not computed mergeability yet; wait a minute and retry",
    "HAS_HOOKS": "the base branch has pre-receive hooks; this gate only accepts CLEAN",
    "DRAFT": "mark the pull request ready with `gh pr ready`",
}


class GateError(Exception):
    pass


def evaluate(pr: dict, local_head: str) -> list[str]:
    failures = []
    state = pr.get("state")
    if state != "OPEN":
        failures.append(f"state is {state!r}, expected 'OPEN': reopen the pull request or open a new one")
    if pr.get("isDraft") is not False:
        failures.append(f"isDraft is {pr.get('isDraft')!r}, expected False: mark it ready with `gh pr ready`")
    merge_state = pr.get("mergeStateStatus")
    if merge_state != "CLEAN":
        remedy = MERGE_STATE_REMEDIES.get(merge_state, "wait until GitHub reports CLEAN")
        failures.append(f"mergeStateStatus is {merge_state!r}, expected 'CLEAN': {remedy}")
    remote_head = pr.get("headRefOid")
    if remote_head != local_head:
        failures.append(
            f"headRefOid is {remote_head!r} but the worktree HEAD is {local_head!r}: "
            "push the worktree's commits, or pull if the remote branch is ahead"
        )
    return failures


def _run(cmd: list[str], cwd: str, timeout: int) -> str:
    shown = " ".join(cmd)
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GateError(f"`{shown}` timed out after {timeout}s")
    except OSError as exc:
        raise GateError(f"`{shown}` could not run: {exc}")
    if proc.returncode != 0:
        raise GateError(f"`{shown}` exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _published_pr(args: Any) -> Optional[str]:
    metadata = args.get("metadata") if isinstance(args, dict) else None
    url = metadata.get("published_pr") if isinstance(metadata, dict) else None
    if isinstance(url, str) and PR_URL.match(url.strip()):
        return url.strip()
    return None


def check(args: Any, env: dict) -> tuple[str, list[str]]:
    workspace = env.get("HERMES_KANBAN_WORKSPACE")
    if not workspace:
        raise GateError("HERMES_KANBAN_WORKSPACE is not set, so the worktree is unknown")
    local_head = _run(["git", "-C", workspace, "rev-parse", "HEAD"], workspace, GIT_TIMEOUT_SECONDS)
    pr_url = _published_pr(args)
    if pr_url:
        target = pr_url
        repo_args = ["--repo", PR_URL.match(pr_url).group(1)]
    else:
        target = env.get("HERMES_KANBAN_BRANCH") or _run(
            ["git", "-C", workspace, "rev-parse", "--abbrev-ref", "HEAD"], workspace, GIT_TIMEOUT_SECONDS
        )
        repo_args = []
    raw = _run(["gh", "pr", "view", target, "--json", PR_FIELDS, *repo_args], workspace, GH_TIMEOUT_SECONDS)
    try:
        pr = json.loads(raw)
    except ValueError:
        raise GateError(f"`gh pr view {target}` returned malformed JSON: {raw[:200]!r}")
    if not isinstance(pr, dict):
        raise GateError(f"`gh pr view {target}` returned {type(pr).__name__}, expected an object")
    return pr.get("url") or target, evaluate(pr, local_head)


def make_hook(ctx: Any):
    def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
        if tool_name != "kanban_complete" or not os.environ.get("HERMES_KANBAN_TASK"):
            return None
        profile = ctx.profile_name or os.environ.get("HERMES_PROFILE", "")
        if profile not in ctx.get_config("gated_profiles", default=DEFAULT_GATED_PROFILES):
            return None
        try:
            pr, failures = check(args, dict(os.environ))
        except GateError as exc:
            return {
                "action": "block",
                "message": (
                    f"pr-ready-gate blocked kanban_complete because the pull request could not be verified: {exc}. "
                    "If no pull request exists for this branch, push and open a ready (non-draft) one; "
                    "otherwise fix the cause and call kanban_complete again."
                ),
            }
        if not failures:
            return None
        lines = "\n".join(f"- {failure}" for failure in failures)
        return {
            "action": "block",
            "message": (
                f"pr-ready-gate blocked kanban_complete: {pr} is not ready to merge.\n{lines}\n"
                "Call kanban_complete again once every condition holds."
            ),
        }

    return on_pre_tool_call


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", make_hook(ctx))
