from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.parse
from typing import Any, Callable, Optional

DEFAULT_GATED_PROFILES = ["claude-worker"]
# Hermes runs its completion contract inside complete_task only, so a review handoff needs this gate too.
GATED_TOOLS = ("kanban_complete", "kanban_request_review")
PR_FIELDS = "url,isDraft,mergeStateStatus,headRefOid,state,reviewDecision,statusCheckRollup,baseRefName"
PASSING_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
# gh reports a run that has not started with Go's zero time, so it sorts as the newest run of its check.
UNSTARTED = "0001-01-01T00:00:00Z"
PR_URL = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/pull/\d+/?$")
# Hermes kills a pre_tool_call hook after 30s (plugins.hook_callback_timeout), fails closed, and skips the hook for 60s.
# One clock starts when the check starts, and every subprocess gets the smaller of its cap and the time left.
HOOK_BUDGET_SECONDS = 25
GIT_TIMEOUT_SECONDS = 3
GH_TIMEOUT_SECONDS = 15
GH_EXTRA_TIMEOUT_SECONDS = 6
MIN_CALL_SECONDS = 1
# Rules whose only effect on a pull request is a merge-time or review-time requirement the gate checks or does not need.
REVIEW_HANDOFF_RULES = frozenset(
    {
        "pull_request",
        "required_status_checks",
        "required_signatures",
        "required_linear_history",
        "deletion",
        "non_fast_forward",
        "creation",
        "update",
    }
)
THREAD_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved}} "
    "commits(first:100){pageInfo{hasNextPage} nodes{commit{oid signature{isValid}}}}}}}"
)

MERGE_STATE_REMEDIES = {
    "BEHIND": "update the branch from the base branch and push",
    "BLOCKED": (
        "a branch rule is not satisfied: required checks, reviews, or verified commit signatures; "
        "wait for checks, address reviews, and make sure every commit is signed. "
        "kanban_request_review passes a pull request whose only unmet rule is the approving review"
    ),
    "DIRTY": "merge the base branch, resolve the conflicts, and publish the merge with push-signed",
    "UNSTABLE": "a check is pending or failing; wait for checks to finish and fix any failures",
    "UNKNOWN": "GitHub has not computed mergeability yet; wait a minute and retry",
    "HAS_HOOKS": "the base branch has pre-receive hooks; this gate only accepts CLEAN",
    "DRAFT": "mark the pull request ready with `gh pr ready`",
}


MERGED_REVIEW = "state is 'MERGED', so there is nothing left to review: call kanban_complete instead"


class GateError(Exception):
    pass


def evaluate(
    pr: dict,
    local_head: str,
    tool_name: str,
    other_blockers: Optional[Callable[[], list[str]]] = None,
) -> list[str]:
    """other_blockers lists every unmet branch rule except the approval, for a pull request that waits on a review."""
    failures = []
    state = pr.get("state")
    if state == "MERGED" and tool_name == "kanban_request_review":
        return [MERGED_REVIEW]
    if state == "MERGED":
        # A merged pull request is past ready-to-merge; only its head still has to be the worker's commit.
        return _head_failures(pr, local_head)
    if state != "OPEN":
        failures.append(f"state is {state!r}, expected 'OPEN' or 'MERGED': reopen the pull request or open a new one")
    if pr.get("isDraft") is not False:
        failures.append(f"isDraft is {pr.get('isDraft')!r}, expected False: {MERGE_STATE_REMEDIES['DRAFT']}")
    merge_state = pr.get("mergeStateStatus")
    hidden: list[str] = []
    if tool_name == "kanban_request_review" and _awaiting_review(pr) and other_blockers is not None:
        # Hold for a review keeps GitHub at BLOCKED until a reviewer acts, which needs this handoff.
        # BLOCKED also covers other unmet rules, so the handoff passes only when the approval is the sole one.
        hidden = other_blockers()
        if not hidden:
            merge_state = "CLEAN"
    if merge_state != "CLEAN":
        remedy = MERGE_STATE_REMEDIES.get(merge_state, "wait until GitHub reports CLEAN")
        failures.append(f"mergeStateStatus is {merge_state!r}, expected 'CLEAN': {remedy}")
    failures += hidden + _head_failures(pr, local_head)
    return failures + _check_failures(pr) if failures else []


def _awaiting_review(pr: dict) -> bool:
    checks = _latest_runs(pr.get("statusCheckRollup"))
    return (
        pr.get("mergeStateStatus") == "BLOCKED"
        and pr.get("reviewDecision") in ("CHANGES_REQUESTED", "REVIEW_REQUIRED")
        and bool(checks)
        and all(_check_passed(check) for check in checks)
    )


def _latest_runs(rollup) -> list:
    """Keeps the newest run of each check, as GitHub's merge state does; a tie keeps the run that did not pass."""
    if not isinstance(rollup, list):
        return []
    latest: dict = {}
    for index, check in enumerate(rollup):
        if not isinstance(check, dict):
            latest[("unreadable", index)] = check
            continue
        key = (check.get("__typename"), check.get("workflowName"), _rollup_name(check))
        if key not in latest or _run_order(check) >= _run_order(latest[key]):
            latest[key] = check
    return list(latest.values())


def _run_order(check: dict) -> tuple[str, bool]:
    started = check.get("startedAt") or UNSTARTED
    return ("~" if started == UNSTARTED else started, not _check_passed(check))


def _check_failures(pr: dict) -> list[str]:
    problems = []
    for check in _latest_runs(pr.get("statusCheckRollup")):
        if _check_passed(check):
            continue
        name = _rollup_name(check) or "unnamed"
        if not isinstance(check, dict):
            problems.append(f"a check in the rollup is unreadable: {check!r}")
        elif check.get("state") in ("PENDING", "EXPECTED") or check.get("status", "COMPLETED") != "COMPLETED":
            observed = check.get("state") or check.get("status")
            problems.append(f"check {name!r} is pending ({observed}): wait for it to finish, then call again")
        else:
            observed = check.get("state") or check.get("conclusion")
            problems.append(f"check {name!r} failed ({observed}): fix the cause and push")
    return problems


def _rollup_name(check) -> Optional[str]:
    if not isinstance(check, dict):
        return None
    return check.get("name") or check.get("context")


def _unmet_rules(pr: dict, rules: list, pr_state: Optional[Callable[[], dict]]) -> list[str]:
    """Names every branch rule other than the approval that the pull request does not meet."""
    problems = []
    types = {rule.get("type") for rule in rules if isinstance(rule, dict)}
    for unknown in sorted(str(t) for t in types - REVIEW_HANDOFF_RULES):
        problems.append(f"branch rule {unknown!r} is one this gate cannot verify, so BLOCKED may not be only the review")
    rollup = pr.get("statusCheckRollup") or []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for required in (rule.get("parameters") or {}).get("required_status_checks") or []:
            context = required.get("context")
            seen = [check for check in rollup if _rollup_name(check) == context]
            if not seen:
                problems.append(f"required check {context!r} has not passed: wait for it, or fix and push")
    wants_threads = any(
        isinstance(rule, dict)
        and rule.get("type") == "pull_request"
        and (rule.get("parameters") or {}).get("required_review_thread_resolution")
        for rule in rules
    )
    if pr_state is not None and (wants_threads or "required_signatures" in types):
        state = pr_state()
        if wants_threads:
            if state["threads_truncated"]:
                problems.append("more than 100 review threads, so the gate cannot confirm they are all resolved")
            elif state["unresolved_threads"]:
                problems.append(f"{state['unresolved_threads']} review thread(s) are unresolved: resolve them")
        if "required_signatures" in types:
            if state["commits_truncated"]:
                problems.append("more than 100 commits, so the gate cannot confirm each is signed")
            elif state["unsigned_commits"]:
                problems.append(f"{state['unsigned_commits']} commit(s) lack a verified signature: publish with push-signed")
    return problems


def _check_passed(check) -> bool:
    if not isinstance(check, dict):
        return False
    if "state" in check:
        return check["state"] == "SUCCESS"
    return check.get("status") == "COMPLETED" and check.get("conclusion") in PASSING_CONCLUSIONS


def _head_failures(pr: dict, local_head: str) -> list[str]:
    remote_head = pr.get("headRefOid")
    if remote_head == local_head:
        return []
    return [
        f"headRefOid is {remote_head!r} but the worktree HEAD is {local_head!r}: "
        "push the worktree's commits, or pull if the remote branch is ahead"
    ]


def _run(cmd: list[str], cwd: str, cap: float, deadline: float) -> str:
    shown = " ".join(cmd)
    timeout = min(cap, deadline - time.monotonic())
    if timeout < MIN_CALL_SECONDS:
        raise GateError(f"the gate ran out of time before it could run `{shown}`")
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GateError(f"`{shown}` timed out after {timeout:.0f}s")
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


def check(tool_name: str, args: Any, env: dict) -> tuple[str, list[str]]:
    deadline = time.monotonic() + HOOK_BUDGET_SECONDS
    workspace = env.get("HERMES_KANBAN_WORKSPACE")
    if not workspace:
        raise GateError("HERMES_KANBAN_WORKSPACE is not set, so the worktree is unknown")
    local_head = _run(["git", "-C", workspace, "rev-parse", "HEAD"], workspace, GIT_TIMEOUT_SECONDS, deadline)
    pr_url = _published_pr(args)
    if pr_url:
        target = pr_url
        repo_args = ["--repo", PR_URL.match(pr_url).group(1)]
    else:
        target = env.get("HERMES_KANBAN_BRANCH") or _run(
            ["git", "-C", workspace, "rev-parse", "--abbrev-ref", "HEAD"], workspace, GIT_TIMEOUT_SECONDS, deadline
        )
        repo_args = []
    raw = _run(["gh", "pr", "view", target, "--json", PR_FIELDS, *repo_args], workspace, GH_TIMEOUT_SECONDS, deadline)
    try:
        pr = json.loads(raw)
    except ValueError:
        raise GateError(f"`gh pr view {target}` returned malformed JSON: {raw[:200]!r}")
    if not isinstance(pr, dict):
        raise GateError(f"`gh pr view {target}` returned {type(pr).__name__}, expected an object")
    return pr.get("url") or target, evaluate(pr, local_head, tool_name, _blockers(pr, workspace, deadline))


def _blockers(pr: dict, workspace: str, deadline: float) -> Optional[Callable[[], list[str]]]:
    match = PR_URL.match(str(pr.get("url")))
    base = pr.get("baseRefName")
    if not match or not isinstance(base, str) or not base:
        return None
    repo = match.group(1)

    def gh_json(args: list[str]):
        out = _run(["gh", *args], workspace, GH_EXTRA_TIMEOUT_SECONDS, deadline)
        try:
            return json.loads(out)
        except ValueError:
            raise GateError(f"`gh {args[0]}` returned malformed JSON: {out[:200]!r}")

    def pr_state() -> dict:
        owner, name = repo.split("/")
        number = pr["url"].rstrip("/").rsplit("/", 1)[1]
        data = gh_json(["api", "graphql", "-f", f"query={THREAD_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"])
        try:
            node = data["data"]["repository"]["pullRequest"]
            threads, commits = node["reviewThreads"], node["commits"]
            return {
                "threads_truncated": threads["pageInfo"]["hasNextPage"],
                "unresolved_threads": sum(1 for t in threads["nodes"] if not t["isResolved"]),
                "commits_truncated": commits["pageInfo"]["hasNextPage"],
                "unsigned_commits": sum(1 for c in commits["nodes"] if not (c["commit"].get("signature") or {}).get("isValid")),
            }
        except (KeyError, TypeError):
            raise GateError(f"the review threads and commit signatures of {pr['url']} could not be read")

    def blockers() -> list[str]:
        rules = gh_json(["api", f"repos/{repo}/rules/branches/{urllib.parse.quote(base, safe='')}?per_page=100"])
        if not isinstance(rules, list):
            raise GateError(f"the branch rules of {repo}:{base} could not be read")
        return _unmet_rules(pr, rules, pr_state)

    return blockers


def make_hook(ctx: Any):
    def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
        if tool_name not in GATED_TOOLS:
            return None
        profile = ctx.profile_name or os.environ.get("HERMES_PROFILE", "")
        if profile not in ctx.get_config("gated_profiles", default=DEFAULT_GATED_PROFILES):
            return None
        try:
            pr, failures = check(tool_name, args, dict(os.environ))
        except GateError as exc:
            return {
                "action": "block",
                "message": (
                    f"pr-ready-gate blocked {tool_name} because the pull request could not be verified: {exc}. "
                    "If no pull request exists for this branch, push and open a ready (non-draft) one; "
                    f"otherwise fix the cause and call {tool_name} again."
                ),
            }
        if not failures:
            return None
        if failures == [MERGED_REVIEW]:
            return {
                "action": "block",
                "message": f"pr-ready-gate blocked {tool_name}: {pr} is already merged. Call kanban_complete instead.",
            }
        lines = "\n".join(f"- {failure}" for failure in failures)
        return {
            "action": "block",
            "message": (
                f"pr-ready-gate blocked {tool_name}: {pr} is not ready to merge.\n{lines}\n"
                f"Call {tool_name} again once every condition holds."
            ),
        }

    return on_pre_tool_call


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", make_hook(ctx))
