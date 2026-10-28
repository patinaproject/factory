from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_GATED_PROFILES = ["claude-worker"]
TRIAGE_BOARD = "triage"
# Hermes runs its completion contract inside complete_task only, so a review handoff needs this gate too.
GATED_TOOLS = ("kanban_complete", "kanban_request_review")
PR_FIELDS = (
    "url,isDraft,mergeStateStatus,headRefOid,state,reviewDecision,statusCheckRollup,baseRefName,reviewRequests,latestReviews"
)
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
# The CLI mints a Linear token through the broker, then makes one 10s-bounded Linear request.
LINEAR_TIMEOUT_SECONDS = 12
LINEAR_CLI = Path("plugins") / "linear-agent-session" / "cli.py"
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
    "commits(first:100){pageInfo{hasNextPage} nodes{commit{oid signature{isValid}}}} "
    "latestOpinionatedReviews(first:100){pageInfo{hasNextPage} nodes{state author{login} commit{oid}}}}}}"
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
    """A failed check may still be one no rule requires, which other_blockers judges; a pending one is not judged yet."""
    checks = _latest_runs(pr.get("statusCheckRollup"))
    return (
        pr.get("mergeStateStatus") == "BLOCKED"
        and pr.get("reviewDecision") in ("CHANGES_REQUESTED", "REVIEW_REQUIRED")
        and bool(checks)
        and all(_check_passed(check) or _check_failed(check) for check in checks)
    )


def _failed_checks(pr: dict) -> list[str]:
    return [_rollup_name(check) or "unnamed" for check in _latest_runs(pr.get("statusCheckRollup")) if _check_failed(check)]


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
        elif not _check_failed(check):
            observed = check.get("state") or check.get("status")
            problems.append(f"check {name!r} is pending ({observed}): wait for it to finish, then call again")
        else:
            observed = check.get("state") or check.get("conclusion")
            problems.append(f"check {name!r} failed ({observed}): fix the cause and push")
    return problems


def _change_requesters(pr: dict, state: dict, current: bool) -> list[Optional[str]]:
    """Authors of changes-requested reviews on the current head, or on an older head, whose re-review alone clears them."""
    head = pr.get("headRefOid")
    return [login for login, oid in state["change_requests"] if (oid == head) == current]


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
    latest = _latest_runs(pr.get("statusCheckRollup"))
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for required in (rule.get("parameters") or {}).get("required_status_checks") or []:
            context = required.get("context")
            seen = [check for check in latest if _rollup_name(check) == context]
            if not seen or not all(_check_passed(check) for check in seen):
                problems.append(f"required check {context!r} has not passed: wait for it, or fix and push")
    changes_requested = pr.get("reviewDecision") == "CHANGES_REQUESTED"
    wants_threads = any(
        isinstance(rule, dict)
        and rule.get("type") == "pull_request"
        and (rule.get("parameters") or {}).get("required_review_thread_resolution")
        for rule in rules
    )
    if pr_state is not None and (wants_threads or "required_signatures" in types or changes_requested):
        state = pr_state()
        if changes_requested:
            if state["reviews_truncated"]:
                problems.append("more than 100 reviews, so the gate cannot confirm none requests changes on the current head")
            for login in _change_requesters(pr, state, current=True):
                problems.append(f"{login or 'a deleted user'} requested changes on the current head: address the review and push")
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


def _check_failed(check) -> bool:
    """A finished run that did not pass, as opposed to one still pending."""
    if not isinstance(check, dict) or _check_passed(check):
        return False
    return check.get("state") not in ("PENDING", "EXPECTED") and check.get("status", "COMPLETED") == "COMPLETED"


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
        raise GateError(f"`{shown}` exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def _published_pr(args: Any) -> Optional[str]:
    metadata = args.get("metadata") if isinstance(args, dict) else None
    url = metadata.get("published_pr") if isinstance(metadata, dict) else None
    if isinstance(url, str) and PR_URL.match(url.strip()):
        return url.strip()
    return None


def check(tool_name: str, args: Any, env: dict) -> tuple[str, list[str]]:
    deadline = time.monotonic() + HOOK_BUDGET_SECONDS
    workspace, local_head, pr = _load_pr(args, env, deadline)
    return pr["url"], evaluate(pr, local_head, tool_name, _blockers(pr, workspace, deadline))


def re_request_reviews(pr: dict, workspace: str, deadline: float) -> tuple[list[str], list[str]]:
    """Asks each reviewer whose changes-requested review is on an older head to review again; returns who was asked and what failed."""
    match = PR_URL.match(str(pr.get("url")))
    if pr.get("state") != "OPEN" or pr.get("reviewDecision") != "CHANGES_REQUESTED" or not match:
        return [], []
    repo = match.group(1)
    asked, failed = [], []
    for login in filter(None, _change_requesters(pr, _pr_state(pr, repo, workspace, deadline), current=False)):
        try:
            _gh_json(
                ["api", "-X", "POST", f"repos/{repo}/pulls/{_pr_number(pr)}/requested_reviewers", "-f", f"reviewers[]={login}"],
                workspace,
                deadline,
            )
            asked.append(login)
        except GateError as exc:
            failed.append(f"{login}: {exc}")
    return asked, failed


def linear_owner(task_id: str, workspace: str, deadline: float) -> Optional[dict]:
    """The `name` and `gitHubUserId` of the person who owns the card's Linear issue, from linear-agent-session's CLI."""
    cli = Path(os.environ.get("HERMES_HOME", "")) / LINEAR_CLI
    out = _run([sys.executable, str(cli), "review-owner", "--task", task_id], workspace, LINEAR_TIMEOUT_SECONDS, deadline)
    try:
        return json.loads(out)["owner"]
    except (ValueError, KeyError, TypeError):
        raise GateError(f"`review-owner` returned unexpected output: {out[:200]!r}")


def request_owner_review(
    pr: dict, workspace: str, deadline: float, owner_lookup: Callable[[str, str, float], Optional[dict]], asked: list[str]
) -> dict:
    """Asks the Linear issue's owner to review on GitHub as the App, and returns the result fields that say what happened.

    Linear requests a review only through the acting user's personal GitHub connection, which an agent app cannot have,
    so the owner's login comes from the `gitHubUserId` that Linear keeps for a person who connected GitHub.
    """
    match = PR_URL.match(str(pr.get("url")))
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if pr.get("state") != "OPEN" or not match or not task_id:
        return {}
    try:
        owner = owner_lookup(task_id, workspace, deadline)
        if owner is None:
            return {"owner_review_not_requested": "the Linear issue has no assignee and no one is on triage"}
        if not owner.get("gitHubUserId"):
            return {"owner_review_not_requested": f"{owner.get('name')} has not connected GitHub in Linear"}
        login = _run(["gh", "api", f"user/{owner['gitHubUserId']}", "--jq", ".login"], workspace, GH_EXTRA_TIMEOUT_SECONDS, deadline)
        if login in asked or login in _reviewers(pr):
            return {}
        _gh_json(
            ["api", "-X", "POST", f"repos/{match.group(1)}/pulls/{_pr_number(pr)}/requested_reviewers", "-f", f"reviewers[]={login}"],
            workspace,
            deadline,
        )
        return {"owner_review_requested": login}
    except GateError as exc:
        return {"owner_review_not_requested": str(exc)}


def _reviewers(pr: dict) -> set[str]:
    """Logins already asked to review the pull request or that have reviewed it."""
    requested = {r.get("login") for r in pr.get("reviewRequests") or [] if isinstance(r, dict)}
    reviewed = {(r.get("author") or {}).get("login") for r in pr.get("latestReviews") or [] if isinstance(r, dict)}
    return {login for login in requested | reviewed if login}


def _load_pr(args: Any, env: dict, deadline: float) -> tuple[str, str, dict]:
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
    pr["url"] = pr.get("url") or target
    return workspace, local_head, pr


def _gh_json(args: list[str], workspace: str, deadline: float):
    out = _run(["gh", *args], workspace, GH_EXTRA_TIMEOUT_SECONDS, deadline)
    try:
        return json.loads(out)
    except ValueError:
        raise GateError(f"`gh {args[0]}` returned malformed JSON: {out[:200]!r}")


def _pr_number(pr: dict) -> str:
    return pr["url"].rstrip("/").rsplit("/", 1)[1]


def _pr_state(pr: dict, repo: str, workspace: str, deadline: float) -> dict:
    owner, name = repo.split("/")
    data = _gh_json(
        ["api", "graphql", "-f", f"query={THREAD_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={_pr_number(pr)}"],
        workspace,
        deadline,
    )
    try:
        node = data["data"]["repository"]["pullRequest"]
        threads, commits, reviews = node["reviewThreads"], node["commits"], node["latestOpinionatedReviews"]
        return {
            "threads_truncated": threads["pageInfo"]["hasNextPage"],
            "unresolved_threads": sum(1 for t in threads["nodes"] if not t["isResolved"]),
            "commits_truncated": commits["pageInfo"]["hasNextPage"],
            "unsigned_commits": sum(1 for c in commits["nodes"] if not (c["commit"].get("signature") or {}).get("isValid")),
            "reviews_truncated": reviews["pageInfo"]["hasNextPage"],
            "change_requests": [
                ((r.get("author") or {}).get("login"), (r.get("commit") or {}).get("oid"))
                for r in reviews["nodes"]
                if r["state"] == "CHANGES_REQUESTED"
            ],
        }
    except (KeyError, TypeError):
        raise GateError(f"the review threads, commit signatures, and reviews of {pr['url']} could not be read")


def _blockers(pr: dict, workspace: str, deadline: float) -> Optional[Callable[[], list[str]]]:
    match = PR_URL.match(str(pr.get("url")))
    base = pr.get("baseRefName")
    if not match or not isinstance(base, str) or not base:
        return None
    repo = match.group(1)

    def blockers() -> list[str]:
        rules = _gh_json(["api", f"repos/{repo}/rules/branches/{urllib.parse.quote(base, safe='')}?per_page=100"], workspace, deadline)
        if not isinstance(rules, list):
            raise GateError(f"the branch rules of {repo}:{base} could not be read")
        return _unmet_rules(pr, rules, lambda: _pr_state(pr, repo, workspace, deadline))

    return blockers


def _gated(ctx: Any) -> bool:
    if os.environ.get("HERMES_KANBAN_BOARD") == TRIAGE_BOARD:
        return False
    profile = ctx.profile_name or os.environ.get("HERMES_PROFILE", "")
    return profile in ctx.get_config("gated_profiles", default=DEFAULT_GATED_PROFILES)


def make_hook(ctx: Any):
    def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
        if tool_name not in GATED_TOOLS or not _gated(ctx):
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


def make_review_hook(ctx: Any, owner_lookup: Callable[[str, str, float], Optional[dict]] = linear_owner):
    """After a card lands in review, asks stale changes-requested reviewers to look again and names them in the tool result.

    It also asks the Linear issue's owner to review, and names each failed check, which the gate let through only because
    no branch rule requires it. Hermes keeps only the first transform_tool_result string, so one hook does all of it.

    transform_tool_result is the one hook that can change what the worker reads, and Hermes bounds it at 30s and fails open.
    """

    def on_tool_result(tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> Optional[str]:
        if tool_name != "kanban_request_review" or not _gated(ctx):
            return None
        try:
            landed = json.loads(result)
        except (TypeError, ValueError):
            return None
        if not isinstance(landed, dict) or landed.get("status") != "review":
            return None
        unrequired: list[str] = []
        owner: dict = {}
        try:
            deadline = time.monotonic() + HOOK_BUDGET_SECONDS
            workspace, _, pr = _load_pr(args, dict(os.environ), deadline)
            unrequired = _failed_checks(pr)
            asked, failed = re_request_reviews(pr, workspace, deadline)
            owner = request_owner_review(pr, workspace, deadline, owner_lookup, asked)
        except GateError as exc:
            asked, failed = [], [str(exc)]
        if not asked and not failed and not unrequired and not owner:
            return None
        landed.update(owner)
        if asked:
            landed["re_requested_reviewers"] = asked
        if failed:
            landed["re_request_failures"] = failed
        if unrequired:
            landed["failed_unrequired_checks"] = unrequired
        return json.dumps(landed)

    return on_tool_result


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", make_hook(ctx))
    ctx.register_hook("transform_tool_result", make_review_hook(ctx))
