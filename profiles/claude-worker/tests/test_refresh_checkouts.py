from __future__ import annotations

import subprocess

from tests.support import SCRIPTS, GitFixture, git

REPO = "example-org/example-repo"


class RefreshCheckoutsTest(GitFixture):
    def refresh(self, path=None) -> str:
        settings = self.write_settings([{"full_name": REPO, "path": str(path or self.main)}])
        proc = subprocess.run(
            [str(SCRIPTS / "refresh-checkouts"), "--settings-json", str(settings)],
            capture_output=True, text=True, check=True,
        )
        return proc.stdout

    def test_clean_default_branch_fast_forwards_silently(self) -> None:
        upstream = self.push_upstream_commit("upstream")
        self.add_worktree("abc-123-fix")

        self.assertEqual(self.refresh(), "")
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), upstream)

    def test_dirty_checkout_is_reported_and_left_alone(self) -> None:
        before = git(self.main, "rev-parse", "HEAD")
        self.push_upstream_commit("upstream")
        (self.main / "README.md").write_text("local edit\n")

        self.assertEqual(self.refresh(), f"{REPO}: uncommitted changes on main; left unchanged\n")
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), before)
        self.assertEqual((self.main / "README.md").read_text(), "local edit\n")

    def test_other_branch_is_reported_and_left_alone(self) -> None:
        self.push_upstream_commit("upstream")
        git(self.main, "checkout", "-q", "-b", "side")

        self.assertEqual(self.refresh(), f"{REPO}: on side, not main; left unchanged\n")
        self.assertEqual(git(self.main, "symbolic-ref", "--short", "HEAD"), "side")

    def test_diverged_checkout_is_reported_and_left_alone(self) -> None:
        self.push_upstream_commit("upstream")
        (self.main / "local.txt").write_text("local\n")
        git(self.main, "add", "local.txt")
        git(self.main, "commit", "-q", "-m", "local")
        local = git(self.main, "rev-parse", "HEAD")

        self.assertEqual(self.refresh(), f"{REPO}: main cannot fast-forward to origin/main; left unchanged\n")
        self.assertEqual(git(self.main, "rev-parse", "HEAD"), local)

    def test_missing_checkout_is_reported(self) -> None:
        missing = self.tmp / "missing"

        self.assertEqual(self.refresh(missing), f"{REPO}: checkout {missing} does not exist\n")
