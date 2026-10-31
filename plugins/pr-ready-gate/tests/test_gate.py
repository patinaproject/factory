from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "pr_ready_gate", Path(__file__).resolve().parent.parent / "__init__.py"
)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

HEAD = "a" * 40
OTHER = "b" * 40
WORKSPACE = "/work/example-repo/.worktrees/task-1"
BRANCH = "abc-123-example"
PR_URL = "https://github.com/example-org/example-repo/pull/7"
CLEAN = {"url": PR_URL, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": HEAD, "state": "OPEN", "baseRefName": "main"}
WORKER_ENV = {
    "HERMES_KANBAN_TASK": "t_1",
    "HERMES_KANBAN_WORKSPACE": WORKSPACE,
    "HERMES_KANBAN_BRANCH": BRANCH,
}


class FakeCtx:
    def __init__(self, profile="claude-worker", gated=None):
        self.profile_name = profile
        self.settings = {} if gated is None else {"gated_profiles": gated}
        self.hooks = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


class FakeShell:
    def __init__(self, pr=None, gh_stdout=None, gh_returncode=0, gh_raises=None, head=HEAD, branch=BRANCH, rules=None, state=None, post_returncode=0, login="luke"):
        self.post_returncode = post_returncode
        self.login = login
        self.rules = DEFAULT_RULES if rules is None else rules
        self.state = REVIEW_STATE if state is None else state
        self.gh_stdout = json.dumps(pr if pr is not None else CLEAN) if gh_stdout is None else gh_stdout
        self.gh_returncode = gh_returncode
        self.gh_raises = gh_raises
        self.head = head
        self.branch = branch
        self.calls = []

    def __call__(self, cmd, cwd=None, timeout=None, **_):
        self.calls.append((cmd, cwd, timeout))
        if cmd[0] == "git":
            stdout = self.branch if "--abbrev-ref" in cmd else self.head
            return subprocess.CompletedProcess(cmd, 0, stdout + "\n", "")
        if self.gh_raises:
            raise self.gh_raises
        if cmd[1:4] == ["api", "-X", "POST"]:
            stderr = "" if self.post_returncode == 0 else "Reviews may only be requested from collaborators"
            return subprocess.CompletedProcess(cmd, self.post_returncode, "{}", stderr)
        if cmd[1] == "api" and cmd[2].startswith("user/"):
            return subprocess.CompletedProcess(cmd, 0, self.login + "\n", "")
        if cmd[1] == "api":
            body = self.state if cmd[2] == "graphql" else self.rules
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        stderr = "" if self.gh_returncode == 0 else "no pull requests found for branch"
        return subprocess.CompletedProcess(cmd, self.gh_returncode, self.gh_stdout, stderr)

    def gh_call(self):
        return next(call for call in self.calls if call[0][0] == "gh")


PASSING_CHECKS = [
    {"__typename": "CheckRun", "name": "Required checks pass", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "name": "Deploy", "status": "COMPLETED", "conclusion": "SKIPPED"},
    {"__typename": "StatusContext", "context": "Preview", "state": "SUCCESS"},
]
AWAITING_RE_REVIEW = {
    "mergeStateStatus": "BLOCKED",
    "reviewDecision": "CHANGES_REQUESTED",
    "statusCheckRollup": PASSING_CHECKS,
}
AWAITING_APPROVAL = {**AWAITING_RE_REVIEW, "reviewDecision": "REVIEW_REQUIRED"}
DEFAULT_RULES = [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {"required_approving_review_count": 1, "required_review_thread_resolution": True}},
    {"type": "required_signatures"},
    {"type": "required_linear_history"},
    {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Required checks pass"}]}},
]
VALIDATE_REQUIRED = [
    *DEFAULT_RULES[:-1],
    {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Required checks pass"}, {"context": "Validate pull request"}]}},
]
REVIEW_STATE = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{"isResolved": True}]},
                "commits": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"commit": {"oid": HEAD, "signature": {"isValid": True}}}],
                },
                "latestOpinionatedReviews": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"state": "CHANGES_REQUESTED", "author": {"login": "reviewer"}, "commit": {"oid": OTHER}}],
                },
            }
        }
    }
}


def state_with(**overrides):
    node = json.loads(json.dumps(REVIEW_STATE["data"]["repository"]["pullRequest"]))
    node.update(overrides)
    return {"data": {"repository": {"pullRequest": node}}}


def review_nodes(*reviews):
    return {
        "pageInfo": {"hasNextPage": False},
        "nodes": [{"state": "CHANGES_REQUESTED", "author": {"login": login}, "commit": {"oid": oid}} for login, oid in reviews],
    }


def call_hook(shell, env=WORKER_ENV, ctx=None, tool_name="kanban_complete", args=None):
    hook = gate.make_hook(ctx or FakeCtx())
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gate.subprocess, "run", shell):
        return hook(tool_name=tool_name, args=args or {"summary": "done"}, task_id="s", session_id="s")


class EvaluateTest(unittest.TestCase):
    def test_clean_open_ready_pr_at_local_head_passes(self):
        self.assertEqual(gate.evaluate(CLEAN, HEAD, "kanban_complete"), [])

    def test_merged_pr_at_local_head_passes_whatever_its_merge_state(self):
        merged = {**CLEAN, "state": "MERGED", "mergeStateStatus": "UNKNOWN"}
        self.assertEqual(gate.evaluate(merged, HEAD, "kanban_complete"), [])

    def test_merged_pr_at_another_head_is_blocked(self):
        merged = {**CLEAN, "state": "MERGED", "mergeStateStatus": "UNKNOWN", "headRefOid": OTHER}
        self.assertEqual(
            gate.evaluate(merged, HEAD, "kanban_complete"),
            [f"headRefOid is '{OTHER}' but the worktree HEAD is '{HEAD}': push the worktree's commits, or pull if the remote branch is ahead"],
        )

    def test_each_failed_condition_is_reported_with_observed_value(self):
        cases = {
            "state": ("CLOSED", "state is 'CLOSED', expected 'OPEN' or 'MERGED'"),
            "isDraft": (True, "isDraft is True, expected False"),
            "headRefOid": (OTHER, f"headRefOid is '{OTHER}' but the worktree HEAD is '{HEAD}'"),
        }
        for field, (value, expected) in cases.items():
            with self.subTest(field=field):
                failures = gate.evaluate({**CLEAN, field: value}, HEAD, "kanban_complete")
                self.assertEqual(len(failures), 1)
                self.assertTrue(failures[0].startswith(expected), failures[0])

    def test_every_non_clean_merge_state_blocks(self):
        for status in ["UNSTABLE", "BEHIND", "BLOCKED", "DIRTY", "UNKNOWN", "HAS_HOOKS", "DRAFT", None]:
            with self.subTest(status=status):
                failures = gate.evaluate({**CLEAN, "mergeStateStatus": status}, HEAD, "kanban_complete")
                self.assertEqual(len(failures), 1)
                self.assertTrue(failures[0].startswith(f"mergeStateStatus is {status!r}"), failures[0])

    def test_missing_fields_fail_every_condition(self):
        self.assertEqual(len(gate.evaluate({}, HEAD, "kanban_complete")), 4)


class HookScopeTest(unittest.TestCase):
    def test_other_tool_is_ignored(self):
        shell = FakeShell(pr={**CLEAN, "isDraft": True})
        self.assertIsNone(call_hook(shell, tool_name="terminal"))
        self.assertEqual(shell.calls, [])

    def test_gated_profile_without_worker_env_fails_closed(self):
        shell = FakeShell(pr=CLEAN)
        env = {k: v for k, v in WORKER_ENV.items() if not k.startswith("HERMES_KANBAN_")}
        result = call_hook(shell, env=env)
        self.assertEqual(result["action"], "block")
        self.assertIn("HERMES_KANBAN_WORKSPACE is not set", result["message"])
        self.assertEqual(shell.calls, [])

    def test_ungated_profile_is_ignored(self):
        shell = FakeShell(pr={**CLEAN, "isDraft": True})
        self.assertIsNone(call_hook(shell, ctx=FakeCtx(profile="default")))
        self.assertEqual(shell.calls, [])

    def test_configured_gated_profiles_replace_the_default(self):
        shell = FakeShell(pr={**CLEAN, "isDraft": True})
        self.assertIsNone(call_hook(shell, ctx=FakeCtx(profile="claude-worker", gated=["other-worker"])))
        result = call_hook(shell, ctx=FakeCtx(profile="other-worker", gated=["other-worker"]))
        self.assertEqual(result["action"], "block")

    def test_a_triage_board_card_is_not_gated(self):
        shell = FakeShell(pr={**CLEAN, "isDraft": True})
        self.assertIsNone(call_hook(shell, env={**WORKER_ENV, "HERMES_KANBAN_BOARD": "triage"}))
        self.assertEqual(shell.calls, [])

    def test_an_implementation_board_card_is_gated(self):
        shell = FakeShell(pr={**CLEAN, "isDraft": True})
        result = call_hook(shell, env={**WORKER_ENV, "HERMES_KANBAN_BOARD": "default"})
        self.assertEqual(result["action"], "block")

    def test_profile_falls_back_to_hermes_profile_env(self):
        shell = FakeShell(pr={**CLEAN, "isDraft": True})
        result = call_hook(shell, env={**WORKER_ENV, "HERMES_PROFILE": "claude-worker"}, ctx=FakeCtx(profile=""))
        self.assertEqual(result["action"], "block")


class HookDecisionTest(unittest.TestCase):
    def test_ready_pr_allows_completion_with_one_gh_call_on_the_branch(self):
        shell = FakeShell()
        self.assertIsNone(call_hook(shell))
        gh_calls = [call for call in shell.calls if call[0][0] == "gh"]
        self.assertEqual(
            gh_calls,
            [(["gh", "pr", "view", BRANCH, "--json", "url,isDraft,mergeStateStatus,headRefOid,state,reviewDecision,statusCheckRollup,baseRefName,reviewRequests,latestReviews"], WORKSPACE, 15)],
        )

    def test_published_pr_url_is_viewed_with_its_repo(self):
        shell = FakeShell()
        self.assertIsNone(call_hook(shell, args={"metadata": {"published_pr": PR_URL}}))
        self.assertEqual(
            shell.gh_call()[0],
            ["gh", "pr", "view", PR_URL, "--json", "url,isDraft,mergeStateStatus,headRefOid,state,reviewDecision,statusCheckRollup,baseRefName,reviewRequests,latestReviews",
             "--repo", "example-org/example-repo"],
        )

    def test_non_url_published_pr_falls_back_to_branch(self):
        shell = FakeShell()
        call_hook(shell, args={"metadata": {"published_pr": "#7"}})
        self.assertEqual(shell.gh_call()[0][3], BRANCH)

    def test_branch_falls_back_to_worktree_head_ref(self):
        shell = FakeShell(branch="from-git")
        env = {k: v for k, v in WORKER_ENV.items() if k != "HERMES_KANBAN_BRANCH"}
        self.assertIsNone(call_hook(shell, env=env))
        self.assertEqual(shell.gh_call()[0][3], "from-git")

    def test_not_ready_pr_blocks_naming_every_failure(self):
        shell = FakeShell(pr={**CLEAN, "isDraft": True, "mergeStateStatus": "BLOCKED", "headRefOid": OTHER})
        result = call_hook(shell)
        self.assertEqual(result["action"], "block")
        message = result["message"]
        self.assertIn(PR_URL, message)
        self.assertIn("isDraft is True", message)
        self.assertIn("mergeStateStatus is 'BLOCKED'", message)
        self.assertIn(f"headRefOid is '{OTHER}'", message)
        self.assertNotIn("state is 'OPEN'", message)

    def test_review_handoff_with_not_ready_pr_is_blocked(self):
        shell = FakeShell(pr={**CLEAN, "mergeStateStatus": "BLOCKED"})
        result = call_hook(shell, tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")
        self.assertIn("pr-ready-gate blocked kanban_request_review", result["message"])
        self.assertIn("mergeStateStatus is 'BLOCKED'", result["message"])

    def test_review_handoff_after_changes_requested_is_allowed_once_checks_pass(self):
        shell = FakeShell(pr={**CLEAN, **AWAITING_RE_REVIEW})
        self.assertIsNone(call_hook(shell, tool_name="kanban_request_review"))

    def test_review_handoff_after_changes_requested_waits_for_pending_checks(self):
        pending = {**AWAITING_RE_REVIEW, "statusCheckRollup": [*PASSING_CHECKS, {"status": "IN_PROGRESS", "conclusion": ""}]}
        result = call_hook(FakeShell(pr={**CLEAN, **pending}), tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")
        self.assertIn("mergeStateStatus is 'BLOCKED'", result["message"])

    def test_review_handoff_after_changes_requested_refuses_a_failed_required_status(self):
        rules = [*DEFAULT_RULES[:-1], {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Preview"}]}}]
        rollup = [*PASSING_CHECKS[:2], {"__typename": "StatusContext", "context": "Preview", "state": "FAILURE"}]
        failed = {**AWAITING_RE_REVIEW, "statusCheckRollup": rollup}
        result = call_hook(FakeShell(pr={**CLEAN, **failed}, rules=rules), tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")
        self.assertIn("required check 'Preview' has not passed", result["message"])

    def test_review_handoff_after_changes_requested_waits_for_checks_to_register(self):
        unregistered = {**AWAITING_RE_REVIEW, "statusCheckRollup": []}
        result = call_hook(FakeShell(pr={**CLEAN, **unregistered}), tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")

    def review_handoff(self, pr, tool_name="kanban_request_review", **fake):
        return call_hook(FakeShell(pr={**CLEAN, **pr}, **fake), tool_name=tool_name)

    def assert_blocked(self, result, expected):
        self.assertEqual(result["action"], "block")
        self.assertIn(expected, result["message"])

    def test_changes_requested_on_the_current_head_is_refused(self):
        reviews = review_nodes(("reviewer", HEAD))
        self.assert_blocked(
            self.review_handoff(AWAITING_RE_REVIEW, state=state_with(latestOpinionatedReviews=reviews)),
            "reviewer requested changes on the current head",
        )

    def test_changes_requested_on_the_current_head_is_refused_without_thread_or_signature_rules(self):
        rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
        reviews = review_nodes(("reviewer", HEAD))
        self.assert_blocked(
            self.review_handoff(AWAITING_RE_REVIEW, rules=rules, state=state_with(latestOpinionatedReviews=reviews)),
            "reviewer requested changes on the current head",
        )

    def test_more_than_100_reviews_is_refused(self):
        reviews = {"pageInfo": {"hasNextPage": True}, "nodes": []}
        self.assert_blocked(
            self.review_handoff(AWAITING_RE_REVIEW, state=state_with(latestOpinionatedReviews=reviews)),
            "more than 100 reviews",
        )

    def test_changes_requested_on_an_older_head_is_allowed(self):
        self.assertIsNone(self.review_handoff(AWAITING_RE_REVIEW))

    def test_review_handoff_waiting_for_approval_is_allowed(self):
        self.assertIsNone(self.review_handoff(AWAITING_APPROVAL))

    def test_review_handoff_reads_the_rules_of_the_base_branch(self):
        shell = FakeShell(pr={**CLEAN, **AWAITING_APPROVAL, "baseRefName": "feature/x"})
        call_hook(shell, tool_name="kanban_request_review")
        api = [call[0][2] for call in shell.calls if call[0][:2] == ["gh", "api"]]
        self.assertEqual(api[0], "repos/example-org/example-repo/rules/branches/feature%2Fx?per_page=100")

    def test_completion_waiting_for_approval_is_blocked(self):
        shell = FakeShell(pr={**CLEAN, **AWAITING_APPROVAL})
        result = call_hook(shell, tool_name="kanban_complete")
        self.assert_blocked(result, "mergeStateStatus is 'BLOCKED'")
        self.assertEqual([call for call in shell.calls if call[0][:2] == ["gh", "api"]], [])

    def test_approval_wait_with_a_failing_check_no_rule_requires_is_allowed(self):
        failed = [*PASSING_CHECKS, {"__typename": "CheckRun", "name": "Diff infra", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertIsNone(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": failed}))

    def test_approval_wait_with_a_failing_status_no_rule_requires_is_allowed(self):
        failed = [*PASSING_CHECKS, {"__typename": "StatusContext", "context": "Vercel - cms", "state": "FAILURE"}]
        self.assertIsNone(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": failed}))

    def test_approval_wait_with_a_failing_required_check_is_refused_naming_it(self):
        failed = [PASSING_CHECKS[1], {"__typename": "CheckRun", "name": "Required checks pass", "status": "COMPLETED", "conclusion": "FAILURE"}]
        result = self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": failed})
        self.assert_blocked(result, "required check 'Required checks pass' has not passed")
        self.assertIn("check 'Required checks pass' failed (FAILURE)", result["message"])

    def test_completion_with_a_failing_check_no_rule_requires_is_blocked(self):
        failed = [*PASSING_CHECKS, {"__typename": "CheckRun", "name": "Diff infra", "status": "COMPLETED", "conclusion": "FAILURE"}]
        result = self.review_handoff({"mergeStateStatus": "UNSTABLE", "statusCheckRollup": failed}, tool_name="kanban_complete")
        self.assert_blocked(result, "check 'Diff infra' failed (FAILURE)")
        result = self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": failed}, tool_name="kanban_complete")
        self.assert_blocked(result, "check 'Diff infra' failed (FAILURE)")

    def test_approval_wait_with_a_pending_check_is_refused(self):
        pending = [*PASSING_CHECKS, {"__typename": "CheckRun", "name": "Test", "status": "IN_PROGRESS", "conclusion": ""}]
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": pending}), "mergeStateStatus is 'BLOCKED'")

    def test_approval_wait_with_a_required_check_missing_from_the_rollup_is_refused(self):
        rules = [*DEFAULT_RULES[:-1], {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Validate pull request"}]}}]
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, rules=rules), "required check 'Validate pull request' has not passed")

    def test_approval_wait_with_a_second_run_of_a_required_check_failing_is_refused(self):
        twin = {"__typename": "CheckRun", "name": "Required checks pass", "status": "COMPLETED", "conclusion": "FAILURE"}
        pr = {**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, twin]}
        self.assert_blocked(self.review_handoff(pr), "mergeStateStatus is 'BLOCKED'")

    def check_run(self, conclusion, started, status="COMPLETED", name="Validate pull request"):
        return {"__typename": "CheckRun", "name": name, "workflowName": "Pull request", "status": status, "conclusion": conclusion, "startedAt": started}

    def test_approval_wait_after_a_newer_run_replaced_a_failed_one_is_allowed(self):
        rollup = [*PASSING_CHECKS, self.check_run("FAILURE", "2026-10-14T01:09:23Z"), self.check_run("SUCCESS", "2026-10-14T01:10:14Z")]
        self.assertIsNone(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": rollup}))

    def test_approval_wait_whose_latest_run_failed_is_refused_naming_the_check(self):
        rollup = [*PASSING_CHECKS, self.check_run("FAILURE", "2026-10-14T01:10:14Z"), self.check_run("SUCCESS", "2026-10-14T01:09:23Z")]
        result = self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": rollup}, rules=VALIDATE_REQUIRED)
        self.assert_blocked(result, "check 'Validate pull request' failed (FAILURE)")
        self.assertNotIn("Required checks pass", result["message"])

    def test_approval_wait_with_a_pending_rerun_is_refused_naming_the_check(self):
        rollup = [*PASSING_CHECKS, self.check_run("SUCCESS", "2026-10-14T01:09:23Z"), self.check_run("", "0001-01-01T00:00:00Z", status="QUEUED")]
        result = self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": rollup})
        self.assert_blocked(result, "check 'Validate pull request' is pending (QUEUED)")

    def test_runs_that_started_together_count_the_one_that_did_not_pass(self):
        rollup = [*PASSING_CHECKS, self.check_run("FAILURE", "2026-10-14T01:09:23Z"), self.check_run("SUCCESS", "2026-10-14T01:09:23Z")]
        result = self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": rollup}, rules=VALIDATE_REQUIRED)
        self.assert_blocked(result, "check 'Validate pull request' failed")

    def test_a_non_required_check_whose_older_run_passed_is_judged_by_its_latest_run(self):
        rollup = [*PASSING_CHECKS, self.check_run("SUCCESS", "2026-10-14T01:09:23Z"), self.check_run("FAILURE", "2026-10-14T01:10:14Z")]
        self.assertIsNone(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": rollup}))

    def test_a_required_check_whose_older_run_passed_is_judged_by_its_latest_run(self):
        rollup = [*PASSING_CHECKS, self.check_run("SUCCESS", "2026-10-14T01:09:23Z"), self.check_run("FAILURE", "2026-10-14T01:10:14Z")]
        result = self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": rollup}, rules=VALIDATE_REQUIRED)
        self.assert_blocked(result, "required check 'Validate pull request' has not passed")

    def test_a_pending_status_context_is_named(self):
        rollup = [*PASSING_CHECKS, {"__typename": "StatusContext", "context": "Preview deploy", "state": "PENDING"}]
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": rollup}), "check 'Preview deploy' is pending (PENDING)")

    def test_completion_refusal_names_the_failing_check(self):
        rollup = [*PASSING_CHECKS, self.check_run("FAILURE", "2026-10-14T01:09:23Z")]
        result = self.review_handoff({"mergeStateStatus": "UNSTABLE", "statusCheckRollup": rollup}, tool_name="kanban_complete")
        self.assert_blocked(result, "check 'Validate pull request' failed (FAILURE)")

    def test_approval_wait_with_no_registered_checks_is_refused_even_without_required_checks(self):
        rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": []}, rules=rules), "mergeStateStatus is 'BLOCKED'")

    def test_approval_wait_on_a_draft_is_refused(self):
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "isDraft": True}), "isDraft is True")

    def test_approval_wait_at_another_head_is_refused(self):
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "headRefOid": OTHER}), f"headRefOid is '{OTHER}'")

    def test_approval_wait_with_an_unresolved_thread_is_refused(self):
        state = state_with(reviewThreads={"pageInfo": {"hasNextPage": False}, "nodes": [{"isResolved": True}, {"isResolved": False}]})
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, state=state), "1 review thread(s) are unresolved")

    def test_approval_wait_with_unconfirmed_threads_is_refused(self):
        state = state_with(reviewThreads={"pageInfo": {"hasNextPage": True}, "nodes": []})
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, state=state), "more than 100 review threads")

    def test_approval_wait_with_an_unsigned_commit_is_refused(self):
        commits = {"pageInfo": {"hasNextPage": False}, "nodes": [{"commit": {"oid": HEAD, "signature": None}}]}
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, state=state_with(commits=commits)), "1 commit(s) lack a verified signature")

    def test_signatures_required_without_thread_resolution_still_reads_the_commits(self):
        rules = [
            {"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
            {"type": "required_signatures"},
        ]
        commits = {"pageInfo": {"hasNextPage": False}, "nodes": [{"commit": {"oid": HEAD, "signature": None}}]}
        result = self.review_handoff(AWAITING_APPROVAL, rules=rules, state=state_with(commits=commits))
        self.assert_blocked(result, "1 commit(s) lack a verified signature")

    def test_approval_wait_with_unconfirmed_commits_is_refused(self):
        commits = {"pageInfo": {"hasNextPage": True}, "nodes": []}
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, state=state_with(commits=commits)), "more than 100 commits")

    def test_approval_wait_under_an_unknown_rule_is_refused(self):
        rules = [*DEFAULT_RULES, {"type": "required_deployments", "parameters": {}}]
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, rules=rules), "branch rule 'required_deployments' is one this gate cannot verify")

    def test_approval_wait_whose_rules_cannot_be_read_is_refused(self):
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, rules={"message": "Not Found"}), "could not be read")

    def test_thread_and_signature_reads_are_skipped_when_no_rule_asks_for_them(self):
        rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
        shell = FakeShell(pr={**CLEAN, **AWAITING_APPROVAL}, rules=rules)
        self.assertIsNone(call_hook(shell, tool_name="kanban_request_review"))
        self.assertNotIn("graphql", [call[0][2] for call in shell.calls if call[0][:2] == ["gh", "api"]])

    def test_changes_requested_handoff_is_held_to_the_same_rules(self):
        rules = [*DEFAULT_RULES, {"type": "code_scanning", "parameters": {}}]
        self.assert_blocked(self.review_handoff(AWAITING_RE_REVIEW, rules=rules), "cannot verify")

    def test_review_required_on_a_pr_that_is_not_blocked_gets_no_special_treatment(self):
        pr = {**AWAITING_APPROVAL, "mergeStateStatus": "DIRTY"}
        self.assert_blocked(self.review_handoff(pr), "mergeStateStatus is 'DIRTY'")

    def test_completion_after_changes_requested_is_still_blocked(self):
        result = call_hook(FakeShell(pr={**CLEAN, **AWAITING_RE_REVIEW}), tool_name="kanban_complete")
        self.assertEqual(result["action"], "block")
        self.assertIn("mergeStateStatus is 'BLOCKED'", result["message"])

    def test_review_handoff_with_ready_pr_is_allowed(self):
        self.assertIsNone(call_hook(FakeShell(), tool_name="kanban_request_review"))

    def test_review_handoff_with_merged_pr_is_blocked_toward_completion(self):
        merged = {**CLEAN, "state": "MERGED", "mergeStateStatus": "UNKNOWN"}
        result = call_hook(FakeShell(pr=merged), tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")
        self.assertIn("is already merged. Call kanban_complete instead.", result["message"])
        self.assertNotIn("kanban_request_review again", result["message"])

    def test_completion_with_merged_pr_is_allowed(self):
        merged = {**CLEAN, "state": "MERGED", "mergeStateStatus": "UNKNOWN"}
        self.assertIsNone(call_hook(FakeShell(pr=merged), tool_name="kanban_complete"))

    def test_review_handoff_without_a_pr_is_blocked(self):
        result = call_hook(FakeShell(gh_returncode=1), tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")
        self.assertIn("call kanban_request_review again", result["message"])

    def test_local_head_comes_from_the_workspace(self):
        shell = FakeShell(head=OTHER)
        result = call_hook(shell)
        self.assertEqual(result["action"], "block")
        self.assertIn(f"worktree HEAD is '{OTHER}'", result["message"])
        self.assertEqual(shell.calls[0][0], ["git", "-C", WORKSPACE, "rev-parse", "HEAD"])


class FailClosedTest(unittest.TestCase):
    def assert_blocks(self, shell, expected, env=WORKER_ENV):
        result = call_hook(shell, env=env)
        self.assertEqual(result["action"], "block")
        self.assertIn(expected, result["message"])

    def test_gh_failure_blocks(self):
        self.assert_blocks(FakeShell(gh_returncode=1), "exited 1: no pull requests found for branch")

    def test_gh_timeout_blocks(self):
        self.assert_blocks(FakeShell(gh_raises=subprocess.TimeoutExpired("gh", 20)), "timed out after 15s")

    def test_gh_missing_blocks(self):
        self.assert_blocks(FakeShell(gh_raises=FileNotFoundError("gh")), "could not run")

    def test_malformed_json_blocks(self):
        self.assert_blocks(FakeShell(gh_stdout="not json"), "malformed JSON")

    def test_non_object_json_blocks(self):
        self.assert_blocks(FakeShell(gh_stdout="[]"), "returned list, expected an object")

    def test_missing_workspace_blocks(self):
        env = {k: v for k, v in WORKER_ENV.items() if k != "HERMES_KANBAN_WORKSPACE"}
        self.assert_blocks(FakeShell(), "HERMES_KANBAN_WORKSPACE is not set", env=env)


class SlowShell(FakeShell):
    """Every call takes `delays[kind]` seconds on a fake clock and is killed at its timeout, as subprocess.run does."""

    def __init__(self, delays, **kwargs):
        super().__init__(pr={**CLEAN, **AWAITING_APPROVAL}, **kwargs)
        self.delays = delays
        self.now = 1000.0

    def __call__(self, cmd, cwd=None, timeout=None, **kwargs):
        kind = "git" if cmd[0] == "git" else "api" if cmd[1] == "api" else "view"
        delay = self.delays[kind]
        if delay >= timeout:
            self.now += timeout
            raise subprocess.TimeoutExpired(cmd, timeout)
        self.now += delay
        return super().__call__(cmd, cwd=cwd, timeout=timeout, **kwargs)


class TimeBudgetTest(unittest.TestCase):
    def run_slow(self, delays):
        shell = SlowShell(delays)
        env = {k: v for k, v in WORKER_ENV.items() if k != "HERMES_KANBAN_BRANCH"}  # forces the second git call
        start = shell.now
        with mock.patch.object(gate.time, "monotonic", lambda: shell.now):
            result = call_hook(shell, env=env, tool_name="kanban_request_review")
        return result, shell.now - start

    def test_slow_git_and_gh_block_before_hermes_would_kill_the_hook(self):
        result, elapsed = self.run_slow({"git": 2.9, "view": 14.9, "api": 5.9})

        self.assertEqual(result["action"], "block")
        self.assertIn("timed out", result["message"])
        self.assertLessEqual(elapsed, gate.HOOK_BUDGET_SECONDS)
        self.assertLess(gate.HOOK_BUDGET_SECONDS, 30)

    def test_a_call_that_starts_with_no_time_left_is_not_run(self):
        result, elapsed = self.run_slow({"git": 2.9, "view": 14.9, "api": 3.9})

        self.assertEqual(result["action"], "block")
        self.assertIn("ran out of time", result["message"])
        self.assertLessEqual(elapsed, gate.HOOK_BUDGET_SECONDS)

    def test_fast_calls_still_pass(self):
        result, _ = self.run_slow({"git": 0.1, "view": 0.5, "api": 0.5})

        self.assertIsNone(result)


LANDED_IN_REVIEW = json.dumps({"success": True, "task_id": "t_1", "run_id": 3, "status": "review"})
HANDOFF_ARGS = {"summary": "done", "metadata": {"published_pr": PR_URL}}
# patinaproject/patinaproject#2896: head 0fa2d74, tlmader's changes-requested review on 1fa05af.
P2896_HEAD = "0fa2d744095a6d378a9df3540f1641531d7facfe"
P2896_REVIEWED = "1fa05af2732555ab7b653c1764a94287cd76a700"
P2896 = {
    **CLEAN,
    "headRefOid": P2896_HEAD,
    "mergeStateStatus": "BLOCKED",
    "reviewDecision": "CHANGES_REQUESTED",
    "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "Validate pull request", "workflowName": "Pull request", "status": "COMPLETED",
         "conclusion": "SUCCESS", "startedAt": "2026-10-16T03:03:27Z", "completedAt": "2026-10-16T03:03:39Z"},
        {"__typename": "CheckRun", "name": "Required checks pass", "workflowName": "Preview", "status": "COMPLETED",
         "conclusion": "SUCCESS", "startedAt": "2026-10-16T03:00:53Z", "completedAt": "2026-10-16T03:01:01Z"},
    ],
}
P2896_RULES = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]


def call_review_hook(shell, result=LANDED_IN_REVIEW, ctx=None, tool_name="kanban_request_review", args=HANDOFF_ARGS, owner_lookup=None):
    hook = gate.make_review_hook(ctx or FakeCtx(), owner_lookup or gate.linear_owner)
    env = WORKER_ENV if owner_lookup else {k: v for k, v in WORKER_ENV.items() if k != "HERMES_KANBAN_TASK"}
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gate.subprocess, "run", shell):
        return hook(tool_name=tool_name, args=args, result=result, task_id="s", status="ok")


class ReviewHookTest(unittest.TestCase):
    def posts(self, shell):
        return [call[0] for call in shell.calls if call[0][1:4] == ["api", "-X", "POST"]]

    def shell(self, reviews, pr=None, **fake):
        state = state_with(latestOpinionatedReviews=reviews)
        return FakeShell(pr={**CLEAN, **AWAITING_RE_REVIEW, **(pr or {})}, state=state, **fake)

    def test_the_reviewer_on_an_older_head_is_asked_again_and_named_in_the_result(self):
        shell = FakeShell(pr=P2896, rules=P2896_RULES, state=state_with(latestOpinionatedReviews=review_nodes(("tlmader", P2896_REVIEWED))))
        shell.head = P2896_HEAD
        result = json.loads(call_review_hook(shell))
        self.assertEqual(result, {**json.loads(LANDED_IN_REVIEW), "re_requested_reviewers": ["tlmader"]})
        self.assertEqual(
            self.posts(shell),
            [["gh", "api", "-X", "POST", "repos/example-org/example-repo/pulls/7/requested_reviewers", "-f", "reviewers[]=tlmader"]],
        )

    def test_only_the_stale_reviewer_is_asked(self):
        shell = self.shell(review_nodes(("stale", OTHER), ("fresh", HEAD), ("also-stale", "c" * 40)))
        result = json.loads(call_review_hook(shell))
        self.assertEqual(result["re_requested_reviewers"], ["stale", "also-stale"])
        self.assertEqual([post[-1] for post in self.posts(shell)], ["reviewers[]=stale", "reviewers[]=also-stale"])

    def test_a_review_on_the_current_head_is_never_asked_again(self):
        shell = self.shell(review_nodes(("fresh", HEAD)))
        self.assertIsNone(call_review_hook(shell))
        self.assertEqual(self.posts(shell), [])

    def test_a_failed_request_is_reported_and_the_handoff_still_stands(self):
        shell = self.shell(review_nodes(("reviewer", OTHER)), post_returncode=22)
        result = json.loads(call_review_hook(shell))
        self.assertEqual(result["status"], "review")
        self.assertNotIn("re_requested_reviewers", result)
        self.assertIn("reviewer:", result["re_request_failures"][0])
        self.assertIn("Reviews may only be requested from collaborators", result["re_request_failures"][0])

    def test_an_unreadable_pull_request_is_reported_and_the_handoff_still_stands(self):
        result = json.loads(call_review_hook(FakeShell(gh_returncode=1)))
        self.assertEqual(result["status"], "review")
        self.assertIn("no pull requests found", result["re_request_failures"][0])

    def test_nothing_is_sent_when_the_card_completes(self):
        shell = self.shell(review_nodes(("reviewer", OTHER)))
        done = json.dumps({"success": True, "status": "done"})
        self.assertIsNone(call_review_hook(shell, result=done, tool_name="kanban_complete"))
        self.assertIsNone(call_review_hook(shell, result=LANDED_IN_REVIEW, tool_name="kanban_complete"))
        self.assertIsNone(call_review_hook(shell, result=done))
        self.assertEqual(shell.calls, [])

    def test_a_refused_handoff_sends_nothing(self):
        shell = self.shell(review_nodes(("reviewer", OTHER)))
        self.assertIsNone(call_review_hook(shell, result=json.dumps({"error": "pr-ready-gate blocked"})))
        self.assertEqual(shell.calls, [])

    def test_a_merged_pull_request_is_not_asked(self):
        shell = self.shell(review_nodes(("reviewer", OTHER)), pr={"state": "MERGED"})
        self.assertIsNone(call_review_hook(shell))
        self.assertEqual(self.posts(shell), [])

    def test_awaiting_a_first_approval_asks_nobody(self):
        shell = FakeShell(pr={**CLEAN, **AWAITING_APPROVAL})
        self.assertIsNone(call_review_hook(shell))
        self.assertEqual(self.posts(shell), [])

    def test_a_deleted_reviewer_is_never_asked(self):
        reviews = {"pageInfo": {"hasNextPage": False}, "nodes": [{"state": "CHANGES_REQUESTED", "author": None, "commit": {"oid": OTHER}}]}
        shell = self.shell(reviews)
        self.assertIsNone(call_review_hook(shell))
        self.assertEqual(self.posts(shell), [])

    def test_a_failed_check_the_gate_let_through_is_named_in_the_result(self):
        rollup = [*PASSING_CHECKS, {"__typename": "CheckRun", "name": "Diff infra", "status": "COMPLETED", "conclusion": "FAILURE"},
                  {"__typename": "StatusContext", "context": "Vercel - cms", "state": "ERROR"}]
        shell = FakeShell(pr={**CLEAN, **AWAITING_APPROVAL, "statusCheckRollup": rollup})
        result = json.loads(call_review_hook(shell))
        self.assertEqual(result, {**json.loads(LANDED_IN_REVIEW), "failed_unrequired_checks": ["Diff infra", "Vercel - cms"]})
        self.assertEqual(self.posts(shell), [])

    def test_a_failed_check_replaced_by_a_passing_run_is_not_named(self):
        old = {"__typename": "CheckRun", "name": "Diff infra", "status": "COMPLETED", "conclusion": "FAILURE", "startedAt": "2026-10-14T01:09:23Z"}
        new = {**old, "conclusion": "SUCCESS", "startedAt": "2026-10-14T01:10:14Z"}
        shell = FakeShell(pr={**CLEAN, **AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, old, new]})
        self.assertIsNone(call_review_hook(shell))

    def test_an_ungated_profile_is_ignored(self):
        shell = self.shell(review_nodes(("reviewer", OTHER)))
        self.assertIsNone(call_review_hook(shell, ctx=FakeCtx(profile="default")))
        self.assertEqual(shell.calls, [])


LUKE = {"name": "Luke Nguyen", "gitHubUserId": "42"}


class OwnerReviewTest(unittest.TestCase):
    def posts(self, shell):
        return [call[0] for call in shell.calls if call[0][1:4] == ["api", "-X", "POST"]]

    def review(self, owner=LUKE, pr=None, **fake):
        shell = FakeShell(pr={**CLEAN, **AWAITING_APPROVAL, **(pr or {})}, **fake)
        lookups = []

        def lookup(task_id, workspace, deadline):
            lookups.append((task_id, workspace))
            if isinstance(owner, Exception):
                raise owner
            return owner

        result = call_review_hook(shell, owner_lookup=lookup)
        return shell, lookups, None if result is None else json.loads(result)

    def test_the_owner_is_asked_to_review_by_their_github_login(self):
        shell, lookups, result = self.review()
        self.assertEqual(lookups, [("t_1", WORKSPACE)])
        self.assertIn((["gh", "api", "user/42", "--jq", ".login"], WORKSPACE, 6), shell.calls)
        self.assertEqual(
            self.posts(shell),
            [["gh", "api", "-X", "POST", "repos/example-org/example-repo/pulls/7/requested_reviewers", "-f", "reviewers[]=luke"]],
        )
        self.assertEqual(result, {**json.loads(LANDED_IN_REVIEW), "owner_review_requested": "luke"})

    def test_an_owner_without_github_in_linear_is_named_and_not_asked(self):
        shell, _, result = self.review(owner={"name": "Luke Nguyen", "gitHubUserId": None})
        self.assertEqual(self.posts(shell), [])
        self.assertEqual(result["owner_review_not_requested"], "Luke Nguyen has not connected GitHub in Linear")

    def test_an_issue_with_no_owner_asks_nobody_and_says_so(self):
        shell, _, result = self.review(owner=None)
        self.assertEqual(self.posts(shell), [])
        self.assertEqual(result["owner_review_not_requested"], "the Linear issue has no assignee and no one is on triage")

    def test_an_owner_already_asked_or_who_already_reviewed_is_not_asked_again(self):
        for pr in ({"reviewRequests": [{"__typename": "User", "login": "luke"}]},
                   {"latestReviews": [{"author": {"login": "luke"}, "state": "COMMENTED"}]}):
            with self.subTest(pr=pr):
                shell, _, result = self.review(pr=pr)
                self.assertEqual(self.posts(shell), [])
                self.assertIsNone(result)

    def test_an_owner_the_gate_just_re_asked_gets_one_request(self):
        shell = FakeShell(pr={**CLEAN, **AWAITING_RE_REVIEW}, state=state_with(latestOpinionatedReviews=review_nodes(("luke", OTHER))))
        result = json.loads(call_review_hook(shell, owner_lookup=lambda *_: LUKE))
        self.assertEqual([post[-1] for post in self.posts(shell)], ["reviewers[]=luke"])
        self.assertEqual(result["re_requested_reviewers"], ["luke"])
        self.assertNotIn("owner_review_requested", result)

    def test_a_failed_lookup_or_request_is_reported_and_the_handoff_still_stands(self):
        for owner, fake, reason in (
            (gate.GateError("`review-owner` exited 1: Linear is unreachable"), {}, "Linear is unreachable"),
            (LUKE, {"post_returncode": 22}, "Reviews may only be requested from collaborators"),
        ):
            with self.subTest(reason=reason):
                _, _, result = self.review(owner=owner, **fake)
                self.assertEqual(result["status"], "review")
                self.assertNotIn("owner_review_requested", result)
                self.assertIn(reason, result["owner_review_not_requested"])

    def test_a_merged_pull_request_asks_nobody(self):
        shell, lookups, result = self.review(pr={"state": "MERGED"})
        self.assertEqual((lookups, self.posts(shell), result), ([], [], None))

    def read_card(self, body):
        task = None if body is None else types.SimpleNamespace(body=body)
        seen = []

        class Connection:
            def __enter__(self):
                return "conn"

            def __exit__(self, *exc):
                return None

        def connect_closing(board=None):
            seen.append(board)
            return Connection()

        package = types.ModuleType("hermes_cli")
        package.kanban_db = types.SimpleNamespace(get_task=lambda conn, task_id: task)
        package.kanban_db_connect = types.SimpleNamespace(connect_closing=connect_closing)
        return mock.patch.dict(sys.modules, {"hermes_cli": package}), seen

    def test_the_owner_comes_from_linear_agent_sessions_cli_for_the_cards_issue(self):
        calls = []

        def run(cmd, cwd=None, timeout=None, **_):
            calls.append((cmd, cwd))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"owner": LUKE}), "")

        modules, boards = self.read_card("```text\nlinear_issue_id: issue-uuid-1\n```")
        env = {"HERMES_HOME": "/home/worker", "HERMES_KANBAN_BOARD": "factory"}
        with mock.patch.dict(os.environ, env, clear=True), modules, mock.patch.object(gate.subprocess, "run", run):
            owner = gate.linear_owner("t_1", WORKSPACE, gate.time.monotonic() + 20)
        self.assertEqual(owner, LUKE)
        self.assertEqual(boards, ["factory"])
        self.assertEqual(
            calls,
            [([gate.sys.executable, "/home/worker/plugins/linear-agent-session/cli.py", "review-owner", "--issue", "issue-uuid-1"], WORKSPACE)],
        )

    def test_the_gates_own_command_runs_review_owner_in_a_child_without_hermes(self):
        shim = f"""
import json, runpy, sys, urllib.request

sys.modules["hermes_cli"] = None


class Response:
    status = 200

    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def urlopen(request, timeout=None):
    if request.full_url.endswith("/oauth/token"):
        return Response({{"access_token": "t", "token_type": "Bearer", "expires_in": 3600, "scope": "read"}})
    assert json.loads(request.data)["variables"] == {{"id": "issue-uuid-1"}}
    return Response({{"data": {{"issue": {{"assignee": {{"name": "Luke", "gitHubUserId": "9"}}, "team": {{"triageResponsibility": None}}}}}}}})


urllib.request.urlopen = urlopen
runpy.run_path({str(Path(__file__).resolve().parents[2] / "linear-agent-session" / "cli.py")!r}, run_name="__main__")
"""
        modules, _ = self.read_card("```text\nlinear_issue_id: issue-uuid-1\n```")
        with tempfile.TemporaryDirectory() as home:
            cli = Path(home) / gate.LINEAR_CLI
            cli.parent.mkdir(parents=True)
            cli.write_text(shim)
            env = {"HERMES_HOME": home, "LINEAR_CLIENT_ID": "client-id", "LINEAR_CLIENT_SECRET": "client-secret"}
            with mock.patch.dict(os.environ, env, clear=True), modules:
                owner = gate.linear_owner("t_1", home, gate.time.monotonic() + 20)
        self.assertEqual(owner, {"name": "Luke", "gitHubUserId": "9"})

    def test_a_card_with_no_linear_issue_is_named_in_the_reason(self):
        for body in ("repo: o/r", "note: linear_issue_id: other\nrepo: o/r"):
            modules, _ = self.read_card(body)
            with self.subTest(body=body), mock.patch.dict(os.environ, {"HERMES_HOME": "/home/worker"}, clear=True), modules:
                with self.assertRaisesRegex(gate.GateError, "^card t_1 names no linear_issue_id$"):
                    gate.linear_owner("t_1", WORKSPACE, gate.time.monotonic() + 20)

    def test_a_card_that_cannot_be_read_is_named_in_the_reason(self):
        for modules, reason in (
            (mock.patch.dict(sys.modules, {"hermes_cli": None}), "^could not read card t_1: ModuleNotFoundError: "),
            (self.read_card(None)[0], "^card t_1 is not on the board$"),
        ):
            with self.subTest(reason=reason), mock.patch.dict(os.environ, {"HERMES_HOME": "/home/worker"}, clear=True), modules:
                with self.assertRaisesRegex(gate.GateError, reason):
                    gate.linear_owner("t_1", WORKSPACE, gate.time.monotonic() + 20)

    def test_a_cli_error_reaches_the_reason(self):
        def run(cmd, **_):
            return subprocess.CompletedProcess(cmd, 1, json.dumps({"error": "Linear has no issue issue-uuid-1"}), "")

        modules, _ = self.read_card("linear_issue_id: issue-uuid-1")
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/home/worker"}, clear=True), modules, mock.patch.object(gate.subprocess, "run", run):
            with self.assertRaisesRegex(gate.GateError, "Linear has no issue issue-uuid-1"):
                gate.linear_owner("t_1", WORKSPACE, gate.time.monotonic() + 20)

    def test_a_traceback_reaches_the_reason_as_its_last_line(self):
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "cli.py", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'hermes_cli'\n"
        )

        def run(cmd, **_):
            return subprocess.CompletedProcess(cmd, 1, "", traceback)

        modules, _ = self.read_card("linear_issue_id: issue-uuid-1")
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/home/worker"}, clear=True), modules, mock.patch.object(gate.subprocess, "run", run):
            with self.assertRaises(gate.GateError) as caught:
                gate.linear_owner("t_1", WORKSPACE, gate.time.monotonic() + 20)
        self.assertTrue(str(caught.exception).endswith("exited 1: ModuleNotFoundError: No module named 'hermes_cli'"))
        self.assertNotIn("Traceback", str(caught.exception))


class RegisterTest(unittest.TestCase):
    def test_registers_the_gate_and_the_review_hook(self):
        ctx = FakeCtx()
        gate.register(ctx)
        self.assertEqual([name for name, _ in ctx.hooks], ["pre_tool_call", "transform_tool_result"])
        hook = ctx.hooks[0][1]
        with mock.patch.dict(os.environ, WORKER_ENV, clear=True), \
                mock.patch.object(gate.subprocess, "run", FakeShell(pr={**CLEAN, "state": "CLOSED"})):
            result = hook(tool_name="kanban_complete", args={})
        self.assertEqual(result["action"], "block")


if __name__ == "__main__":
    unittest.main()
