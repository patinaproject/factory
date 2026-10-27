from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location("work", Path(__file__).resolve().parent.parent / "__init__.py")
work = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(work)


def parse_frontmatter(content):
    if not content.startswith("---\n"):
        return {}, content
    head, _, body = content[4:].partition("\n---\n")
    return dict(line.split(": ", 1) for line in head.splitlines()), body


def extract_skill_description(frontmatter):
    return frontmatter.get("description", "")


skill_utils = types.ModuleType("agent.skill_utils")
skill_utils.parse_frontmatter = parse_frontmatter
skill_utils.extract_skill_description = extract_skill_description
agent_package = types.ModuleType("agent")
agent_package.skill_utils = skill_utils


class FakeCtx:
    def __init__(self, skills_dir=None, allow=None):
        self.settings = {}
        if skills_dir is not None:
            self.settings["skills_dir"] = str(skills_dir)
        if allow is not None:
            self.settings["allow"] = allow
        self.skills = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, path, description, frontmatter))


class WorkSkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = self.tmp / "skills"
        self.dir.mkdir()
        patcher = mock.patch.dict(sys.modules, {"agent": agent_package, "agent.skill_utils": skill_utils})
        patcher.start()
        self.addCleanup(patcher.stop)

    def skill(self, name, description="Does a thing", directory=None):
        folder = (directory or self.dir) / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody\n")

    def run_register(self, **kwargs):
        ctx = FakeCtx(**kwargs)
        work.register(ctx)
        return ctx

    def test_empty_allow_registers_every_skill_in_name_order(self):
        self.skill("zeta", "Last one")
        self.skill("alpha", "First one")

        ctx = self.run_register(skills_dir=self.dir, allow=[])

        self.assertEqual(
            ctx.skills,
            [
                ("alpha", self.dir / "alpha" / "SKILL.md", "First one", {"name": "alpha", "description": "First one"}),
                ("zeta", self.dir / "zeta" / "SKILL.md", "Last one", {"name": "zeta", "description": "Last one"}),
            ],
        )

    def test_allow_registers_only_the_named_skills(self):
        for name in ("a", "b", "c"):
            self.skill(name)

        ctx = self.run_register(skills_dir=self.dir, allow=["c", "a"])

        self.assertEqual([skill[0] for skill in ctx.skills], ["a", "c"])

    def test_allow_naming_a_missing_skill_raises_and_registers_nothing(self):
        self.skill("a")
        ctx = FakeCtx(skills_dir=self.dir, allow=["a", "nope", "gone"])

        with self.assertRaises(ValueError) as raised:
            work.register(ctx)

        self.assertIn("gone, nope", str(raised.exception))
        self.assertEqual(ctx.skills, [])

    def test_allow_that_is_not_a_list_raises_and_registers_nothing(self):
        self.skill("a")
        ctx = FakeCtx(skills_dir=self.dir, allow="a")

        with self.assertRaises(ValueError) as raised:
            work.register(ctx)

        self.assertEqual(
            str(raised.exception), "work: plugins.entries.work.settings.allow must be a list of skill names"
        )
        self.assertEqual(ctx.skills, [])

    def test_unset_skills_dir_registers_nothing_and_logs_one_line(self):
        with self.assertLogs("work", logging.WARNING) as logs:
            ctx = self.run_register()

        self.assertEqual(ctx.skills, [])
        self.assertEqual(len(logs.records), 1)
        self.assertIn("plugins.entries.work.settings.skills_dir is not set", logs.output[0])

    def test_missing_skills_dir_registers_nothing_and_logs_its_path(self):
        missing = self.tmp / "absent"

        with self.assertLogs("work", logging.WARNING) as logs:
            ctx = self.run_register(skills_dir=missing)

        self.assertEqual(ctx.skills, [])
        self.assertEqual(len(logs.records), 1)
        self.assertIn(f"{missing} is not a directory", logs.output[0])

    def test_invalid_directory_name_is_skipped_and_logged(self):
        self.skill("good")
        self.skill("bad.name")

        with self.assertLogs("work", logging.WARNING) as logs:
            ctx = self.run_register(skills_dir=self.dir)

        self.assertEqual([skill[0] for skill in ctx.skills], ["good"])
        self.assertIn(f"skipping {self.dir / 'bad.name'}", logs.output[0])

    def test_directory_without_skill_md_is_skipped(self):
        self.skill("good")
        (self.dir / "empty").mkdir()
        (self.dir / "notes.txt").write_text("x")

        ctx = self.run_register(skills_dir=self.dir)

        self.assertEqual([skill[0] for skill in ctx.skills], ["good"])

    def test_symlinked_skill_directory_registers_with_the_unresolved_path(self):
        elsewhere = self.tmp / "elsewhere"
        self.skill("linked", directory=elsewhere)
        (self.dir / "linked").symlink_to(elsewhere / "linked")

        ctx = self.run_register(skills_dir=self.dir)

        self.assertEqual(len(ctx.skills), 1)
        self.assertEqual(ctx.skills[0][1], self.dir / "linked" / "SKILL.md")
        self.assertNotEqual(ctx.skills[0][1], elsewhere / "linked" / "SKILL.md")


if __name__ == "__main__":
    unittest.main()
