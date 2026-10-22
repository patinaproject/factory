from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from tests.support import SCRIPTS, GitFixture, git, with_interpreter

REPO = "example-org/example-repo"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
if os.environ.get("FAKE_GH_SLOW"):
    time.sleep(float(os.environ["FAKE_GH_SLOW"]))
if os.environ.get("FAKE_GH_DOWN"):
    sys.stderr.write("gh: connection refused\\n")
    sys.exit(1)
if args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "example-org/example-repo"}))
elif args[:2] == ["pr", "list"]:
    body = os.environ.get("FAKE_PR_BODY")
    print(json.dumps([] if body is None else [{"body": body}]))
else:
    sys.exit("unexpected gh call " + " ".join(args))
"""


class GuardCommitClosesTest(GitFixture):
    def setUp(self) -> None:
        super().setUp()
        self.gh = self.tmp / "fake-gh"
        self.gh.write_text(with_interpreter(FAKE_GH))
        os.chmod(self.gh, 0o755)
        self.tree = self.add_worktree("275-example")

    def commit(self, message: str, name: str = "work") -> None:
        (self.tree / f"{name}.txt").write_text(message)
        git(self.tree, "add", f"{name}.txt")
        git(self.tree, "commit", "-q", "-m", message)

    def guard(self, command: str = "python3 /w/scripts/push-signed", body: str | None = None,
              gh_down: bool = False, tool: str = "Bash", extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "GH_BIN": str(self.gh), **(extra_env or {})}
        if body is not None:
            env["FAKE_PR_BODY"] = body
        if gh_down:
            env["FAKE_GH_DOWN"] = "1"
        event = {"tool_name": tool, "cwd": str(self.tree), "tool_input": {"command": command}}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "guard-commit-closes")],
            input=json.dumps(event), env=env, capture_output=True, text=True, timeout=60,
        )

    def test_publishing_a_fix_commit_to_a_pull_request_that_only_part_of_closes_is_refused(self) -> None:
        self.commit("fix: #177 first slice")

        result = self.guard(body="Part of #177\n\nCloses #247")

        self.assertEqual(result.returncode, 2)
        self.assertIn("#177", result.stderr)
        self.assertNotIn("#247", result.stderr)
        self.assertIn("git reset --soft origin/main", result.stderr)
        self.assertIn("feat", result.stderr)

    def test_publishing_a_commit_for_an_issue_the_body_closes_is_allowed(self) -> None:
        self.commit("fix: #177 first slice")

        self.assertEqual(self.guard(body="Closes #177").returncode, 0)

    def test_a_commit_that_closes_nothing_is_allowed(self) -> None:
        self.commit("feat: #177 first slice")

        self.assertEqual(self.guard(body="Part of #177").returncode, 0)

    def test_a_commit_already_on_the_remote_branch_is_not_refused_because_it_cannot_be_reworded(self) -> None:
        self.commit("fix: #177 published slice", "published")
        git(self.tree, "push", "-q", "origin", "HEAD:refs/heads/275-example")
        self.commit("feat: #177 next slice", "next")

        self.assertEqual(self.guard(body="Part of #177").returncode, 0)

    def test_an_unpublished_commit_after_a_published_one_is_still_checked(self) -> None:
        self.commit("feat: #177 published slice", "published")
        git(self.tree, "push", "-q", "origin", "HEAD:refs/heads/275-example")
        self.commit("fix: #177 next slice", "next")

        result = self.guard(body="Part of #177")

        self.assertEqual(result.returncode, 2)
        self.assertIn("git reset --soft origin/275-example", result.stderr)

    def test_without_a_pull_request_there_is_no_body_to_compare(self) -> None:
        self.commit("fix: #177 first slice")

        self.assertEqual(self.guard(body=None).returncode, 0)

    def test_a_call_that_does_not_publish_is_ignored(self) -> None:
        self.commit("fix: #177 first slice")

        self.assertEqual(self.guard(command="git status", body="Part of #177").returncode, 0)
        self.assertEqual(self.guard(body="Part of #177", tool="Read").returncode, 0)

    def test_a_github_failure_refuses_instead_of_publishing_unchecked(self) -> None:
        self.commit("fix: #177 first slice")

        result = self.guard(body="Part of #177", gh_down=True)

        self.assertEqual(result.returncode, 2)
        self.assertIn("connection refused", result.stderr)

    def test_a_local_merge_of_the_default_branch_does_not_count_the_commits_it_brought_in(self) -> None:
        self.commit("feat: #177 published slice", "published")
        git(self.tree, "push", "-q", "origin", "HEAD:refs/heads/275-example")
        self.push_upstream_commit("fix: #50 other work (#51)")
        git(self.tree, "fetch", "-q", "origin")
        git(self.tree, "merge", "-q", "--no-edit", "origin/main")

        self.assertEqual(self.guard(body="Part of #177").returncode, 0)

    def test_a_commit_before_a_local_merge_is_refused_with_advice_that_keeps_the_merge(self) -> None:
        self.commit("feat: #177 published slice", "published")
        git(self.tree, "push", "-q", "origin", "HEAD:refs/heads/275-example")
        self.commit("fix: #177 next slice", "next")
        self.push_upstream_commit("fix: #50 other work (#51)")
        git(self.tree, "fetch", "-q", "origin")
        git(self.tree, "merge", "-q", "--no-edit", "origin/main")

        result = self.guard(body="Part of #177")

        self.assertEqual(result.returncode, 2)
        self.assertIn("#177", result.stderr)
        self.assertNotIn("#50", result.stderr)
        self.assertNotIn("reset --soft", result.stderr)
        self.assertIn("--rebase-merges", result.stderr)

    def test_a_slow_github_refuses_within_the_time_limit(self) -> None:
        self.commit("fix: #177 first slice")
        started = time.monotonic()

        result = self.guard(body="Part of #177", extra_env={"FAKE_GH_SLOW": "30", "GUARD_COMMIT_CLOSES_SECONDS": "2"})

        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("time", result.stderr)

    def test_only_a_call_that_runs_push_signed_publishes(self) -> None:
        self.commit("fix: #177 first slice")
        publishing = [
            "python3 /w/scripts/push-signed",
            "python3 -I /w/scripts/push-signed",
            "GH_CONFIG_DIR=/x python3 /w/scripts/push-signed",
            "GH_CONFIG_DIR=/x python3 -I /w/scripts/push-signed",
            "cd /w && python3 /w/scripts/push-signed",
            "/usr/bin/python3 /w/scripts/push-signed",
            "env GH_CONFIG_DIR=/x python3 /w/scripts/push-signed",
        ]
        ignored = [
            "grep push-signed README.md",
            "cat /w/scripts/push-signed",
            "git commit -m 'document push-signed'",
            "git commit -m 'run python3 /w/scripts/push-signed'",
            "echo python3 push-signed",
        ]
        for command in publishing:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 2, command)
        for command in ignored:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 0, command)
