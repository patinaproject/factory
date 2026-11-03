from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

DEFAULT_GATED_PROFILES = ["claude-worker"]
TRIAGE_BOARD = "triage"
# Hermes runs its completion contract inside complete_task only, so a review handoff needs this gate too.
GATED_TOOLS = ("kanban_complete", "kanban_request_review")
PR_FIELDS = (
    "url,isDraft,mergeStateStatus,headRefOid,state,reviewDecision,statusCheckRollup,baseRefName,reviewRequests,latestReviews,labels"
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
LINEAR_TIMEOUT_SECONDS = 12
ISSUE_LINE = re.compile(r"^linear_issue_id:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
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
        "merge_queue",
    }
)
THREAD_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100){pageInfo{hasNextPage} nodes{isResolved comments(first:1){nodes{author{__typename login}}}}} "
    "commits(first:100){pageInfo{hasNextPage} nodes{commit{oid signature{isValid}}}} "
    "latestOpinionatedReviews(first:100){pageInfo{hasNextPage} nodes{state author{__typename login} commit{oid}}}}}}"
)
MERGE_QUEUE_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){isInMergeQueue}}}"
)

MERGE_STATE_REMEDIES = {
    "BEHIND": "update the branch from the base branch and push",
    "BLOCKED": (
        "a branch rule is not satisfied: required checks, reviews, or verified commit signatures; "
        "wait for required checks, address reviews, and make sure every commit is signed. "
        "kanban_request_review passes a pull request that waits only for a reviewer, for the merge queue, "
        "or for a person to add an approved pull request to the merge queue (the base branch has an update rule and "
        "the repository declares a merge queue), but it refuses a BLOCKED pull request with none of those to wait for"
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


class Blockers(NamedTuple):
    problems: list[str]
    required_checks: frozenset[str]
    reviewer_threads: int
    stale_approval: bool = False
    update_rule: bool = False
    in_merge_queue: Callable[[], bool] = lambda: False


def evaluate(
    pr: dict,
    local_head: str,
    tool_name: str,
    other_blockers: Optional[Callable[[], Blockers]] = None,
    merge_queue: Optional[dict] = None,
) -> list[str]:
    """Lists what stops the pull request from being ready; an empty list lets the tool call through.

    kanban_request_review also passes an UNSTABLE pull request, and a BLOCKED one that waits for a reviewer: its review
    decision is REVIEW_REQUIRED or CHANGES_REQUESTED, or a person started a review thread that is still open.
    A BLOCKED pull request that is approved and in the merge queue passes too: GitHub reports BLOCKED until the queue merges it.
    So does an approved one that is not queued yet when the base branch has an update rule and merge_queue declares the repository's
    queue: only the queue may write the branch, and a person adds the pull request to it.
    It does so only when other_blockers finds no unmet branch rule. A check that no rule requires does not stop it.
    merge_queue maps "<owner>/<repo>" to the queue's "labels" and "checks"; an entry, even {}, declares the queue.
    """
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
    blockers = None
    hidden: list[str] = []
    required = None
    if tool_name == "kanban_request_review" and other_blockers is not None and state == "OPEN" and merge_state != "CLEAN":
        blockers = other_blockers()
        hidden = blockers.problems
        required = blockers.required_checks
        if not hidden and _awaiting_review(pr, blockers.reviewer_threads):
            merge_state = "CLEAN"
        elif not hidden and merge_state == "BLOCKED" and (
            _approved_and_queued(pr, blockers, merge_queue) or _awaiting_queue_entry(pr, blockers, merge_queue)
        ):
            merge_state = "CLEAN"
    if merge_state != "CLEAN":
        remedy = MERGE_STATE_REMEDIES.get(merge_state, "wait until GitHub reports CLEAN")
        failures.append(f"mergeStateStatus is {merge_state!r}, expected 'CLEAN': {remedy}")
    failures += hidden + _head_failures(pr, local_head)
    return failures + _check_failures(pr, required) if failures else []


def _awaiting_review(pr: dict, reviewer_threads: int) -> bool:
    """True when only a reviewer or the merge queue can clear the merge state; the caller has found no unmet branch rule.

    UNSTABLE means a check that no rule requires is pending or failing. BLOCKED needs a cause that a reviewer clears.
    """
    merge_state = pr.get("mergeStateStatus")
    reviewer_cause = pr.get("reviewDecision") in ("CHANGES_REQUESTED", "REVIEW_REQUIRED") or reviewer_threads > 0
    return bool(_latest_runs(pr.get("statusCheckRollup"))) and (
        merge_state == "UNSTABLE" or (merge_state == "BLOCKED" and reviewer_cause)
    )


def _approved_and_queued(pr: dict, blockers: Blockers, merge_queue: Optional[dict]) -> bool:
    """True when only the merge queue holds an approved pull request at BLOCKED; the caller has found no unmet branch rule."""
    return (
        pr.get("reviewDecision") == "APPROVED"
        and bool(_latest_runs(pr.get("statusCheckRollup")))
        and not blockers.stale_approval
        and (_queue_signal(pr, merge_queue) or blockers.in_merge_queue())
    )


def _queue_config(pr: dict, merge_queue: Optional[dict]) -> Optional[dict]:
    """The repository's merge_queue entry, or None when the repository declares no queue; an empty entry declares one."""
    match = PR_URL.match(str(pr.get("url")))
    entry = (merge_queue or {}).get(match.group(1)) if match else None
    return entry if isinstance(entry, dict) else None


def _queue_entry_candidate(pr: dict, merge_queue: Optional[dict]) -> bool:
    """True when the pull request is approved, BLOCKED, and has checks, and its repository declares a queue."""
    return (
        pr.get("reviewDecision") == "APPROVED"
        and pr.get("mergeStateStatus") == "BLOCKED"
        and bool(_latest_runs(pr.get("statusCheckRollup")))
        and _queue_config(pr, merge_queue) is not None
    )


def _awaiting_queue_entry(pr: dict, blockers: Blockers, merge_queue: Optional[dict]) -> bool:
    """True when a person only has to add the approved pull request to the queue.

    A ruleset update rule lets only its bypass actors write the branch, and the App cannot read them, so the declared queue
    stands for the merge queue. The caller has checked that the pull request is not queued.
    """
    return (
        _queue_entry_candidate(pr, merge_queue)
        and blockers.update_rule
        and not blockers.problems
        and not blockers.stale_approval
    )


def _queue_signal(pr: dict, merge_queue: Optional[dict]) -> bool:
    """True when a configured label is on the pull request or the newest run of a configured check is still pending."""
    config = _queue_config(pr, merge_queue) or {}
    labels = {str(label).lower() for label in config.get("labels") or []}
    checks = set(config.get("checks") or [])
    if any(isinstance(label, dict) and str(label.get("name")).lower() in labels for label in pr.get("labels") or []):
        return True
    return any(
        isinstance(check, dict) and _rollup_name(check) in checks and not _check_passed(check) and not _check_failed(check)
        for check in _latest_runs(pr.get("statusCheckRollup"))
    )


def _failed_checks(pr: dict) -> list[str]:
    return [_rollup_name(check) or "unnamed" for check in _latest_runs(pr.get("statusCheckRollup")) if _check_failed(check)]


def _pending_checks(pr: dict) -> list[str]:
    return [
        _rollup_name(check) or "unnamed"
        for check in _latest_runs(pr.get("statusCheckRollup"))
        if isinstance(check, dict) and not _check_passed(check) and not _check_failed(check)
    ]


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


def _check_failures(pr: dict, required: Optional[frozenset[str]] = None) -> list[str]:
    """required, when given, is the set of check names a rule requires; a pending check outside it is not reported."""
    problems = []
    for check in _latest_runs(pr.get("statusCheckRollup")):
        if _check_passed(check):
            continue
        name = _rollup_name(check) or "unnamed"
        if not isinstance(check, dict):
            problems.append(f"a check in the rollup is unreadable: {check!r}")
        elif not _check_failed(check):
            if required is not None and name not in required:
                continue
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


def _unmet_rules(pr: dict, rules: list, pr_state: Optional[Callable[[], dict]]) -> Blockers:
    """Names every branch rule other than the approval that the pull request does not meet."""
    problems = []
    required_checks: set[str] = set()
    reviewer_threads = 0
    types = {rule.get("type") for rule in rules if isinstance(rule, dict)}
    for unknown in sorted(str(t) for t in types - REVIEW_HANDOFF_RULES):
        problems.append(f"branch rule {unknown!r} is one this gate cannot verify, so BLOCKED may not be only the review")
    latest = _latest_runs(pr.get("statusCheckRollup"))
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        for required in (rule.get("parameters") or {}).get("required_status_checks") or []:
            context = required.get("context")
            required_checks.add(context)
            seen = [check for check in latest if _rollup_name(check) == context]
            if not seen or not all(_check_passed(check) for check in seen):
                problems.append(f"required check {context!r} has not passed: wait for it, or fix and push")
    changes_requested = pr.get("reviewDecision") == "CHANGES_REQUESTED"
    wants_threads = _pull_request_wants(rules, "required_review_thread_resolution")
    wants_head_approval = _pull_request_wants(rules, "require_last_push_approval") and pr.get("reviewDecision") == "APPROVED"
    stale_approval = False
    if pr_state is not None and (wants_threads or "required_signatures" in types or changes_requested or wants_head_approval):
        state = pr_state()
        stale_approval = wants_head_approval and pr.get("headRefOid") not in state["approvals"]
        if changes_requested:
            if state["reviews_truncated"]:
                problems.append("more than 100 reviews, so the gate cannot confirm none requests changes on the current head")
            for login in _change_requesters(pr, state, current=True):
                problems.append(f"{login or 'a deleted user'} requested changes on the current head: address the review and push")
        if wants_threads:
            reviewer_threads = state["reviewer_threads"]
            if state["threads_truncated"]:
                problems.append("more than 100 review threads, so the gate cannot confirm they are all resolved")
            elif state["worker_threads"]:
                problems.append(f"{state['worker_threads']} review thread(s) are unresolved: resolve them")
        if "required_signatures" in types:
            if state["commits_truncated"]:
                problems.append("more than 100 commits, so the gate cannot confirm each is signed")
            elif state["unsigned_commits"]:
                problems.append(f"{state['unsigned_commits']} commit(s) lack a verified signature: publish with push-signed")
    return Blockers(problems, frozenset(required_checks), reviewer_threads, stale_approval, "update" in types)


def _pull_request_wants(rules: list, parameter: str) -> bool:
    return any(
        isinstance(rule, dict) and rule.get("type") == "pull_request" and (rule.get("parameters") or {}).get(parameter)
        for rule in rules
    )


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
        detail = proc.stderr.strip()
        if detail.startswith("Traceback"):
            detail = detail.splitlines()[-1]
        raise GateError(f"`{shown}` exited {proc.returncode}: {detail or proc.stdout.strip()}")
    return proc.stdout.strip()


def _published_pr(args: Any) -> Optional[str]:
    metadata = args.get("metadata") if isinstance(args, dict) else None
    url = metadata.get("published_pr") if isinstance(metadata, dict) else None
    if isinstance(url, str) and PR_URL.match(url.strip()):
        return url.strip()
    return None


def check(tool_name: str, args: Any, env: dict, merge_queue: Optional[dict] = None) -> tuple[str, list[str]]:
    deadline = time.monotonic() + HOOK_BUDGET_SECONDS
    workspace, local_head, pr = _load_pr(args, env, deadline)
    return pr["url"], evaluate(pr, local_head, tool_name, _blockers(pr, workspace, deadline), merge_queue)


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
    try:
        from hermes_cli import kanban_db, kanban_db_connect

        with kanban_db_connect.connect_closing(board=os.environ.get("HERMES_KANBAN_BOARD")) as conn:
            task = kanban_db.get_task(conn, task_id)
    except Exception as exc:
        raise GateError(f"could not read card {task_id}: {type(exc).__name__}: {exc}")
    if task is None:
        raise GateError(f"card {task_id} is not on the board")
    match = ISSUE_LINE.search(task.body or "")
    if match is None:
        raise GateError(f"card {task_id} names no linear_issue_id")
    cli = Path(os.environ.get("HERMES_HOME", "")) / LINEAR_CLI
    out = _run([sys.executable, str(cli), "review-owner", "--issue", match.group(1)], workspace, LINEAR_TIMEOUT_SECONDS, deadline)
    try:
        return json.loads(out)["owner"]
    except (ValueError, KeyError, TypeError):
        raise GateError(f"`review-owner` returned unexpected output: {out[:200]!r}")


def request_owner_review(
    pr: dict, workspace: str, deadline: float, owner_lookup: Callable[[str, str, float], Optional[dict]], asked: list[str]
) -> dict:
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


def _person_started(thread: dict) -> bool:
    """Only a User account counts as a reviewer. A bot, a deleted account, or a thread with no comment is the worker's to resolve."""
    first = ((thread.get("comments") or {}).get("nodes") or [None])[0]
    author = first.get("author") if isinstance(first, dict) else None
    return isinstance(author, dict) and author.get("__typename") == "User"


def _graphql(query: str, pr: dict, repo: str, workspace: str, deadline: float):
    owner, name = repo.split("/")
    return _gh_json(
        ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={_pr_number(pr)}"],
        workspace,
        deadline,
    )


def _in_merge_queue(pr: dict, repo: str, workspace: str, deadline: float) -> bool:
    data = _graphql(MERGE_QUEUE_QUERY, pr, repo, workspace, deadline)
    try:
        return bool(data["data"]["repository"]["pullRequest"]["isInMergeQueue"])
    except (KeyError, TypeError):
        raise GateError(f"whether {pr['url']} is in the merge queue could not be read")


def _pr_state(pr: dict, repo: str, workspace: str, deadline: float) -> dict:
    data = _graphql(THREAD_QUERY, pr, repo, workspace, deadline)
    try:
        node = data["data"]["repository"]["pullRequest"]
        threads, commits, reviews = node["reviewThreads"], node["commits"], node["latestOpinionatedReviews"]
        return {
            "threads_truncated": threads["pageInfo"]["hasNextPage"],
            "worker_threads": sum(1 for t in threads["nodes"] if not t["isResolved"] and not _person_started(t)),
            "reviewer_threads": sum(1 for t in threads["nodes"] if not t["isResolved"] and _person_started(t)),
            "commits_truncated": commits["pageInfo"]["hasNextPage"],
            "unsigned_commits": sum(1 for c in commits["nodes"] if not (c["commit"].get("signature") or {}).get("isValid")),
            "reviews_truncated": reviews["pageInfo"]["hasNextPage"],
            "approvals": [
                (r.get("commit") or {}).get("oid")
                for r in reviews["nodes"]
                if r["state"] == "APPROVED" and (r.get("author") or {}).get("__typename") == "User"
            ],
            "change_requests": [
                ((r.get("author") or {}).get("login"), (r.get("commit") or {}).get("oid"))
                for r in reviews["nodes"]
                if r["state"] == "CHANGES_REQUESTED"
            ],
        }
    except (KeyError, TypeError):
        raise GateError(f"the review threads, commit signatures, and reviews of {pr['url']} could not be read")


def _blockers(pr: dict, workspace: str, deadline: float) -> Optional[Callable[[], Blockers]]:
    match = PR_URL.match(str(pr.get("url")))
    base = pr.get("baseRefName")
    if not match or not isinstance(base, str) or not base:
        return None
    return lambda: _read_blockers(pr, match.group(1), base, workspace, deadline, lambda: _pr_state(pr, match.group(1), workspace, deadline))


def _read_blockers(pr: dict, repo: str, base: str, workspace: str, deadline: float, pr_state: Callable[[], dict]) -> Blockers:
    rules = _gh_json(["api", f"repos/{repo}/rules/branches/{urllib.parse.quote(base, safe='')}?per_page=100"], workspace, deadline)
    if not isinstance(rules, list):
        raise GateError(f"the branch rules of {repo}:{base} could not be read")
    unmet = _unmet_rules(pr, rules, pr_state)
    return unmet._replace(in_merge_queue=lambda: _in_merge_queue(pr, repo, workspace, deadline))


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
            pr, failures = check(tool_name, args, dict(os.environ), ctx.get_config("merge_queue", default={}))
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

    It also asks the Linear issue's owner to review. It names each failed or pending check that no branch rule requires,
    and counts the open review threads that a person started, which the gate let through.
    It sets waiting_for_merge_queue when the pull request is approved and queued, with no such thread, so only the queue is left.
    It sets waiting_for_queue_entry instead when the gate would pass the approved pull request for a person to queue.
    That reads the base branch rules once, and reuses the pull request state it already read.
    Hermes keeps only the first transform_tool_result string, so one hook does all of it.

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
        pending: list[str] = []
        reviewer_threads = 0
        waiting = False
        entry = False
        owner: dict = {}
        try:
            deadline = time.monotonic() + HOOK_BUDGET_SECONDS
            workspace, _, pr = _load_pr(args, dict(os.environ), deadline)
            unrequired = _failed_checks(pr)
            pending = _pending_checks(pr)
            asked, failed = re_request_reviews(pr, workspace, deadline)
            match = PR_URL.match(str(pr.get("url")))
            if pr.get("state") == "OPEN" and match:
                try:
                    state = _pr_state(pr, match.group(1), workspace, deadline)
                    reviewer_threads = state["reviewer_threads"]
                    merge_queue = ctx.get_config("merge_queue", default={})
                    waiting = (
                        pr.get("reviewDecision") == "APPROVED"
                        and pr.get("mergeStateStatus") == "BLOCKED"
                        and not reviewer_threads
                        and (_queue_signal(pr, merge_queue) or _in_merge_queue(pr, match.group(1), workspace, deadline))
                    )
                    base = pr.get("baseRefName")
                    if not waiting and not reviewer_threads and _queue_entry_candidate(pr, merge_queue) and isinstance(base, str) and base:
                        blockers = _read_blockers(pr, match.group(1), base, workspace, deadline, lambda: state)
                        entry = _awaiting_queue_entry(pr, blockers, merge_queue)
                except GateError as exc:
                    failed.append(str(exc))
            owner = request_owner_review(pr, workspace, deadline, owner_lookup, asked)
        except GateError as exc:
            asked, failed = [], [str(exc)]
        if not (asked or failed or unrequired or pending or reviewer_threads or waiting or entry or owner):
            return None
        landed.update(owner)
        if asked:
            landed["re_requested_reviewers"] = asked
        if failed:
            landed["re_request_failures"] = failed
        if unrequired:
            landed["failed_unrequired_checks"] = unrequired
        if pending:
            landed["pending_unrequired_checks"] = pending
        if reviewer_threads:
            landed["reviewer_owned_threads"] = reviewer_threads
        if waiting:
            landed["waiting_for_merge_queue"] = True
        if entry:
            landed["waiting_for_queue_entry"] = True
        return json.dumps(landed)

    return on_tool_result


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", make_hook(ctx))
    ctx.register_hook("transform_tool_result", make_review_hook(ctx))
