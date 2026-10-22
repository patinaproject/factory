from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
SCRIPT = SETUP / "scripts" / "agent-block"
TEMPLATE = SETUP / "references" / "agent-instructions.block.tmpl"
EXAMPLE = SETUP / "references" / "agent-instructions.values.example.json"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "agent-block.golden.txt"
loader = importlib.machinery.SourceFileLoader("agent_block", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
agent_block = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = agent_block
loader.exec_module(agent_block)

VALUES = {
    "record": 'the wiki page "Acme factory record",\nhttps://wiki.example/acme-factory',
    "app_slug": "acme-factory",
    "worker": "/home/acme/.hermes/profiles/worker",
    "gateway_home": "~/.hermes",
    "bin_dir": "/home/acme/.local/bin",
    "secret_source": "Infisical, environment\n  `prod`, folder `/factory`",
    "operator_only": "`OP_TOKEN`, the 1Password service account\n  that interactive shells reach through `op`.",
    "onepassword": True,
    "sst_app": "`infra` of the `acme/product` repository",
    "deploy_step": "Merging\n  its pull request into `main`",
    "ssh_account": "acme",
    "ssh_hostname": "ssh.acme.example",
    "ssh_allowlist": "the SSH allowlist in `infra` of the\n  `acme/product` repository",
    "ssh_users": "a@acme.example,b@acme.example",
    "factory_checkout": "~/acme/factory",
    "worker_checkouts": "`~/acme/product` and `~/acme/factory`",
}
NO_OPTIONAL = {**VALUES, "operator_only": "", "onepassword": False, "ssh_account": "", "ssh_hostname": "", "ssh_allowlist": "", "ssh_users": ""}


def render(values: dict) -> str:
    return agent_block.render(TEMPLATE.read_text(), values)


class RenderTest(unittest.TestCase):
    def test_matches_the_golden_render(self) -> None:
        self.assertEqual(render(VALUES), GOLDEN.read_text())

    def test_block_runs_from_begin_marker_to_end_marker(self) -> None:
        lines = render(VALUES).splitlines()
        self.assertEqual((lines[0], lines[-1]), (agent_block.BEGIN, agent_block.END))

    def test_leaves_no_placeholder_behind(self) -> None:
        for values in (VALUES, NO_OPTIONAL):
            self.assertNotIn("{{", render(values))

    def test_leaves_out_the_optional_lines(self) -> None:
        text = render(NO_OPTIONAL)
        for gone in ("OP_TOKEN", "`op`", "People SSH", "who may SSH", "--ssh-users", "authorized_principals"):
            self.assertNotIn(gone, text)
        self.assertIn("check-host-exposure`.\n", text)

    def test_keeps_the_optional_lines_when_set(self) -> None:
        text = render(VALUES)
        self.assertIn("check-host-exposure --ssh-users a@acme.example,b@acme.example`.", text)
        self.assertIn("/etc/ssh/authorized_principals/acme`", text)

    def test_missing_value_is_refused(self) -> None:
        values = dict(VALUES)
        del values["worker"]
        with self.assertRaisesRegex(ValueError, "worker"):
            render(values)

    def test_unknown_value_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "wroker"):
            render({**VALUES, "wroker": "x"})

    def test_example_values_carry_exactly_the_templates_keys(self) -> None:
        example = json.loads(EXAMPLE.read_text())
        self.assertEqual(set(example), agent_block.template_keys(TEMPLATE.read_text()))
        self.assertNotIn("{{", render(example))


class FileTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name)
        self.claude_md = self.config / "CLAUDE.md"
        self.values = self.config / "values.json"
        self.values.write_text(json.dumps(VALUES))
        self.block = render(VALUES)

    def failures(self) -> list[str]:
        return [m for ok, m in agent_block.check(self.config, TEMPLATE.read_text(), VALUES) if not ok]

    def test_passes_when_the_live_block_is_the_render(self) -> None:
        self.claude_md.write_text(f"mine above\n\n{self.block}\n@include\n")
        self.assertEqual(self.failures(), [])

    def test_reports_a_one_character_difference_with_the_line(self) -> None:
        self.claude_md.write_text(self.block.replace("Never use `git push`", "Never use `git pull`"))
        failures = self.failures()
        self.assertEqual(len(failures), 1)
        self.assertIn("-  Never use `git pull`.", failures[0])
        self.assertIn("+  Never use `git push`.", failures[0])

    def test_reports_a_rewrapped_line(self) -> None:
        self.claude_md.write_text(self.block.replace("record is the wiki page", "record is\nthe wiki page"))
        self.assertEqual(len(self.failures()), 1)

    def test_reports_missing_or_doubled_markers(self) -> None:
        self.claude_md.write_text("no block here\n")
        self.assertEqual(len(self.failures()), 1)
        self.claude_md.write_text(self.block + self.block)
        self.assertEqual(len(self.failures()), 1)

    def test_reports_a_missing_file(self) -> None:
        self.assertEqual(len(self.failures()), 1)

    def test_write_replaces_the_block_and_keeps_every_other_line(self) -> None:
        stale = self.block.replace("Never use `git push`", "Never use `git pull`")
        self.claude_md.write_text(f"above\n\n{stale}\n@~/.claude/pstack-models.md\n")
        agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
        self.assertEqual(self.claude_md.read_text(), f"above\n\n{self.block}\n@~/.claude/pstack-models.md\n")
        self.assertEqual(self.failures(), [])

    def test_write_appends_when_there_is_no_block(self) -> None:
        self.claude_md.write_text("mine\n")
        agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
        self.assertEqual(self.claude_md.read_text(), f"mine\n\n{self.block}")

    def test_write_appends_when_the_markers_appear_only_inline(self) -> None:
        notes = f"the block sits between `{agent_block.BEGIN}` and `{agent_block.END}`\n"
        self.claude_md.write_text(notes)
        agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
        self.assertEqual(self.claude_md.read_text(), f"{notes}\n{self.block}")
        self.assertEqual(self.failures(), [])

    def test_write_creates_the_file(self) -> None:
        agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
        self.assertEqual(self.claude_md.read_text(), self.block)

    def test_write_is_idempotent_and_backs_up_only_when_it_changes(self) -> None:
        self.claude_md.write_text(f"above\n\n{self.block}")
        self.assertEqual(agent_block.write(self.config, TEMPLATE.read_text(), VALUES), "unchanged")
        self.assertEqual(list(self.config.glob("CLAUDE.md.bak-*")), [])
        self.claude_md.write_text(f"above\n\n{self.block.replace('push-signed', 'x')}")
        agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
        self.assertEqual(len(list(self.config.glob("CLAUDE.md.bak-*"))), 1)

    def test_write_refuses_a_broken_marker_pair(self) -> None:
        self.claude_md.write_text(self.block + self.block)
        with self.assertRaises(ValueError):
            agent_block.write(self.config, TEMPLATE.read_text(), VALUES)


class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name)
        self.values = self.config / "values.json"
        self.values.write_text(json.dumps(VALUES))

    def run_main(self, *args: str) -> tuple[int, str]:
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = agent_block.main(["--config-dir", str(self.config), "--values", str(self.values), *args])
        return code, out.getvalue()

    def test_print_writes_the_render_to_stdout_and_touches_nothing(self) -> None:
        code, out = self.run_main("--print")
        self.assertEqual((code, out), (0, render(VALUES)))
        self.assertFalse((self.config / "CLAUDE.md").exists())

    def test_check_exits_1_on_a_difference_and_0_after_write(self) -> None:
        (self.config / "CLAUDE.md").write_text(render(VALUES).replace("push-signed", "push-unsigned"))
        self.assertEqual(self.run_main()[0], 1)
        self.assertEqual(self.run_main("--write")[0], 0)
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("PASS"))

    def test_explicit_check_equals_the_default(self) -> None:
        (self.config / "CLAUDE.md").write_text(render(VALUES).replace("push-signed", "push-unsigned"))
        self.assertEqual(self.run_main("--check"), self.run_main())
        self.assertEqual(self.run_main("--check")[0], 1)

    def test_check_refuses_to_combine_with_write(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main("--check", "--write")


if __name__ == "__main__":
    unittest.main()
