from __future__ import annotations

import unittest

from tests import load_plugin

plugin = load_plugin()

from linear_agent_session import kanban_guard  # noqa: E402


def decide(tool, args, platform=""):
    return kanban_guard.make_hook(lambda: platform)(tool_name=tool, args=args, task_id="t_1")


class CommentTest(unittest.TestCase):
    def test_a_pull_request_url_is_blocked_and_the_short_form_named(self):
        result = decide("kanban_comment", {"body": "see https://github.com/acme/app/pull/12 now"})
        self.assertEqual(result["action"], "block")
        self.assertIn("- https://github.com/acme/app/pull/12 -> acme/app#12", result["message"])

    def test_the_match_ignores_case_and_scheme(self):
        result = decide("kanban_comment", {"body": "HTTP://GitHub.com/a/b/pull/3"})
        self.assertIn("HTTP://GitHub.com/a/b/pull/3 -> a/b#3", result["message"])

    def test_every_url_is_listed(self):
        message = decide(
            "kanban_comment",
            {"body": "https://github.com/a/b/pull/1 and https://github.com/c/d/pull/2/files"},
        )["message"]
        self.assertIn("-> a/b#1", message)
        self.assertIn("-> c/d#2", message)

    def test_short_forms_and_issue_urls_pass(self):
        for body in ("acme/app#12, review 345", "https://github.com/acme/app/issues/12"):
            self.assertIsNone(decide("kanban_comment", {"body": body}))

    def test_a_comment_is_checked_on_every_platform(self):
        body = {"body": "https://github.com/a/b/pull/1"}
        self.assertEqual(decide("kanban_comment", body, "kanban")["action"], "block")


class CreateTest(unittest.TestCase):
    def test_a_webhook_turn_may_not_use_the_tool(self):
        result = decide("kanban_create", {"title": "x"}, "webhook")
        self.assertEqual(result["action"], "block")
        self.assertIn("hermes kanban create", result["message"])

    def test_other_platforms_may(self):
        for platform in ("", "kanban", "slack"):
            self.assertIsNone(decide("kanban_create", {"title": "x"}, platform))


class OtherCallsTest(unittest.TestCase):
    def test_other_tools_pass(self):
        self.assertIsNone(decide("terminal", {"body": "https://github.com/a/b/pull/1"}, "webhook"))

    def test_odd_args_do_not_raise(self):
        for args in (None, "text", [], {}, {"body": None}, {"body": 5}):
            self.assertIsNone(decide("kanban_comment", args))


if __name__ == "__main__":
    unittest.main()
