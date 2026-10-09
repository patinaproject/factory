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
CLEAN = {"url": PR_URL, "isDraft": False, "mergeStateStatus": "CLEAN", "headRefOid": HEAD, "state": "OPEN"}
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
    def __init__(self, pr=None, gh_stdout=None, gh_returncode=0, gh_raises=None, head=HEAD, branch=BRANCH):
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
        stderr = "" if self.gh_returncode == 0 else "no pull requests found for branch"
        return subprocess.CompletedProcess(cmd, self.gh_returncode, self.gh_stdout, stderr)

    def gh_call(self):
        return next(call for call in self.calls if call[0][0] == "gh")


def call_hook(shell, env=WORKER_ENV, ctx=None, tool_name="kanban_complete", args=None):
    hook = gate.make_hook(ctx or FakeCtx())
    with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gate.subprocess, "run", shell):
        return hook(tool_name=tool_name, args=args or {"summary": "done"}, task_id="s", session_id="s")


class EvaluateTest(unittest.TestCase):
    def test_clean_open_ready_pr_at_local_head_passes(self):
        self.assertEqual(gate.evaluate(CLEAN, HEAD), [])

    def test_each_failed_condition_is_reported_with_observed_value(self):
        cases = {
            "state": ("CLOSED", "state is 'CLOSED', expected 'OPEN'"),
            "isDraft": (True, "isDraft is True, expected False"),
            "headRefOid": (OTHER, f"headRefOid is '{OTHER}' but the worktree HEAD is '{HEAD}'"),
        }
        for field, (value, expected) in cases.items():
            with self.subTest(field=field):
                failures = gate.evaluate({**CLEAN, field: value}, HEAD)
                self.assertEqual(len(failures), 1)
                self.assertTrue(failures[0].startswith(expected), failures[0])

    def test_every_non_clean_merge_state_blocks(self):
        for status in ["UNSTABLE", "BEHIND", "BLOCKED", "DIRTY", "UNKNOWN", "HAS_HOOKS", "DRAFT", None]:
            with self.subTest(status=status):
                failures = gate.evaluate({**CLEAN, "mergeStateStatus": status}, HEAD)
                self.assertEqual(len(failures), 1)
                self.assertTrue(failures[0].startswith(f"mergeStateStatus is {status!r}"), failures[0])

    def test_missing_fields_fail_every_condition(self):
        self.assertEqual(len(gate.evaluate({}, HEAD)), 4)


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
            [(["gh", "pr", "view", BRANCH, "--json", "url,isDraft,mergeStateStatus,headRefOid,state"], WORKSPACE, 20)],
        )

    def test_published_pr_url_is_viewed_with_its_repo(self):
        shell = FakeShell()
        self.assertIsNone(call_hook(shell, args={"metadata": {"published_pr": PR_URL}}))
        self.assertEqual(
            shell.gh_call()[0],
            ["gh", "pr", "view", PR_URL, "--json", "url,isDraft,mergeStateStatus,headRefOid,state",
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
        self.assert_blocks(FakeShell(gh_raises=subprocess.TimeoutExpired("gh", 20)), "timed out after 20s")

    def test_gh_missing_blocks(self):
        self.assert_blocks(FakeShell(gh_raises=FileNotFoundError("gh")), "could not run")

    def test_malformed_json_blocks(self):
        self.assert_blocks(FakeShell(gh_stdout="not json"), "malformed JSON")

    def test_non_object_json_blocks(self):
        self.assert_blocks(FakeShell(gh_stdout="[]"), "returned list, expected an object")

    def test_missing_workspace_blocks(self):
        env = {k: v for k, v in WORKER_ENV.items() if k != "HERMES_KANBAN_WORKSPACE"}
        self.assert_blocks(FakeShell(), "HERMES_KANBAN_WORKSPACE is not set", env=env)


class RegisterTest(unittest.TestCase):
    def test_registers_one_pre_tool_call_hook_that_gates(self):
        ctx = FakeCtx()
        gate.register(ctx)
        self.assertEqual([name for name, _ in ctx.hooks], ["pre_tool_call"])
        hook = ctx.hooks[0][1]
        with mock.patch.dict(os.environ, WORKER_ENV, clear=True), \
                mock.patch.object(gate.subprocess, "run", FakeShell(pr={**CLEAN, "state": "MERGED"})):
            result = hook(tool_name="kanban_complete", args={})
        self.assertEqual(result["action"], "block")


if __name__ == "__main__":
    unittest.main()
