from __future__ import annotations

import importlib.util
import json
import os
import subprocess
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
    def __init__(self, pr=None, gh_stdout=None, gh_returncode=0, gh_raises=None, head=HEAD, branch=BRANCH, rules=None, state=None):
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

def validate_runs(*conclusions):
    """Check runs of one check, oldest first, in the shape `gh pr view --json statusCheckRollup` returns."""
    return [
        {
            "__typename": "CheckRun",
            "workflowName": "Pull request",
            "name": "Validate pull request",
            "status": "COMPLETED",
            "conclusion": conclusion,
            "startedAt": f"2026-10-14T01:{9 + index:02d}:23Z",
            "completedAt": f"2026-10-14T01:{9 + index:02d}:37Z",
        }
        for index, conclusion in enumerate(conclusions)
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
REVIEW_STATE = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{"isResolved": True}]},
                "commits": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{"commit": {"oid": HEAD, "signature": {"isValid": True}}}],
                },
            }
        }
    }
}


def state_with(**overrides):
    node = json.loads(json.dumps(REVIEW_STATE["data"]["repository"]["pullRequest"]))
    node.update(overrides)
    return {"data": {"repository": {"pullRequest": node}}}


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
            [(["gh", "pr", "view", BRANCH, "--json", "url,isDraft,mergeStateStatus,headRefOid,state,reviewDecision,statusCheckRollup,baseRefName"], WORKSPACE, 15)],
        )

    def test_published_pr_url_is_viewed_with_its_repo(self):
        shell = FakeShell()
        self.assertIsNone(call_hook(shell, args={"metadata": {"published_pr": PR_URL}}))
        self.assertEqual(
            shell.gh_call()[0],
            ["gh", "pr", "view", PR_URL, "--json", "url,isDraft,mergeStateStatus,headRefOid,state,reviewDecision,statusCheckRollup,baseRefName",
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

    def test_review_handoff_after_changes_requested_refuses_a_failed_status(self):
        failed = {**AWAITING_RE_REVIEW, "statusCheckRollup": [*PASSING_CHECKS, {"state": "FAILURE"}]}
        result = call_hook(FakeShell(pr={**CLEAN, **failed}), tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")

    def test_review_handoff_after_changes_requested_waits_for_checks_to_register(self):
        unregistered = {**AWAITING_RE_REVIEW, "statusCheckRollup": []}
        result = call_hook(FakeShell(pr={**CLEAN, **unregistered}), tool_name="kanban_request_review")
        self.assertEqual(result["action"], "block")

    def review_handoff(self, pr, tool_name="kanban_request_review", **fake):
        return call_hook(FakeShell(pr={**CLEAN, **pr}, **fake), tool_name=tool_name)

    def assert_blocked(self, result, expected):
        self.assertEqual(result["action"], "block")
        self.assertIn(expected, result["message"])

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

    def test_approval_wait_with_a_failing_check_is_refused(self):
        failed = [*PASSING_CHECKS, {"__typename": "CheckRun", "name": "Test", "status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": failed}), "mergeStateStatus is 'BLOCKED'")

    def test_approval_wait_with_a_pending_check_is_refused(self):
        pending = [*PASSING_CHECKS, {"__typename": "CheckRun", "name": "Test", "status": "IN_PROGRESS", "conclusion": ""}]
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": pending}), "mergeStateStatus is 'BLOCKED'")

    def test_approval_wait_ignores_a_failed_run_that_a_newer_run_of_the_same_check_replaced(self):
        self.assertIsNone(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, *validate_runs("FAILURE", "SUCCESS")]}))

    def test_the_order_of_the_rollup_does_not_pick_the_latest_run(self):
        reversed_runs = list(reversed(validate_runs("FAILURE", "SUCCESS")))
        self.assertIsNone(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, *reversed_runs]}))

    def test_approval_wait_is_refused_when_the_latest_run_failed_and_names_the_check(self):
        pr = {**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, *validate_runs("SUCCESS", "FAILURE")]}
        result = self.review_handoff(pr)
        self.assert_blocked(result, "check 'Validate pull request' is FAILURE")
        self.assertNotIn("is SUCCESS", result["message"])

    def test_approval_wait_is_refused_while_a_rerun_is_pending_and_names_the_check(self):
        pending = {**validate_runs("SUCCESS")[0], "status": "IN_PROGRESS", "conclusion": "", "completedAt": "0001-01-01T00:00:00Z", "startedAt": "0001-01-01T00:00:00Z"}
        pr = {**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, *validate_runs("SUCCESS"), pending]}
        self.assert_blocked(self.review_handoff(pr), "check 'Validate pull request' is IN_PROGRESS: wait for it")

    def test_same_named_checks_of_different_workflows_are_separate_checks(self):
        other = {**validate_runs("FAILURE")[0], "workflowName": "Preview", "startedAt": "2026-10-14T01:00:00Z"}
        pr = {**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, *validate_runs("SUCCESS"), other]}
        self.assert_blocked(self.review_handoff(pr), "check 'Validate pull request' is FAILURE")

    def test_a_status_context_is_replaced_by_its_newer_report(self):
        old = {"__typename": "StatusContext", "context": "ci/legacy", "state": "FAILURE", "startedAt": "2026-10-14T01:09:00Z"}
        new = {**old, "state": "SUCCESS", "startedAt": "2026-10-14T01:12:00Z"}
        self.assertIsNone(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, old, new]}))

    def test_runs_without_a_timestamp_resolve_to_the_one_that_does_not_pass(self):
        runs = [{k: v for k, v in run.items() if k not in ("startedAt", "completedAt")} for run in validate_runs("SUCCESS", "FAILURE")]
        self.assert_blocked(self.review_handoff({**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, *runs]}), "is FAILURE")

    def test_completion_refusal_names_a_pending_check(self):
        pending = {**validate_runs("SUCCESS")[0], "status": "QUEUED", "conclusion": ""}
        pr = {**CLEAN, "mergeStateStatus": "UNSTABLE", "statusCheckRollup": [pending]}
        self.assert_blocked(call_hook(FakeShell(pr=pr)), "check 'Validate pull request' is QUEUED")

    def test_completion_waiting_for_approval_stays_blocked_despite_a_replaced_failure(self):
        pr = {**CLEAN, **AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, *validate_runs("FAILURE", "SUCCESS")]}
        self.assert_blocked(call_hook(FakeShell(pr=pr)), "mergeStateStatus is 'BLOCKED'")

    def test_approval_wait_with_a_required_check_missing_from_the_rollup_is_refused(self):
        rules = [*DEFAULT_RULES[:-1], {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Validate pull request"}]}}]
        self.assert_blocked(self.review_handoff(AWAITING_APPROVAL, rules=rules), "required check 'Validate pull request' has not passed")

    def test_approval_wait_with_a_second_run_of_a_required_check_failing_is_refused(self):
        twin = {"__typename": "CheckRun", "name": "Required checks pass", "status": "COMPLETED", "conclusion": "FAILURE"}
        pr = {**AWAITING_APPROVAL, "statusCheckRollup": [*PASSING_CHECKS, twin]}
        self.assert_blocked(self.review_handoff(pr), "mergeStateStatus is 'BLOCKED'")

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


class RegisterTest(unittest.TestCase):
    def test_registers_one_pre_tool_call_hook_that_gates(self):
        ctx = FakeCtx()
        gate.register(ctx)
        self.assertEqual([name for name, _ in ctx.hooks], ["pre_tool_call"])
        hook = ctx.hooks[0][1]
        with mock.patch.dict(os.environ, WORKER_ENV, clear=True), \
                mock.patch.object(gate.subprocess, "run", FakeShell(pr={**CLEAN, "state": "CLOSED"})):
            result = hook(tool_name="kanban_complete", args={})
        self.assertEqual(result["action"], "block")


if __name__ == "__main__":
    unittest.main()
