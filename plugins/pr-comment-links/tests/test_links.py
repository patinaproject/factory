from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "pr_comment_links", Path(__file__).resolve().parent.parent / "__init__.py"
)
links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(links)

# The pattern Hermes' respawn guard applies to recent card comments
# (hermes_cli/kanban_db_dispatch.py, _RESPAWN_GUARD_PR_URL_RE).
GUARD = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)

URL = "https://github.com/example-org/example-repo/pull/12"


def rewrite(body: str, tool_name: str = "kanban_comment") -> dict | None:
    return links.on_pre_tool_call(tool_name=tool_name, args={"task_id": "t_1", "body": body})


class NamePullRequests(unittest.TestCase):
    def test_names_a_pull_request_by_repository_and_number(self):
        self.assertEqual(
            links.name_pull_requests(f"Opened {URL} for review."),
            "Opened example-org/example-repo#12 for review.",
        )

    def test_keeps_a_comment_anchor_as_text(self):
        self.assertEqual(
            links.name_pull_requests(f"See {URL}#issuecomment-6112392912."),
            "See example-org/example-repo#12 (issuecomment-6112392912).",
        )

    def test_keeps_a_path_and_anchor_as_text(self):
        self.assertEqual(
            links.name_pull_requests(f"{URL}/files#diff-1a2b"),
            "example-org/example-repo#12 (files#diff-1a2b)",
        )

    def test_rewrites_every_form_the_guard_matches(self):
        bodies = [
            f"({URL})",
            "HTTP://GitHub.com/Example-Org/Example-Repo/pull/3",
            f"[the pull request]({URL})",
            f"first {URL}, then https://github.com/other/repo/pull/4",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertIsNone(GUARD.search(links.name_pull_requests(body)))

    def test_leaves_other_github_pages_alone(self):
        body = (
            "https://github.com/example-org/example-repo/issues/12 and "
            "https://github.com/example-org/example-repo/actions/runs/5"
        )
        self.assertEqual(links.name_pull_requests(body), body)


class PreToolCallHook(unittest.TestCase):
    def test_modifies_only_the_body_of_a_comment_with_a_pull_request_url(self):
        self.assertEqual(
            rewrite(f"Blocked on {URL}"),
            {"action": "modify", "args": {"body": "Blocked on example-org/example-repo#12"}},
        )

    def test_leaves_a_comment_without_a_pull_request_url_alone(self):
        self.assertIsNone(rewrite("Linear reply: re-run the gate"))

    def test_ignores_other_tools(self):
        self.assertIsNone(rewrite(URL, tool_name="kanban_complete"))

    def test_ignores_arguments_without_a_string_body(self):
        self.assertIsNone(links.on_pre_tool_call(tool_name="kanban_comment", args=None))
        self.assertIsNone(links.on_pre_tool_call(tool_name="kanban_comment", args={"task_id": "t_1"}))

    def test_registers_the_pre_tool_call_hook(self):
        hooks = []

        class Ctx:
            def register_hook(self, name, callback):
                hooks.append((name, callback))

        links.register(Ctx())
        self.assertEqual(hooks, [("pre_tool_call", links.on_pre_tool_call)])


if __name__ == "__main__":
    unittest.main()
