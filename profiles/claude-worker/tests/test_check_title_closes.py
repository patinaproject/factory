from __future__ import annotations

import json
import subprocess
import sys
import unittest

from tests.support import SCRIPTS

REPO = "example-org/example-repo"


class CheckTitleClosesTest(unittest.TestCase):
    def check(self, title: str, body: str, head_ref: str = "252-example", author: str = "someone",
              commits: list[tuple[str, str]] | None = None) -> tuple[int, str]:
        lines = "\n".join(json.dumps({"sha": sha, "message": message}) for sha, message in commits or [])
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "check_title_closes.py")],
            env={
                "PATH": "/usr/bin:/bin",
                "GITHUB_REPOSITORY": REPO,
                "PR_TITLE": title,
                "PR_BODY": body,
                "PR_HEAD_REF": head_ref,
                "PR_AUTHOR": author,
                "PR_COMMITS": lines,
            },
            capture_output=True, text=True, timeout=60,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_a_fix_title_for_an_issue_the_body_only_mentions_fails_and_names_both_remedies(self) -> None:
        code, output = self.check("fix: #177 a", "Part of #177\n\nCloses #247")
        self.assertEqual(code, 1)
        self.assertIn("#177", output)
        self.assertNotIn("#247", output)
        self.assertIn("Retitle to the issue this pull request completes", output)
        self.assertIn("use another type", output)

    def test_a_fix_title_for_an_issue_the_body_closes_passes(self) -> None:
        self.assertEqual(self.check("fix: #247 a", "Closes #247")[0], 0)

    def test_a_non_closing_type_passes_even_when_the_body_closes_the_issue(self) -> None:
        self.assertEqual(self.check("feat: #177 a", "Closes #177")[0], 0)

    def test_a_closing_keyword_inside_the_subject_fails_on_that_issue_only(self) -> None:
        code, output = self.check("chore: #5 fix #7 typo", f"Closes {REPO}#5")
        self.assertEqual(code, 1)
        self.assertIn("#7", output)
        self.assertNotIn("#5", output)

    def test_every_closing_keyword_form_in_the_title_counts(self) -> None:
        for subject in ("close #9", "closes #9", "closed #9", "fix #9", "fixes #9", "fixed #9",
                        "resolve #9", "resolves #9", "resolved #9", "Resolves: #9",
                        f"fixes {REPO}#9", f"closes https://github.com/{REPO}/issues/9"):
            with self.subTest(subject):
                self.assertEqual(self.check(f"chore: #5 {subject}", "Closes #5")[0], 1)

    def test_a_title_issue_closed_by_any_body_form_passes(self) -> None:
        for body in ("Closes #9", f"Fixes {REPO}#9", f"Resolves https://github.com/{REPO}/issues/9", "closes: #9"):
            with self.subTest(body):
                self.assertEqual(self.check("fix: #9 a", body)[0], 0)

    def test_a_body_line_in_code_or_a_quote_closes_nothing(self) -> None:
        for body in ("`Closes #9`", "> Closes #9", "<!-- Closes #9 -->", "~~Closes #9~~"):
            with self.subTest(body):
                self.assertEqual(self.check("fix: #9 a", body)[0], 1)

    def test_an_issue_of_another_repository_is_not_checked(self) -> None:
        self.assertEqual(self.check("chore: #5 fixes other-org/other-repo#9", "Closes #5")[0], 0)

    def test_the_fix_type_with_a_scope_or_a_breaking_marker_still_names_its_issue(self) -> None:
        for title in ("fix(api): #9 a", "fix!: #9 a"):
            with self.subTest(title):
                self.assertEqual(self.check(title, "Closes #5")[0], 1)

    def test_a_title_naming_no_issue_passes(self) -> None:
        self.assertEqual(self.check("docs: update the readme", "Closes #5")[0], 0)

    def test_every_missing_issue_is_named(self) -> None:
        code, output = self.check("fix: #9 a, closes #8", "Closes #5")
        self.assertEqual(code, 1)
        self.assertIn("#8", output)
        self.assertIn("#9", output)

    def test_exempt_pull_requests_skip_the_check(self) -> None:
        for head_ref, author in (("release-please--branches--main", "someone"), ("bot/bump-1.2.3", "someone"),
                                 ("dependabot/npm_and_yarn/x", "dependabot[bot]")):
            with self.subTest(head_ref, author=author):
                self.assertEqual(self.check("fix: #9 a", "Part of #9", head_ref, author)[0], 0)

    def test_a_fix_commit_for_an_issue_the_body_does_not_close_fails_and_names_the_commit(self) -> None:
        code, output = self.check("feat: #5 a", "Part of #177\n\nCloses #5",
                                  commits=[("a24b147", "fix: #177 tweak")])
        self.assertEqual(code, 1)
        self.assertIn("a24b147", output)
        self.assertIn("#177", output)
        self.assertNotIn("#5 ", output)

    def test_a_published_commit_error_names_the_recovery_push_signed_supports(self) -> None:
        code, output = self.check("feat: #177 a", "Part of #177", commits=[("a24b147", "fix: #177 tweak")])

        self.assertEqual(code, 1)
        self.assertIn("push-signed", output)
        self.assertIn("git switch -c <new-branch> origin/main", output)
        self.assertIn("git merge --squash", output)
        self.assertNotIn("Reword the commit", output)

    def test_a_fix_commit_for_an_issue_the_body_closes_passes(self) -> None:
        self.assertEqual(self.check("feat: #177 a", "Closes #177", commits=[("a24b147", "fix: #177 tweak")])[0], 0)

    def test_a_closing_keyword_in_a_commit_body_fails(self) -> None:
        code, output = self.check("feat: #5 a", "Closes #5",
                                  commits=[("b1", "chore: #5 tidy\n\nResolves #8 as well")])
        self.assertEqual(code, 1)
        self.assertIn("b1", output)
        self.assertIn("#8", output)

    def test_a_commit_naming_an_issue_without_closing_it_passes(self) -> None:
        self.assertEqual(self.check("feat: #5 a", "Closes #5", commits=[("c1", "feat: #5 more\n\nPart of #8")])[0], 0)

    def test_every_commit_that_closes_a_missing_issue_is_named(self) -> None:
        code, output = self.check("feat: #5 a", "Closes #5",
                                  commits=[("e1", "fix: #8 a"), ("e2", "fix: #9 b"), ("e3", "fix: #5 c")])
        self.assertEqual(code, 1)
        for expected in ("e1", "e2", "#8", "#9"):
            self.assertIn(expected, output)
        self.assertNotIn("e3", output)

    def test_exempt_pull_requests_skip_their_commits_too(self) -> None:
        self.assertEqual(self.check("feat: #5 a", "", "bot/bump-1.2.3", commits=[("f1", "fix: #9 a")])[0], 0)


if __name__ == "__main__":
    unittest.main()
