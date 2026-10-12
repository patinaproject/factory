from __future__ import annotations

import contextlib
import importlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
memory = importlib.import_module("memory")

from tests.test_render import SETTINGS  # noqa: E402

GH_DIR = "/srv/hermes/profiles/claude-worker/gh"
AGENT_ENTRY = "Pull requests by example-app[bot] are the agent's own work."


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps(SETTINGS))
        self.file = self.tmp / "memories" / "MEMORY.md"
        self.entries = memory.factory_entries("example-app[bot]", GH_DIR)

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = memory.main([str(self.file), "--gh-config-dir", GH_DIR, "--settings-json", str(self.settings), *extra])
        return code, out.getvalue().splitlines(), err.getvalue()

    def stored(self):
        return self.file.read_text().split("\n§\n")

    def test_template_renders_labeled_entries_within_the_limit(self):
        self.assertGreater(len(self.entries), 0)
        self.assertEqual(len({memory.label(entry) for entry in self.entries}), len(self.entries))
        joined = memory.SEPARATOR.join(self.entries)
        self.assertNotIn("<<", joined)
        self.assertIn(f"GH_CONFIG_DIR={GH_DIR}", joined)
        self.assertIn("example-app[bot]", joined)
        self.assertLessEqual(len(joined), memory.CHAR_LIMIT // 2)

    def test_install_creates_a_private_file(self):
        self.assertEqual(self.run_main(), (0, [], ""))

        self.assertEqual(self.stored(), self.entries)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_install_keeps_agent_entries_and_replaces_stale_factory_entries(self):
        self.file.parent.mkdir()
        stale = f"{memory.label(self.entries[1])}: an older version of this entry."
        self.file.write_text(f"{AGENT_ENTRY}\n§\n{stale}\n")

        self.assertEqual(self.run_main()[0], 0)

        self.assertEqual(self.stored(), [AGENT_ENTRY, self.entries[1], self.entries[0], *self.entries[2:]])
        before = self.file.read_text()
        self.assertEqual(self.run_main(), (0, [], ""))
        self.assertEqual(self.file.read_text(), before)
        self.assertEqual(self.run_main("--check"), (0, [], ""))

    def test_check_names_missing_and_stale_entries_without_writing(self):
        self.file.parent.mkdir()
        stale = f"{memory.label(self.entries[0])}: an older version of this entry."
        self.file.write_text(stale)

        code, lines, _ = self.run_main("--check")

        self.assertEqual(code, 1)
        self.assertEqual(lines, [
            f"memory entry {memory.label(self.entries[0])}: differs from the template",
            *(f"memory entry {memory.label(entry)}: missing" for entry in self.entries[1:]),
        ])
        self.assertEqual(self.file.read_text(), stale)

    def test_install_refuses_to_exceed_the_limit(self):
        self.file.parent.mkdir()
        crowded = "Agent note: " + "x" * memory.CHAR_LIMIT
        self.file.write_text(crowded)

        code, _, err = self.run_main()

        self.assertEqual(code, 1)
        self.assertIn("over Hermes' limit", err)
        self.assertEqual(self.file.read_text(), crowded)


if __name__ == "__main__":
    unittest.main()
