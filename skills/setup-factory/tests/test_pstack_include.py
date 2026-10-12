from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pstack-include"
loader = importlib.machinery.SourceFileLoader("pstack_include", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
pstack = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = pstack
loader.exec_module(pstack)

BLOCK = "<!-- factory:begin -->\n## Factory machine\n\nRules.\n<!-- factory:end -->\n"
SHEET = "# pstack model configuration\n\nfeature, refactoring: sonnet\ndefault effort: medium\nsession hook: on\n"
INCLUDE = "@~/.claude/pstack-models.md"


class PstackIncludeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.config = self.home / ".claude"
        self.config.mkdir()
        self.claude_md = self.config / "CLAUDE.md"
        (self.config / "pstack-models.md").write_text(SHEET)

    def failures(self) -> list[str]:
        return [message for ok, message in pstack.check(self.config, self.home) if not ok]

    def test_passes_with_one_include_outside_the_block(self) -> None:
        self.claude_md.write_text(f"{BLOCK}\n{INCLUDE}\n")
        self.assertEqual(self.failures(), [])

    def test_accepts_the_absolute_form(self) -> None:
        self.claude_md.write_text(f"@{self.config}/pstack-models.md\n\n{BLOCK}")
        self.assertEqual(self.failures(), [])

    def test_write_adds_an_absent_include_after_a_backup(self) -> None:
        original = f"intro\n\n{BLOCK}"
        self.claude_md.write_text(original)
        self.assertIn("added the include", pstack.write(self.config, self.home))
        self.assertEqual(self.claude_md.read_text(), f"{original}\n{INCLUDE}\n")
        backups = list(self.config.glob("CLAUDE.md.bak-*"))
        self.assertEqual([b.read_text() for b in backups], [original])
        self.assertEqual(self.failures(), [])

    def test_write_leaves_a_present_include_unchanged(self) -> None:
        text = f"{BLOCK}\n{INCLUDE}\n"
        self.claude_md.write_text(text)
        self.assertEqual(pstack.write(self.config, self.home), "unchanged")
        self.assertEqual(self.claude_md.read_text(), text)
        self.assertEqual(list(self.config.glob("CLAUDE.md.bak-*")), [])

    def test_write_twice_adds_the_include_once(self) -> None:
        self.claude_md.write_text(BLOCK)
        pstack.write(self.config, self.home)
        pstack.write(self.config, self.home)
        self.assertEqual(self.claude_md.read_text().count(INCLUDE), 1)

    def test_an_include_only_inside_the_block_does_not_count_and_write_adds_one_outside(self) -> None:
        self.claude_md.write_text(BLOCK.replace("Rules.", f"Rules.\n{INCLUDE}"))
        self.assertTrue(any("0 times outside" in m for m in self.failures()))
        pstack.write(self.config, self.home)
        self.assertTrue(self.claude_md.read_text().endswith(f"{pstack.END}\n\n{INCLUDE}\n"))
        self.assertEqual(self.failures(), [])

    def test_duplicate_include_fails_the_check_and_the_write(self) -> None:
        duplicated = f"{INCLUDE}\n{BLOCK}{INCLUDE}\n"
        self.claude_md.write_text(duplicated)
        self.assertTrue(any("2 times outside" in m for m in self.failures()))
        with self.assertRaises(ValueError):
            pstack.write(self.config, self.home)
        self.assertEqual(self.claude_md.read_text(), duplicated)

    def test_missing_include_fails_the_check(self) -> None:
        self.claude_md.write_text(BLOCK)
        self.assertTrue(any("0 times outside" in m for m in self.failures()))

    def test_broken_marker_pair_fails_and_blocks_the_write(self) -> None:
        for broken in (BLOCK.replace(pstack.END, ""), BLOCK + BLOCK, f"{pstack.END}\n{pstack.BEGIN}\n"):
            with self.subTest(broken=broken):
                self.claude_md.write_text(broken)
                self.assertTrue(any("CLAUDE.md:" in m for m in self.failures()))
                with self.assertRaises(ValueError):
                    pstack.write(self.config, self.home)
                self.assertEqual(self.claude_md.read_text(), broken)

    def test_missing_claude_md_fails(self) -> None:
        self.assertTrue(any("CLAUDE.md is missing" in m for m in self.failures()))
        with self.assertRaises(ValueError):
            pstack.write(self.config, self.home)

    def test_missing_sheet_fails(self) -> None:
        (self.config / "pstack-models.md").unlink()
        self.claude_md.write_text(f"{BLOCK}\n{INCLUDE}\n")
        self.assertTrue(any("pstack-models.md is missing" in m for m in self.failures()))

    def test_sheet_naming_an_unusable_model_fails(self) -> None:
        for line in ("bug-fix: claude-opus-5", "bug-fix: <model>", "arena runners: opus, gpt-4o"):
            with self.subTest(line=line):
                (self.config / "pstack-models.md").write_text(SHEET + line + "\n")
                self.claude_md.write_text(f"{BLOCK}\n{INCLUDE}\n")
                self.assertTrue(any(line.split(":")[0] in m for m in self.failures()))

    def test_sheet_with_aliases_and_effort_passes(self) -> None:
        (self.config / "pstack-models.md").write_text(SHEET + "bug-fix: opus @xhigh\narena runners: opus, auto, inherit-parent\n")
        self.claude_md.write_text(f"{BLOCK}\n{INCLUDE}\n")
        self.assertEqual(self.failures(), [])

    def test_config_outside_home_uses_the_absolute_form(self) -> None:
        other = self.home / "elsewhere"
        other.mkdir()
        (other / "CLAUDE.md").write_text(BLOCK)
        pstack.write(other, Path("/nonexistent-home"))
        self.assertTrue((other / "CLAUDE.md").read_text().endswith(f"\n@{other}/pstack-models.md\n"))


if __name__ == "__main__":
    unittest.main()
