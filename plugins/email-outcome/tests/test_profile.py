from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONFIG = (REPO / "profiles" / "mail-worker" / "config.yaml").read_text()
PLUGIN = (REPO / "plugins" / "email-outcome" / "plugin.yaml").read_text()


class MailProfileTest(unittest.TestCase):
    def test_the_mail_profile_loads_file_and_email_and_turns_terminal_off(self):
        self.assertEqual(re.search(r"(?m)^  cli: \[(.*)\]$", CONFIG).group(1), "file, email")
        self.assertEqual(re.search(r"(?m)^  disabled_toolsets: \[(.*)\]$", CONFIG).group(1), "terminal")

    def test_the_mail_profile_enables_only_the_email_outcome_plugin(self):
        self.assertEqual(re.search(r"(?m)^  enabled: \[(.*)\]$", CONFIG).group(1), "email-outcome")

    def test_the_plugin_declares_the_hook_and_the_tools_it_registers(self):
        import outcome

        self.assertRegex(PLUGIN, r"(?m)^provides_hooks: \[pre_tool_call\]$")
        self.assertEqual(
            re.findall(r"(?m)^  - (email_[a-z_]+)$", PLUGIN), [outcome.SEARCH_SCHEMA["name"], outcome.SCHEMA["name"]]
        )


if __name__ == "__main__":
    unittest.main()
