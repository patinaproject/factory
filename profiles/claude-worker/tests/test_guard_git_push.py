from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.support import SCRIPTS, GitFixture, git
from tests.test_push_signed import FAKE_GH, GITHUB_URL, REPO

PREFIXES = ["https://github.com/", "git@github.com:", "ssh://git@github.com/"]


class GuardGitPushTest(GitFixture):
    def setUp(self) -> None:
        super().setUp()
        self.global_config = self.tmp / "gitconfig"
        self.global_config.write_text("")
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(self.global_config), "GIT_TERMINAL_PROMPT": "0"}
        git(self.main, "remote", "set-url", "origin", GITHUB_URL)
        # Stands in for github.com: fetches and pushes to GITHUB_URL reach the local bare origin.
        git(self.main, "config", f"url.{self.origin}.insteadOf", GITHUB_URL)
        git(self.main, "checkout", "-q", "-b", "feature")
        (self.main / "change.txt").write_text("change\n")
        git(self.main, "add", "change.txt")
        git(self.main, "commit", "-q", "-m", "Add change")

    def guard(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "guard-git-push"), *args], env=self.env, capture_output=True, text=True
        )

    def git_push(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "push", *args], cwd=self.main, env=self.env, capture_output=True, text=True,
                              timeout=60)

    def origin_has_feature(self) -> bool:
        return subprocess.run(
            ["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "--quiet", "refs/heads/feature"],
            capture_output=True,
        ).returncode == 0

    def test_without_the_guard_a_plain_push_reaches_github(self) -> None:
        self.assertEqual(self.git_push("origin", "feature").returncode, 0)
        self.assertTrue(self.origin_has_feature())

    def test_check_reports_each_missing_entry(self) -> None:
        proc = self.guard("--check")

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.splitlines(), [
            f"global git config: url.no_push_use_push-signed/.pushInsteadOf lacks {prefix}, "
            "so a plain git push to it is not blocked"
            for prefix in PREFIXES
        ])
        self.assertEqual(self.global_config.read_text(), "")

    def test_install_is_silent_and_idempotent(self) -> None:
        for _ in range(2):
            proc = self.guard()
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

        values = subprocess.run(
            ["git", "config", "--global", "--get-all", "url.no_push_use_push-signed/.pushInsteadOf"],
            env=self.env, capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertEqual(values, PREFIXES)
        self.assertEqual(self.guard("--check").returncode, 0)

    def test_guarded_plain_push_fails_and_names_push_signed(self) -> None:
        self.guard()

        for args in (("origin", "feature"), ("--no-verify", "origin", "feature"), (GITHUB_URL, "feature")):
            with self.subTest(args=args):
                proc = self.git_push(*args)

                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("no_push_use_push-signed/example-org/example-repo.git", proc.stderr)
        self.assertFalse(self.origin_has_feature())

    def test_guarded_push_signed_still_publishes(self) -> None:
        self.guard()
        gh = self.tmp / "fake-gh"
        gh.write_text(FAKE_GH)
        os.chmod(gh, 0o755)

        proc = subprocess.run(
            [str(SCRIPTS / "push-signed")], cwd=self.main, capture_output=True, text=True,
            env={**self.env, "GH_BIN": str(gh), "FAKE_GH_ORIGIN": str(self.origin), "FAKE_GH_REPO": REPO,
                 "FAKE_GH_LOG": str(self.tmp / "gh-calls.jsonl")},
        )

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["branch"], "feature")
        self.assertTrue(self.origin_has_feature())
