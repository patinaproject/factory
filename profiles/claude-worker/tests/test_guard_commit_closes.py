from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from tests.support import SCRIPTS, GitFixture, git, with_interpreter

REPO = "example-org/example-repo"
# Git refuses to commit without an identity, and a CI runner has none configured.
IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}

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

    def follow_advice(self, refusal: str, message: str, resolve: dict[str, str] | None = None) -> None:
        """Run the refusal's rewrite as a worker would under Claude Code, which sets GIT_EDITOR=true.

        resolve maps a file to its content for each merge the rebase stops on with conflicts.
        """
        commands = re.findall(r"`([^`]+)`", refusal)
        rebase = next(command for command in commands if "git rebase --rebase-merges" in command)
        amend = next(command for command in commands if command.startswith("git commit --amend -m"))
        self.assertIn("git rebase --continue", commands)

        def shell(command: str) -> subprocess.CompletedProcess:
            return subprocess.run(["sh", "-c", command], cwd=self.tree, capture_output=True, text=True,
                                  env={**os.environ, **IDENTITY, "GIT_EDITOR": "true"}, timeout=60)

        stopped = shell(rebase)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        amended = shell(amend.replace("<new message>", message))
        self.assertEqual(amended.returncode, 0, amended.stderr)
        for _ in range(5):
            continued = shell("git rebase --continue")
            if continued.returncode == 0:
                return
            self.assertTrue(resolve, continued.stdout + continued.stderr)
            for name, content in resolve.items():
                (self.tree / name).write_text(content)
                git(self.tree, "add", name)
        self.fail("the rebase did not finish")

    def subjects(self) -> list[str]:
        return git(self.tree, "log", "--format=%s", "HEAD").splitlines()

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

    def merge_default_branch(self, conflict: bool) -> None:
        """Merge an upstream change into the branch, resolving a README conflict by hand when asked."""
        self.push_upstream_commit("fix: #50 other work (#51)")
        if conflict:
            other = self.tmp / "conflict"
            git(self.tmp, "clone", "-q", str(self.origin), str(other))
            (other / "README.md").write_text("upstream\n")
            git(other, "commit", "-q", "-am", "feat: #60 upstream readme (#61)")
            git(other, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(self.tree, "fetch", "-q", "origin")
        merged = subprocess.run(["git", "merge", "-q", "--no-edit", "origin/main"], cwd=self.tree, capture_output=True,
                                env={**os.environ, **IDENTITY})
        self.assertEqual(merged.returncode != 0, conflict)
        if conflict:
            (self.tree / "README.md").write_text("resolved by hand\n")
            git(self.tree, "add", "README.md")
            git(self.tree, "commit", "-q", "--no-edit")

    def publish_readme_change(self) -> None:
        (self.tree / "README.md").write_text("published\n")
        git(self.tree, "commit", "-q", "-am", "feat: #177 published slice")
        git(self.tree, "push", "-q", "origin", "HEAD:refs/heads/275-example")

    def assert_rewritten_and_publishable(self, tree: str) -> None:
        self.assertEqual(self.guard(body="Part of #177").returncode, 0)
        self.assertEqual(git(self.tree, "rev-parse", "HEAD^{tree}"), tree)
        self.assertEqual(git(self.tree, "rev-list", "--count", "--merges", "origin/275-example..HEAD"), "1")
        self.assertIn("feat: #177 next slice", self.subjects())
        self.assertNotIn("fix: #177 next slice", self.subjects())

    def test_following_the_merge_advice_headless_rewords_the_commit_and_then_publishing_is_allowed(self) -> None:
        self.publish_readme_change()
        self.commit("fix: #177 next slice", "next")
        self.merge_default_branch(conflict=False)
        tree = git(self.tree, "rev-parse", "HEAD^{tree}")
        refused = self.guard(body="Part of #177")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("#177", refused.stderr)
        self.assertNotIn("#50", refused.stderr)

        self.follow_advice(refused.stderr, "feat: #177 next slice")

        self.assert_rewritten_and_publishable(tree)

    def test_following_the_merge_advice_before_a_conflicted_merge_resolves_it_again(self) -> None:
        self.publish_readme_change()
        self.commit("fix: #177 next slice", "next")
        self.merge_default_branch(conflict=True)
        tree = git(self.tree, "rev-parse", "HEAD^{tree}")
        refused = self.guard(body="Part of #177")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("conflicts again", refused.stderr)

        self.follow_advice(refused.stderr, "feat: #177 next slice", resolve={"README.md": "resolved by hand\n"})

        self.assert_rewritten_and_publishable(tree)

    def test_following_the_merge_advice_after_a_conflicted_merge_keeps_its_resolution(self) -> None:
        self.publish_readme_change()
        self.merge_default_branch(conflict=True)
        self.commit("fix: #177 next slice", "next")
        tree = git(self.tree, "rev-parse", "HEAD^{tree}")
        refused = self.guard(body="Part of #177")
        self.assertEqual(refused.returncode, 2)

        self.follow_advice(refused.stderr, "feat: #177 next slice")

        self.assert_rewritten_and_publishable(tree)

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

    def test_a_bad_time_limit_refuses_with_its_name_instead_of_crashing(self) -> None:
        self.commit("fix: #177 first slice")

        result = self.guard(body="Part of #177", extra_env={"GUARD_COMMIT_CLOSES_SECONDS": "soon"})

        self.assertEqual(result.returncode, 2)
        self.assertIn("GUARD_COMMIT_CLOSES_SECONDS='soon'", result.stderr)

    def test_the_forms_claude_session_names_for_push_signed_publish(self) -> None:
        self.commit("fix: #177 first slice")
        for command in [
            "/w/scripts/push-signed",
            "'/w/scripts/push-signed'",
            '"/w/scripts/push-signed"',
            "/usr/bin/env python3 /w/scripts/push-signed",
            "python3 -u /w/scripts/push-signed",
            "GH_CONFIG_DIR=/x /w/scripts/push-signed",
        ]:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 2, command)

    def test_a_closing_commit_message_is_refused_before_the_commit_is_made(self) -> None:
        heredoc = "git commit -m \"$(cat <<'EOF'\nfix: #177 it's the first slice\n\nbody\nEOF\n)\""
        for command in [
            "git commit -m 'fix: #177 first slice'",
            "git commit -am 'fix: #177 first slice'",
            "git commit --message='fix: #177 first slice'",
            "git commit -m 'feat: #177 first slice' -m 'Closes #177'",
            f"cd {self.tree} && git add . && GIT_X=1 git commit -q -m 'fix(guard): #177 first slice'",
            heredoc,
        ]:
            result = self.guard(command=command, body="Part of #177\n\nCloses #247")
            self.assertEqual(result.returncode, 2, command)
            self.assertIn("`Part of #177`", result.stderr, command)
            self.assertNotIn("#247", result.stderr, command)

    def test_a_message_file_is_read(self) -> None:
        (self.tree / "message.txt").write_text("fix: #177 first slice\n")

        self.assertEqual(self.guard(command="git commit -F message.txt", body="Part of #177").returncode, 2)
        self.assertEqual(self.guard(command="git commit --file=message.txt", body="Closes #177").returncode, 0)

    def test_the_commit_check_allows_a_commit_it_cannot_or_need_not_judge(self) -> None:
        refused = "git commit -m 'fix: #177 first slice'"
        self.assertEqual(self.guard(command=refused, body="Part of #177").returncode, 2)

        self.assertEqual(self.guard(command=refused, body="Closes #177").returncode, 0)
        self.assertEqual(self.guard(command=refused, body=None).returncode, 0)
        self.assertEqual(self.guard(command="git commit -m 'feat: #177 first slice'", body="Part of #177",
                                    gh_down=True).returncode, 0)
        for command in [
            "git commit",
            "git commit -F -",
            "git commit --amend --no-edit",
            "git commit -m \"$MESSAGE\"",
            "git commit -C HEAD",
        ]:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 0, command)
