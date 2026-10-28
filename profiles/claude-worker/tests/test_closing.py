from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import closing  # noqa: E402

REPO = "example-org/example-repo"
APP = "factory-app[bot]"
LINEAR_URL = "https://linear.app/x/issue/PAT-9"


def session_module():
    loader = importlib.machinery.SourceFileLoader("claude_session_for_closing", str(SCRIPTS / "claude-session"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def workflow_check(body: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "closing.py")],
        env={"PATH": "/usr/bin:/bin", "GITHUB_REPOSITORY": REPO, "PR_BODY": body},
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout + proc.stderr


class ClosesTest(unittest.TestCase):
    def test_each_closing_line_gives_its_reference(self) -> None:
        cases = {
            "Closes #204": {"#204"},
            "fixes: #7 and resolved #8": {"#7", "#8"},
            f"Closes {REPO}#9": {"#9"},
            "Closes someone/else#9": set(),
            f"Closes https://github.com/{REPO}/issues/10": {"#10"},
            "Closes https://github.com/someone/else/issues/10": set(),
            "Closes PAT-5348": {"PAT-5348"},
            "closes pat-5348": {"PAT-5348"},
            "Closes https://linear.app/example/issue/PAT-5325/report-usage": {"PAT-5325"},
            "Closes #190, closes https://linear.app/example/issue/PAT-5325/x": {"#190", "PAT-5325"},
            "Mentions #204 and PAT-5348": set(),
            "Closes [#5](https://github.com/example-org/example-repo/issues/5)": {"#5"},
            "Resolves [PAT-9](https://linear.app/x/issue/PAT-9/the-title)": {"PAT-9"},
            "Closes [#5](https://example.com/5)": set(),
            "Closes [#5]": set(),
            "`Closes #1`\n<!-- Closes #2 -->\n```\nCloses #3\n```\nCloses #4": {"#4"},
            "~~Closes #1~~\n> Closes #2\n~~~\nCloses #3\n~~~\nCloses #4": {"#4"},
        }
        for body, expected in cases.items():
            with self.subTest(body):
                self.assertEqual(closing.closes(body, REPO), expected)

    def test_a_fix_type_closes_its_issue_like_a_closing_keyword(self) -> None:
        cases = {
            "fix: #5 a": {"#5"},
            "fix(scope)!:#5 a": {"#5"},
            "feat: #5 a\n\nCloses #6 and PAT-9": {"#6"},
            "feat: #5 a\n\nPart of #5": set(),
        }
        for message, expected in cases.items():
            with self.subTest(message):
                self.assertEqual(closing.message_closes(message, REPO), expected)
                self.assertEqual(closing.might_close(message), message.startswith("fix") or "Closes" in message)

    def test_an_issue_of_another_repository_is_missing_until_the_body_closes_it(self) -> None:
        wanted = ["someone/else#9", "#3", "PAT-9"]
        self.assertEqual(closing.missing("Closes #3", REPO, wanted), ["someone/else#9", "PAT-9"])
        self.assertEqual(closing.missing("Closes Someone/Else#9, closes https://linear.app/x/issue/PAT-9", REPO, wanted), ["#3"])

    def test_a_closes_argument_is_read_as_one_reference(self) -> None:
        self.assertEqual(closing.reference("PAT-12", REPO), "PAT-12")
        self.assertEqual(closing.reference("#12", REPO), "#12")
        self.assertEqual(closing.reference(f"https://github.com/{REPO.upper()}/issues/12", REPO), "#12")
        with self.assertRaises(ValueError):
            closing.reference("https://github.com/someone/else/issues/12", REPO)

    def test_only_a_rendered_bare_identifier_becomes_a_markdown_link(self) -> None:
        body = "Closes PAT-9\n`Closes PAT-9`\nCloses PAT-90, closes https://linear.app/y/issue/PAT-9\nfixes pat-9\nCloses [PAT-9](https://linear.app/y/issue/PAT-9)"
        link = f"[PAT-9]({LINEAR_URL})"
        self.assertEqual(
            closing.with_link(body, "PAT-9", LINEAR_URL),
            f"Closes {link}\n`Closes PAT-9`\nCloses PAT-90, closes https://linear.app/y/issue/PAT-9\nfixes {link}\nCloses [PAT-9](https://linear.app/y/issue/PAT-9)",
        )

    def test_a_bare_identifier_with_a_keyword_gets_the_literal_link_the_workflow_check_wants(self) -> None:
        self.assertEqual(closing.with_link("Fixes PAT-9", "PAT-9", LINEAR_URL), f"Fixes [PAT-9]({LINEAR_URL})")


class WorkflowCheckTest(unittest.TestCase):
    def test_the_body_step_passes_only_a_body_that_closes_an_issue_of_the_repository(self) -> None:
        cases = {
            "Closes #12": 0,
            "closes #12": 0,
            "Fixed: #12": 0,
            f"Resolves {REPO}#12": 0,
            f"Closes https://github.com/{REPO}/issues/12": 0,
            "Closes someone/else#12": 1,
            "Closes PAT-9": 1,
            "> Closes #12": 1,
            "~~Closes #12~~": 1,
            "```\nCloses #12\n```": 1,
            "<!-- Closes #12 -->": 1,
            "Part of #12": 1,
        }
        for body, expected in cases.items():
            with self.subTest(body):
                self.assertEqual(workflow_check(body)[0], expected)

    def test_an_empty_body_is_named(self) -> None:
        code, output = workflow_check("")
        self.assertEqual(code, 1)
        self.assertIn("PR body is empty", output)


class AgreementTest(unittest.TestCase):
    BODIES = {
        "```\nCloses #5\n```\nCloses https://linear.app/x/issue/PAT-9": False,
        "> Closes #5\nCloses https://linear.app/x/issue/PAT-9": False,
        "~~Closes #5~~\nCloses https://linear.app/x/issue/PAT-9": False,
        "<!-- Closes #5 -->\nCloses https://linear.app/x/issue/PAT-9": False,
        "`Closes #5`\nCloses https://linear.app/x/issue/PAT-9": False,
        "Closes [#5](https://github.com/example-org/example-repo/issues/5), closes https://linear.app/x/issue/PAT-9": True,
        "> Closes [#5](https://github.com/example-org/example-repo/issues/5)\nCloses https://linear.app/x/issue/PAT-9": False,
        "Closes #5, closes https://linear.app/x/issue/PAT-9": True,
        "FIXES example-org/example-repo#5\nresolves https://linear.app/x/issue/PAT-9/slug": True,
    }

    def link(self, body: str) -> tuple[str, str]:
        module = session_module()
        pull = {"user": {"login": APP}, "number": 1, "html_url": "https://x/1", "body": body}

        def gh(path, *options):
            if options:
                pull["body"] = options[-1].removeprefix("body=")
                return None
            return [pull]

        result = module.link_issues(REPO, "b", APP, "PAT-9", LINEAR_URL, f"https://github.com/{REPO}/issues/5", gh)
        return result["action"], pull["body"]

    def test_link_issues_the_workflow_and_closes_agree(self) -> None:
        for body, closes_5 in self.BODIES.items():
            with self.subTest(body):
                action, linked = self.link(body)
                self.assertEqual("#5" in closing.closes(body, REPO), closes_5)
                self.assertEqual(workflow_check(body)[0], 0 if closes_5 else 1)
                self.assertEqual(action, "unchanged" if closes_5 else "linked")
                self.assertEqual(linked, body if closes_5 else f"{body}\n\nCloses #5")
                self.assertEqual(workflow_check(linked)[0], 0)


if __name__ == "__main__":
    unittest.main()
