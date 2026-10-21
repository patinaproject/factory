from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, FakeGithub

REPO = "example-org/example-repo"
LINKBACK = '<!-- linear-linkback -->\n<p><a href="https://linear.app/example/issue/PAT-5348">PAT-5348</a></p>'


class CheckLinearClosesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.github = FakeGithub(self.tmp, REPO)
        self.github.comment_on_issue(204, "linear-code[bot]", LINKBACK)

    def check(self, body: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "check_linear_closes.py")],
            env={"PATH": "/usr/bin:/bin", "GITHUB_REPOSITORY": REPO, "PR_BODY": body, **self.github.env()},
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_a_synced_issue_without_its_linear_line_fails_and_names_the_line(self) -> None:
        self.assertEqual(self.check("Closes #204"), (1, (
            "::error::The body closes #204, which Linear links to PAT-5348, but has no `Closes PAT-5348` line. "
            "Add `Closes PAT-5348` so Linear links this pull request and a card adopts it.\n")))

    def test_a_synced_issue_with_its_linear_line_passes(self) -> None:
        for body in ("Closes #204\nCloses PAT-5348", "Closes #204, closes https://linear.app/example/issue/PAT-5348/x"):
            with self.subTest(body):
                self.assertEqual(self.check(body)[0], 0)

    def test_an_issue_without_a_linkback_needs_no_linear_line(self) -> None:
        self.github.comment_on_issue(205, "someone", LINKBACK)

        self.assertEqual(self.check("Closes #205")[0], 0)

    def test_a_body_that_closes_nothing_needs_no_linear_line(self) -> None:
        for body in ("", "chore: bump the release version", "<!-- Closes #204 -->\n`Closes #204`\n> Closes #204"):
            with self.subTest(body):
                self.assertEqual(self.check(body)[0], 0)

    def test_a_failed_read_fails_the_check(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "check_linear_closes.py")],
            env={"PATH": "/usr/bin:/bin", "GITHUB_REPOSITORY": REPO, "PR_BODY": "Closes #204", "GH_BIN": str(self.tmp / "no-gh")},
            capture_output=True, text=True, timeout=60,
        )

        self.assertEqual((proc.returncode, proc.stdout), (1, f"::error::{self.tmp / 'no-gh'} could not run: No such file or directory\n"))


if __name__ == "__main__":
    unittest.main()
