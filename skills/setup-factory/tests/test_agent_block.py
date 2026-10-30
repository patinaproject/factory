from __future__ import annotations

import importlib.machinery
import importlib.util
import itertools
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
    "default_team": "ACME",
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

    def near_miss_blocks(self) -> dict[str, str]:
        stale = self.block.replace("Never use `git push`", "Never use `git pull`")
        b, e = agent_block.BEGIN, agent_block.END
        return {
            "indented": stale.replace(b, "  " + b).replace(e, "  " + e),
            "trailing spaces": stale.replace(b, b + " ").replace(e, e + " "),
            "zero width space inside": stale.replace(b, b.replace("begin", "\u200bbegin")),
            "spaces removed": stale.replace(b, b.replace(" ", "")),
        }

    def line_ending_blocks(self) -> dict[str, str]:
        stale = self.block.replace("Never use `git push`", "Never use `git pull`")
        text = f"notes\n\n{stale}\n@~/.claude/pstack-models.md\n"
        return {"crlf": text.replace("\n", "\r\n"), "cr only": text.replace("\n", "\r")}

    def test_write_refuses_a_real_block_whose_markers_are_not_exact(self) -> None:
        for name, text in self.near_miss_blocks().items():
            with self.subTest(name):
                self.claude_md.write_bytes(f"notes\n{text}".encode())
                before = self.claude_md.read_bytes()
                with self.assertRaises(ValueError):
                    agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
                self.assertEqual(self.claude_md.read_bytes(), before)

    def test_check_fails_a_real_block_whose_markers_are_not_exact(self) -> None:
        for name, text in self.near_miss_blocks().items():
            with self.subTest(name):
                self.claude_md.write_bytes(f"notes\n{text}".encode())
                self.assertEqual(len(self.failures()), 1)

    def test_refusal_names_the_line_and_its_stray_characters(self) -> None:
        stray = {
            "indented": "stray characters U+0020 SPACE;",
            "trailing spaces": "stray characters U+0020 SPACE;",
            "zero width space inside": "stray characters U+200B ZERO WIDTH SPACE;",
            "spaces removed": "missing characters;",
        }
        for name, text in self.near_miss_blocks().items():
            with self.subTest(name):
                self.claude_md.write_bytes(f"notes\n{text}".encode())
                with self.assertRaises(ValueError) as raised:
                    agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
                self.assertIn(f"line 2 holds a marker with {stray[name]}", str(raised.exception))
                self.assertNotIn("CARRIAGE RETURN", str(raised.exception))

    def test_refusal_names_invisible_characters_around_a_marker(self) -> None:
        self.claude_md.write_text(f"notes\n\ufeff{self.block.replace(agent_block.END, agent_block.END + chr(0x2060))}")
        with self.assertRaises(ValueError) as raised:
            agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
        self.assertIn("line 2 holds a marker with stray characters U+FEFF ZERO WIDTH NO-BREAK SPACE;", str(raised.exception))

    def test_write_never_leaves_a_stale_or_second_block_whatever_pads_the_markers(self) -> None:
        pads = ["".join(p) for n in range(3) for p in itertools.product(" \t\u00a0\u200b\ufeff\u2060", repeat=n)]
        cases = [tuple(pad if i == slot else "" for i in range(4)) for slot in range(4) for pad in pads]
        cases += [(pad,) * 4 for pad in pads]
        stale = self.block.replace("Never use `git push`", "Never use `git pull`")
        b, e = agent_block.BEGIN, agent_block.END
        for pad in cases:
            before_begin, after_begin, before_end, after_end = pad
            padded = stale.replace(b, before_begin + b + after_begin).replace(e, before_end + e + after_end)
            text = f"notes\n\n{padded}\n@~/.claude/pstack-models.md\n"
            for ending in ("\n", "\r\n", "\r"):
                with self.subTest(pads=pad, ending=ending):
                    self.claude_md.write_bytes(text.replace("\n", ending).encode())
                    before = self.claude_md.read_bytes()
                    if any(pad):
                        with self.assertRaises(ValueError):
                            agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
                        self.assertEqual(self.claude_md.read_bytes(), before)
                        continue
                    agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
                    after = self.claude_md.read_bytes().decode()
                    self.assertEqual(after, f"notes\n\n{self.block}\n@~/.claude/pstack-models.md\n")
                    self.assertEqual(self.failures(), [])

    def test_write_replaces_the_one_block_in_a_crlf_or_cr_only_file_and_saves_lf(self) -> None:
        for name, text in self.line_ending_blocks().items():
            with self.subTest(name):
                for backup in self.config.glob("CLAUDE.md.bak-*"):
                    backup.unlink()
                self.claude_md.write_bytes(text.encode())
                agent_block.write(self.config, TEMPLATE.read_text(), VALUES)
                self.assertEqual(
                    self.claude_md.read_bytes().decode(),
                    f"notes\n\n{self.block}\n@~/.claude/pstack-models.md\n",
                )
                self.assertEqual(self.claude_md.read_bytes().decode().count(agent_block.BEGIN), 1)
                self.assertEqual(self.failures(), [])
                backups = list(self.config.glob("CLAUDE.md.bak-*"))
                self.assertEqual([b.read_bytes() for b in backups], [text.encode()])

    def test_check_reads_a_crlf_or_cr_only_block_as_its_lines(self) -> None:
        for name, text in self.line_ending_blocks().items():
            with self.subTest(name):
                self.claude_md.write_bytes(text.replace("git pull", "git push").encode())
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


class SettingsTest(unittest.TestCase):
    RULE = "Bash(python3 ~/.hermes/plugins/linear-agent-session/cli.py session *)"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name)
        self.settings = self.config / "settings.json"

    def saved(self) -> dict:
        return json.loads(self.settings.read_text())

    def test_check_fails_a_missing_file_and_a_missing_rule_naming_the_rule(self) -> None:
        self.assertFalse(agent_block.check_settings(self.config, VALUES)[0][0])
        self.settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        self.assertEqual(
            agent_block.check_settings(self.config, VALUES),
            [(False, f"{self.settings}: permissions.allow lacks {self.RULE}")],
        )

    def test_write_creates_the_file_and_check_then_passes(self) -> None:
        self.assertTrue(agent_block.write_settings(self.config, VALUES).startswith("wrote"))
        self.assertEqual(self.saved(), {"permissions": {"allow": [self.RULE]}})
        self.assertEqual(self.settings.read_text(), json.dumps(self.saved(), indent=2) + "\n")
        self.assertTrue(all(ok for ok, _ in agent_block.check_settings(self.config, VALUES)))

    def test_write_keeps_every_other_key_and_entry_and_backs_up(self) -> None:
        before = {"model": "opus", "permissions": {"allow": ["Bash(ls)"], "deny": ["Read(.env)"]}, "env": {"A": "1"}}
        self.settings.write_text(json.dumps(before))
        agent_block.write_settings(self.config, VALUES)
        self.assertEqual(
            self.saved(),
            {"model": "opus", "permissions": {"allow": ["Bash(ls)", self.RULE], "deny": ["Read(.env)"]}, "env": {"A": "1"}},
        )
        backups = list(self.config.glob("settings.json.bak-*"))
        self.assertEqual([json.loads(b.read_text()) for b in backups], [before])

    def test_write_is_idempotent_and_makes_no_second_backup(self) -> None:
        agent_block.write_settings(self.config, VALUES)
        self.assertEqual(agent_block.write_settings(self.config, VALUES), "unchanged")
        self.assertEqual(self.saved(), {"permissions": {"allow": [self.RULE]}})
        self.assertEqual(list(self.config.glob("settings.json.bak-*")), [])

    def test_a_settings_file_that_is_not_a_json_object_is_refused_and_left_alone(self) -> None:
        for text in ("[]", "{not json", '{"permissions": {"allow": "Bash(ls)"}}'):
            with self.subTest(text):
                self.settings.write_text(text)
                with self.assertRaises(ValueError):
                    agent_block.write_settings(self.config, VALUES)
                with self.assertRaises(ValueError):
                    agent_block.check_settings(self.config, VALUES)
                self.assertEqual(self.settings.read_text(), text)


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

    def test_command_checks_and_writes_the_allow_rules_too(self) -> None:
        (self.config / "CLAUDE.md").write_text(render(VALUES))
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out.splitlines()[-1])
        self.assertEqual(self.run_main("--write")[0], 0)
        code, out = self.run_main()
        self.assertEqual((code, [line[:4] for line in out.splitlines()]), (0, ["PASS", "PASS"]))

    def test_command_fails_on_an_invalid_settings_file(self) -> None:
        (self.config / "settings.json").write_text("[]")
        code, out = self.run_main()
        self.assertEqual((code, out.startswith("FAIL")), (1, True))

    def test_check_refuses_to_combine_with_write(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_main("--check", "--write")


if __name__ == "__main__":
    unittest.main()
