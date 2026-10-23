from __future__ import annotations

import importlib.machinery
import json
import os
import random
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
call = " ".join(args[:2])
if os.environ.get("FAKE_GH_FAIL") == call:
    sys.stderr.write("gh: no GitHub remote\\n")
    sys.exit(1)
if os.environ.get("FAKE_GH_GARBAGE") == call:
    print("not json")
    sys.exit(0)
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
              gh_down: bool = False, tool: str = "Bash", extra_env: dict | None = None,
              cwd: str | None = None) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ["PATH"], "GH_BIN": str(self.gh), **(extra_env or {})}
        if body is not None:
            env["FAKE_PR_BODY"] = body
        if gh_down:
            env["FAKE_GH_DOWN"] = "1"
        event = {"tool_name": tool, "cwd": cwd or str(self.tree), "tool_input": {"command": command}}
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
        self.assertIn(f"git reset --soft {git(self.tree, 'merge-base', 'HEAD', 'origin/main')}", result.stderr)
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
            f"cd {self.tree} && git add . && X=1 git commit -q -m 'fix(guard): #177 first slice'",
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

    def test_a_commit_whose_directory_cannot_be_resolved_is_skipped_not_refused(self) -> None:
        for command in [
            "cd ~/x && git commit -m 'fix: #177 first slice'",
            'cd "$W" && git commit -m \'fix: #177 first slice\'',
            'git -C "$D" commit -m \'fix: #177 first slice\'',
            "git -C ~/x commit -m 'fix: #177 first slice'",
            "cd /nonexistent/zzz && git commit -m 'fix: #177 first slice'",
            "git -C /nonexistent/zzz commit -m 'fix: #177 first slice'",
            "cd ../nonexistent && git commit -m 'fix: #177 first slice'",
            "cd -P /nonexistent && git commit -m 'fix: #177 first slice'",
            "cd && git commit -m 'fix: #177 first slice'",
            "pushd /nonexistent/zzz >/dev/null && git commit -m 'fix: #177 first slice'",
            "pushd /nonexistent/zzz >/dev/null; popd >/dev/null; git commit -m 'fix: #177 first slice'",
            f"cd {self.tree} && cd \"$W\" && git commit -m 'fix: #177 first slice'",
        ]:
            for body in (None, "Part of #177"):
                result = self.guard(command=command, body=body)
                self.assertEqual(result.returncode, 0, f"{command} {body}: {result.stderr}")

    def test_a_commit_in_a_directory_that_is_not_a_repository_is_skipped(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()

        result = self.guard(command=f"cd {plain} && git commit -m 'fix: #177 first slice'", body="Part of #177")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_resolvable_directory_is_still_checked(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()

        result = self.guard(command=f"cd {elsewhere} && git -C {self.tree} commit -m 'fix: #177 first slice'", body="Part of #177")

        self.assertEqual(result.returncode, 2)

    def test_the_flat_advice_resets_to_the_merge_base_when_main_moved_ahead(self) -> None:
        self.commit("fix: #177 first slice")
        self.push_upstream_commit("feat: #50 other work (#51)")
        git(self.tree, "fetch", "-q", "origin")
        base = git(self.tree, "merge-base", "HEAD", "origin/main")

        result = self.guard(body="Part of #177")

        self.assertEqual(result.returncode, 2)
        self.assertIn(f"git reset --soft {base}", result.stderr)
        self.assertNotIn("origin/main", result.stderr)
        subprocess.run(["git", "reset", "--soft", base], cwd=self.tree, check=True)
        self.assertEqual(git(self.tree, "diff", "--name-only", base, "HEAD"), "")
        self.assertEqual(git(self.tree, "diff", "--name-only", "--cached"), "work.txt")

    def test_more_forms_of_a_push_signed_call_publish(self) -> None:
        self.commit("fix: #177 first slice")
        for command in [
            "time python3 /w/scripts/push-signed",
            "timeout 120 python3 /w/scripts/push-signed",
            "timeout -s KILL 2m python3 /w/scripts/push-signed",
            "python3 -X utf8 /w/scripts/push-signed",
            "python3 -W ignore /w/scripts/push-signed",
            "time env GH_CONFIG_DIR=/x python3 /w/scripts/push-signed",
            "bash -c 'python3 /w/scripts/push-signed'",
            'bash -c "cd /w && python3 /w/scripts/push-signed"',
            "/bin/sh -c 'GH_CONFIG_DIR=/x python3 /w/scripts/push-signed'",
            "bash -lc 'python3 /w/scripts/push-signed'",
        ]:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 2, command)
        for command in [
            "time grep push-signed README.md",
            "timeout 5 cat /w/scripts/push-signed",
            "bash -c 'grep push-signed README.md'",
            "bash -c 'echo python3 push-signed'",
            "bash script.sh 'python3 push-signed'",
        ]:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 0, command)

    def test_a_command_outside_the_commit_grammar_is_allowed(self) -> None:
        """Each of these failed on 5eb95b9, which read text it did not understand as commands."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        closing = "git commit -m 'fix: #177 first slice'"
        for command in [
            f"cat > notes.txt <<'EOF'\n{closing}\nEOF",
            f"python3 - <<'EOF'\n{closing}\nEOF",
            f"{{ cd {elsewhere}; {closing}; }}",
            f"(cd {elsewhere} && {closing})",
            f"(cd {elsewhere}); {closing}",
            f"builtin cd {elsewhere} && {closing}",
            f"command cd {elsewhere} && {closing}",
            f"GIT_DIR={elsewhere}/.git {closing}",
            f"git --git-dir={elsewhere}/.git commit -m 'fix: #177 first slice'",
            f"git --work-tree={elsewhere} commit -m 'fix: #177 first slice'",
            "git commit --dry-run -m 'fix: #177 first slice'",
            f"{closing} | cat",
            f"{closing} || true",
            f"{closing} &",
            f"{closing} > /dev/null",
            f"echo $(pwd) && {closing}",
            f"echo `pwd` && {closing}",
            "git commit -m \"fix: #177 $(date)\"",
        ]:
            with self.subTest(command):
                result = self.guard(command=command, body="Part of #177")
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_cd_whose_logical_and_physical_targets_differ_is_allowed(self) -> None:
        outer = self.tmp / "outer"
        outer.mkdir()
        (self.tree / "sub").mkdir()
        (outer / "link").symlink_to(self.tree / "sub")
        command = "cd link/.. && git commit -m 'fix: #177 first slice'"

        event = {"tool_name": "Bash", "cwd": str(outer), "tool_input": {"command": command}}
        result = subprocess.run([sys.executable, str(SCRIPTS / "guard-commit-closes")], input=json.dumps(event),
                                env={"PATH": os.environ["PATH"], "GH_BIN": str(self.gh), "FAKE_PR_BODY": "Part of #177"},
                                capture_output=True, text=True, timeout=60)

        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_cd_into_the_worktree_then_a_closing_commit_is_refused(self) -> None:
        (self.tree / "sub").mkdir()
        for command in [
            f"cd {self.tree} && git commit -m 'fix: #177 first slice'",
            f"cd {self.tree}\ngit add -A\ngit commit -am 'fix: #177 first slice'",
            "cd sub && git commit -m 'fix: #177 first slice'",
            f"git -C {self.tree}/sub commit --amend -m 'fix: #177 first slice'",
            "git commit -m \"$(cat <<'EOF'\nfeat: #177 slice\n\nCloses #177\nEOF\n)\"",
        ]:
            with self.subTest(command):
                result = self.guard(command=command, body="Part of #177")
                self.assertEqual(result.returncode, 2)
                self.assertIn("`Part of #177`", result.stderr)

    def test_a_path_through_a_missing_directory_is_unknown_and_the_commit_is_allowed(self) -> None:
        """`nope/..` fails in bash and zsh, so the commit lands where the shell already was, not in the tree."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.tree / "sub").mkdir()
        message = "git commit -m 'fix: #177 first slice'"
        for command in [
            f"cd {self.tree}/nope/.. ; {message}",
            f"cd {self.tree}/nope/../sub ; {message}",
            f"cd {self.tree}/sub/nope/../.. && {message}",
            f"git -C {self.tree}/nope/.. {message[4:]}",
            f"git -C {self.tree}/sub/nope/../.. {message[4:]}",
        ]:
            with self.subTest(command):
                result = self.guard(command=command, body="Part of #177", cwd=str(elsewhere))
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_commit_chained_after_a_commit_that_cannot_run_does_not_run_either(self) -> None:
        """`git -C <not a repository> commit && git commit ...` stops at the first commit in bash and zsh."""
        plain = self.tmp / "plain"
        plain.mkdir()
        first = f"git -C {plain} commit -m 'chore: tidy'"
        closing = "git commit -m 'fix: #177 first slice'"
        for command, code in [(f"{first} && {closing}", 0), (f"{first} && git add . && {closing}", 0),
                              (f"{first} ; {closing}", 2), (f"{first}\n{closing}", 2),
                              (f"{first} && git status ; {closing}", 2)]:
            with self.subTest(command):
                result = self.guard(command=command, body="Part of #177")
                self.assertEqual(result.returncode, code, result.stderr)

    def test_a_path_through_existing_directories_is_still_judged(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.tree / "sub").mkdir()
        message = "git commit -m 'fix: #177 first slice'"
        for command in [
            f"cd {self.tree}/sub/.. && {message}",
            f"git -C {self.tree}/sub/.. {message[4:]}",
        ]:
            with self.subTest(command):
                result = self.guard(command=command, body="Part of #177", cwd=str(elsewhere))
                self.assertEqual(result.returncode, 2)

    def test_a_commit_chained_after_a_cd_into_a_symlink_to_the_tree_is_refused(self) -> None:
        """Git walks the physical path, so both commits land in the tree; #316 walked `link` and found no `.git`."""
        plain = self.tmp / "plain"
        plain.mkdir()
        (self.tree / "sub").mkdir()
        (plain / "link").symlink_to(self.tree / "sub")
        command = ("cd link && git commit --allow-empty -m 'chore: tidy' "
                   "&& git commit --allow-empty -m 'fix: #177 first slice'")

        result = self.guard(command=command, body="Part of #177", cwd=str(plain))

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("`Part of #177`", result.stderr)

    def test_a_long_run_of_separators_is_checked_for_a_publish_in_well_under_a_second(self) -> None:
        loader = importlib.machinery.SourceFileLoader("guard_commit_closes", str(SCRIPTS / "guard-commit-closes"))
        sys.path.insert(0, str(SCRIPTS))
        try:
            guard = loader.load_module()
        finally:
            sys.path.remove(str(SCRIPTS))
        for unit in ["\n", "&&", ";", "&", "| ", "( ", "\n  ", "&&\n", "x/"]:
            command = unit * 20000
            started = time.monotonic()
            self.assertFalse(guard.publishes(command))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.1, f"{unit!r} x 20000 took {elapsed:.2f} s")
        self.assertTrue(guard.publishes("&&\n" * 20000 + "python3 push-signed"))

    def test_one_time_limit_covers_every_commit_in_a_call(self) -> None:
        """On 5eb95b9 each commit got its own limit, so four slow commits took four times as long."""
        command = " && ".join(["git commit -m 'fix: #177 slice'"] * 4)
        started = time.monotonic()

        result = self.guard(command=command, body="Part of #177",
                            extra_env={"FAKE_GH_SLOW": "1.5", "GUARD_COMMIT_CLOSES_SECONDS": "2"})

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_the_guard_refuses_a_generated_command_only_when_it_fits_the_grammar(self) -> None:
        """Mix commands the grammar knows with risky ones, seeded, and refuse exactly the known ones that commit."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.tree / "sub").mkdir()
        commit = "git commit -m 'fix: #177 x'"
        known = [
            (f"cd {self.tree}", False), (f"cd {self.tree}/sub", False), ("cd .", False), ("git add .", False),
            ("git status", False), (commit, True), ("X=1 git commit -qm \"fix: #177 x\"", True),
            (f"git -C {self.tree} commit -am 'fix: #177 x'", True),
            ("git commit -m \"$(cat <<'EOF'\nfix: #177 x\nEOF\n)\"", True),
            ("git commit -m 'feat: #177 x' -m 'Closes #177'", True),
        ]
        risky = [
            f"{{ cd {elsewhere}; }}", f"( cd {elsewhere} )", f"builtin cd {elsewhere}", f"command cd {elsewhere}",
            "cd ~", "cd -", "cd \"$HOME\"", "cd -P .", f"pushd {elsewhere}",
            f"GIT_DIR={elsewhere} {commit}", f"git --git-dir={elsewhere} commit -m 'fix: #177 x'",
            f"git --work-tree={elsewhere} commit -m 'fix: #177 x'", "git commit --dry-run -m 'fix: #177 x'",
            "git commit --amend --no-edit", "git -C \"$D\" commit -m 'fix: #177 x'", "git commit -C HEAD",
            f"cat > f <<'EOF'\n{commit}\nEOF", f"python3 - <<'EOF'\n{commit}\nEOF", f"echo $({commit})",
            f"echo `{commit}`", "true", "echo 'fix: #177 x'", "git commit -m 'fix: #177 x", "git commit -m \"$M\"",
            "git log *", "git push",
        ]
        separators = [("&&", True), (";", True), ("\n", True), ("||", False), ("|", False), ("&", False)]
        rng = random.Random(291)
        for _ in range(120):
            pieces, fits, commits = [], True, False
            for index in range(rng.randint(1, 4)):
                if index:
                    separator, ok = rng.choice(separators)
                    pieces.append(f" {separator} " if separator != "\n" else "\n")
                    fits &= ok
                if rng.random() < 0.6:
                    text, closes = rng.choice(known)
                    commits |= closes
                else:
                    text = rng.choice(risky)
                    fits = False
                pieces.append(text)
            command = "".join(pieces)
            with self.subTest(command):
                self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 2 if fits and commits else 0)

    def test_nice_if_and_negation_before_push_signed_still_publish(self) -> None:
        self.commit("fix: #177 first slice")
        for command in [
            "nice python3 /w/scripts/push-signed",
            "nice -n 5 python3 /w/scripts/push-signed",
            "nice -5 /w/scripts/push-signed",
            "if true; then python3 /w/scripts/push-signed; fi",
            "if python3 /w/scripts/push-signed; then echo ok; fi",
            "! python3 /w/scripts/push-signed",
            "false || ! python3 /w/scripts/push-signed",
            "while true; do python3 /w/scripts/push-signed; break; done",
        ]:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 2, command)
        for command in [
            "if grep push-signed README.md; then echo ok; fi",
            "nice -n 5 grep push-signed README.md",
            "! grep push-signed README.md",
            "echo if python3 /w/scripts/push-signed",
        ]:
            self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 0, command)

    def shim(self, name: str, body: str) -> dict[str, str]:
        directory = self.tmp / f"shim-{name}"
        directory.mkdir()
        (directory / "git").write_text(f"#!/bin/sh\n{body}\n")
        os.chmod(directory / "git", 0o755)
        return {"PATH": f"{directory}:{os.environ['PATH']}"}

    def test_every_commit_time_lookup_failure_allows_the_commit(self) -> None:
        """The commit-time check refuses only when it knows the message and the open pull request's body. The push-time check and CI are the backstop."""
        (self.tree / "bad.txt").write_bytes(b"fix: #177 \xff\xfe first slice\n")
        message = "git commit -m 'fix: #177 first slice'"
        failures = {
            "git fails": (message, self.shim("fails", "exit 1")),
            "git hangs": (message, {**self.shim("hangs", "sleep 30"), "GUARD_COMMIT_CLOSES_SECONDS": "2"}),
            "gh repo view fails": (message, {"FAKE_GH_FAIL": "repo view"}),
            "gh pr list fails": (message, {"FAKE_GH_FAIL": "pr list"}),
            "gh is down": (message, {"FAKE_GH_DOWN": "1"}),
            "gh is not installed": (message, {"GH_BIN": "/nonexistent/gh"}),
            "gh repo view prints no json": (message, {"FAKE_GH_GARBAGE": "repo view"}),
            "gh pr list prints no json": (message, {"FAKE_GH_GARBAGE": "pr list"}),
            "gh is slow": (message, {"FAKE_GH_SLOW": "30", "GUARD_COMMIT_CLOSES_SECONDS": "2"}),
            "the time limit is not a number": (message, {"GUARD_COMMIT_CLOSES_SECONDS": "soon"}),
            "the message file is not text": ("git commit -F bad.txt", {}),
            "the message file is missing": ("git commit -F missing.txt", {}),
            "the command does not parse": ("git commit -m 'fix: #177 first slice", {}),
        }
        for name, (command, env) in failures.items():
            with self.subTest(name):
                result = self.guard(command=command, body="Part of #177", extra_env=env)
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_commit_with_no_remote_is_allowed(self) -> None:
        git(self.tree, "remote", "remove", "origin")

        result = self.guard(command="git commit -m 'fix: #177 first slice'", extra_env={"FAKE_GH_FAIL": "repo view"})

        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_lookup_failure_in_one_commit_does_not_hide_a_refusal_in_another(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        command = f"git -C {elsewhere} commit -m 'fix: #177 a' ; git commit -m 'fix: #177 b'"

        self.assertEqual(self.guard(command=command, body="Part of #177").returncode, 2)
