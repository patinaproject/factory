from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import allowlist

RESOLVED = (
    "read_file", "write_file", "patch", "search_files",
    "kanban_attach", "kanban_attach_url", "kanban_attachments", "kanban_block", "kanban_comment", "kanban_complete",
    "kanban_create", "kanban_heartbeat", "kanban_link", "kanban_list", "kanban_request_changes",
    "kanban_request_review", "kanban_show", "kanban_unblock",
    "email_issue_search", "email_record_outcome",
    "terminal", "process_manage",
)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "t_1").mkdir()
        (self.root / "t_1" / "email.json").write_text("{}")
        self.hook = allowlist.make_hook(lambda: "t_1", lambda card: self.root / card)

    def tearDown(self):
        self.directory.cleanup()

    def allowed(self, tool, args):
        return self.hook(tool_name=tool, args=args) is None

    def args_for(self, tool):
        if tool in allowlist.READ_TOOLS:
            return {"path": str(self.root / "t_1" / "email.json")}
        return {}

    def test_of_the_resolved_tools_only_the_mail_allowlist_runs(self):
        allowed = sorted(tool for tool in RESOLVED if self.allowed(tool, self.args_for(tool)))

        self.assertEqual(
            allowed,
            ["email_issue_search", "email_record_outcome", "kanban_block", "kanban_heartbeat", "kanban_show", "read_file", "search_files"],
        )

    def test_each_named_write_or_shell_tool_is_blocked_with_a_reason(self):
        for tool in ("write_file", "patch", "kanban_create", "kanban_comment", "kanban_complete", "terminal", "process_manage"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    self.hook(tool_name=tool, args={"path": str(self.root / "t_1" / "x")}),
                    {"action": "block", "message": f"{tool} is not available to a mail run. It can read its email's files and record one outcome."},
                )

    def test_reads_stay_inside_the_cards_directory(self):
        for path in (
            "/etc/passwd",
            str(self.root / "t_2" / "email.json"),
            str(self.root / "t_1" / ".." / "t_2"),
            "email.json",
            "~/.hermes/.env",
            None,
        ):
            with self.subTest(path=path):
                self.assertFalse(self.allowed("read_file", {"path": path}))
                self.assertFalse(self.allowed("search_files", {"path": path}))

    def test_a_symlink_out_of_the_directory_is_refused(self):
        (self.root / "t_1" / "out").symlink_to("/etc")
        self.assertFalse(self.allowed("read_file", {"path": str(self.root / "t_1" / "out" / "hosts")}))

    def test_card_tools_act_only_on_the_cards_own_id(self):
        self.assertTrue(self.allowed("kanban_show", {}))
        self.assertTrue(self.allowed("kanban_show", {"task_id": "t_1"}))
        self.assertFalse(self.allowed("kanban_show", {"task_id": "t_9"}))
        self.assertFalse(self.allowed("kanban_block", {"task_id": "t_9", "reason": "x"}))

    def test_outside_a_card_every_tool_is_refused(self):
        hook = allowlist.make_hook(lambda: None, lambda card: self.root / card)
        self.assertEqual(hook(tool_name="email_issue_search", args={"query": "x"})["action"], "block")


if __name__ == "__main__":
    unittest.main()
