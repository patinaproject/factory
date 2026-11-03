from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scheduled-command"
loader = importlib.machinery.SourceFileLoader("scheduled_command", str(SCRIPT))
scheduled_command = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
sys.modules[loader.name] = scheduled_command
loader.exec_module(scheduled_command)

REPOSITORY = "example-org/example-repo"
FAKE_HERMES = f"""#!{sys.executable}
import json, os, sys
assert "--raw" not in sys.argv
path = os.path.join(os.environ["HERMES_HOME"], "scheduled-commands.json")
if sys.argv[1:3] != ["config", "get"] or not os.path.exists(path):
    sys.exit("Config key not set: " + sys.argv[3])
print(open(path).read())
"""


def entry(commands: list, name: str = "example-job", **fields) -> dict:
    return {
        "name": name, "repository": REPOSITORY, "schedule": "0 9 * * 1", "commands": commands,
        "timeout_minutes": 5, **fields,
    }


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


class ScheduledCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.root = self.home / ".hermes"
        self.log = self.tmp / "log.txt"

        remotes = self.tmp / "remotes"
        self.bare = remotes / REPOSITORY.split("/")[0] / f"{REPOSITORY.split('/')[1]}.git"
        self.bare.mkdir(parents=True)
        git(self.bare, "init", "--quiet", "--bare", "-b", "main")
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "--quiet", str(self.bare), str(self.work))
        self.commit(".nvmrc", "v24.1\n")

        hermes = self.home / ".local" / "bin" / "hermes"
        hermes.parent.mkdir(parents=True)
        hermes.write_text(FAKE_HERMES)
        hermes.chmod(0o755)
        for version in ("v24.1.0", "v24.3.0", "v25.0.0", "v22.9.0"):
            node = self.home / ".nvm" / "versions" / "node" / version / "bin" / "node"
            node.parent.mkdir(parents=True)
            node.write_text(f"#!/bin/sh\necho {version}\n")
            node.chmod(0o755)
        infisical = self.home / ".config" / "infisical"
        infisical.mkdir(parents=True)
        (infisical / "config").write_text("export INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=client-id-fixture\n")
        names = self.root / "plugins" / "factory-secret-names" / "plugin.yaml"
        names.parent.mkdir(parents=True)
        names.write_text("optional_env:\n  - name: LINEAR_CLIENT_SECRET\n    description: x\n")

        self.env = {
            **os.environ, "HOME": str(self.tmp / "profile-home"), "HERMES_REAL_HOME": str(self.home),
            "HERMES_HOME": str(self.root),
            "INFISICAL_CLIENT_ID": "leak", "INFISICAL_TOKEN": "leak", "LINEAR_CLIENT_SECRET": "leak",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{remotes}/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
        }
        self.settings([entry([["true"]])])

    def commit(self, path: str, text: str) -> str:
        (self.work / path).write_text(text)
        git(self.work, "add", ".")
        git(self.work, "commit", "--quiet", "-m", path)
        git(self.work, "push", "--quiet", "origin", "HEAD:main")
        return git(self.work, "rev-parse", "HEAD")

    def settings(self, entries: list) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "scheduled-commands.json").write_text(json.dumps(entries))

    def run_script(self, name: str = "example-job") -> subprocess.CompletedProcess:
        installed = self.root / "scripts" / f"scheduled-{name}"
        installed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SCRIPT, installed)
        return subprocess.run([sys.executable, str(installed)], env=self.env, capture_output=True, text=True)

    def test_each_run_works_in_a_clean_clone_at_the_fetched_head(self) -> None:
        log = str(self.log)
        self.settings([entry([
            ["sh", "-c", 'echo first >> "$0"; touch stale.txt', log],
            ["sh", "-c", 'git rev-parse HEAD >> "$0"; ls -A | grep -v "^.git$" | tr "\\n" " " >> "$0"; echo >> "$0"', log],
        ])])
        first_head = git(self.work, "rev-parse", "HEAD")

        first = self.run_script()
        second_head = self.commit("new.txt", "new\n")
        second = self.run_script()

        self.assertEqual((first.returncode, first.stdout), (0, ""))
        self.assertEqual((second.returncode, second.stdout), (0, ""))
        self.assertEqual(
            self.log.read_text().splitlines(),
            ["first", first_head, ".nvmrc stale.txt ", "first", second_head, ".nvmrc new.txt stale.txt "],
        )

    def test_the_clone_is_reset_and_cleaned_before_the_second_run(self) -> None:
        log = str(self.log)
        self.settings([entry([["sh", "-c", 'ls -A | grep -v "^.git$" | tr "\\n" " " >> "$0"; echo >> "$0"; touch stale.txt', log]])])

        self.run_script()
        self.run_script()

        self.assertEqual(self.log.read_text().splitlines(), [".nvmrc ", ".nvmrc "])

    def test_commands_run_with_the_real_home_the_client_id_and_the_matching_node(self) -> None:
        out = self.tmp / "env.txt"
        script = (
            'printf "%s\\n" "$HOME" "$INFISICAL_UNIVERSAL_AUTH_CLIENT_ID" "${INFISICAL_CLIENT_ID-absent}" '
            '"${INFISICAL_TOKEN-absent}" "${LINEAR_CLIENT_SECRET-absent}" "$(node)" > "$0"'
        )
        self.settings([entry([["sh", "-c", script, str(out)]])])

        result = self.run_script()

        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(
            out.read_text().splitlines(), [str(self.home), "client-id-fixture", "absent", "absent", "absent", "v24.3.0"]
        )

    def test_client_id_file_is_read_when_there_is_no_config_file(self) -> None:
        infisical = self.home / ".config" / "infisical"
        (infisical / "config").unlink()
        (infisical / "client-id").write_text("plain-id\n")
        out = self.tmp / "id.txt"
        self.settings([entry([["sh", "-c", 'echo "$INFISICAL_UNIVERSAL_AUTH_CLIENT_ID" > "$0"', str(out)]])])

        self.run_script()

        self.assertEqual(out.read_text(), "plain-id\n")

    def test_a_failing_command_prints_its_last_forty_lines_and_stops_the_run(self) -> None:
        self.settings([entry([["sh", "-c", "seq 1 50; exit 3"], ["sh", "-c", f"touch {self.log}"]])])

        result = self.run_script()

        lines = result.stdout.splitlines()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(lines[0], "example-job: sh -c 'seq 1 50; exit 3' exited 3")
        self.assertEqual(lines[1:], [str(n) for n in range(11, 51)])
        self.assertFalse(self.log.exists())

    def test_a_missing_command_is_a_failure_with_exit_127(self) -> None:
        self.settings([entry([["no-such-command-here", "--flag"]])])

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.splitlines()[0], "example-job: no-such-command-here --flag exited 127")

    def test_a_timeout_ends_the_whole_process_tree(self) -> None:
        pidfile = self.tmp / "grandchild.pid"
        script = 'sleep 300 & echo $! > "$0"; wait'
        self.settings([entry([["sh", "-c", script, str(pidfile)]], timeout_minutes=1)])
        out = io.StringIO()

        with mock.patch.dict(os.environ, self.env), mock.patch.object(scheduled_command, "SECONDS_PER_MINUTE", 1):
            with contextlib.redirect_stdout(out):
                code = scheduled_command.main(str(self.root / "scripts" / "scheduled-example-job"))

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().splitlines()[0], f"example-job: sh -c {scheduled_command.shlex.quote(script)} {pidfile} timeout after 1 minutes")
        grandchild = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild, 0)

    def test_missing_identity_names_where_it_looked(self) -> None:
        (self.home / ".config" / "infisical" / "config").unlink()

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stdout,
            f"example-job: no INFISICAL_UNIVERSAL_AUTH_CLIENT_ID in {self.home}/.config/infisical/config "
            f"or {self.home}/.config/infisical/client-id\n",
        )

    def test_missing_node_names_the_major_version(self) -> None:
        shutil.rmtree(self.home / ".nvm" / "versions" / "node" / "v24.1.0")
        shutil.rmtree(self.home / ".nvm" / "versions" / "node" / "v24.3.0")

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, f"example-job: no Node v24 under {self.home}/.nvm/versions/node\n")

    def test_missing_nvmrc_names_the_file(self) -> None:
        (self.work / ".nvmrc").unlink()
        git(self.work, "commit", "--quiet", "-am", "drop")
        git(self.work, "push", "--quiet", "origin", "HEAD:main")

        result = self.run_script()

        self.assertEqual(result.stdout, f"example-job: {self.root}/scheduled/example-org/example-repo/.nvmrc is missing, so the Node version is unknown\n")

    def test_the_entry_name_comes_from_the_file_name(self) -> None:
        self.settings([entry([["true"]]), entry([["false"]], name="other-job")])

        other = self.run_script("other-job")

        self.assertEqual((other.returncode, other.stdout.splitlines()[0]), (1, "other-job: false exited 1"))

    def test_an_unknown_name_exits_one_and_prints_it(self) -> None:
        result = self.run_script("nope")

        self.assertEqual((result.returncode, result.stdout), (1, "nope: no scheduled_commands entry has this name\n"))

    def test_an_unset_key_has_no_entries(self) -> None:
        (self.root / "scheduled-commands.json").unlink()

        result = self.run_script()

        self.assertEqual((result.returncode, result.stdout), (1, "example-job: no scheduled_commands entry has this name\n"))

    def test_a_repeated_name_in_the_settings_stops_the_run(self) -> None:
        self.settings([entry([["touch", str(self.log)]]), entry([["true"]])])

        result = self.run_script()

        self.assertEqual(
            (result.returncode, result.stdout, self.log.exists()),
            (1, "example-job: scheduled_commands name example-job is used more than once\n", False),
        )


class ParseTest(unittest.TestCase):
    def test_a_valid_entry_parses_to_a_frozen_value(self) -> None:
        parsed = scheduled_command.parse_entry(entry([["pnpm", "install", "--frozen-lockfile"], ["pnpm", "run", "example"]]))

        self.assertEqual(parsed, scheduled_command.ScheduledCommand(
            "example-job", REPOSITORY, "0 9 * * 1", (("pnpm", "install", "--frozen-lockfile"), ("pnpm", "run", "example")), 5,
        ))

    def test_invalid_fields_are_refused_by_name(self) -> None:
        cases = {
            "Bad_Name": entry([["x"]], name="Bad_Name"),
            "repository": entry([["x"]], repository="no-slash"),
            "schedule": entry([["x"]], schedule="0 9 * *"),
            "commands": entry([]),
            "argv": entry([["x", 1]]),
            "timeout": entry([["x"]], timeout_minutes=0),
            "bool timeout": entry([["x"]], timeout_minutes=True),
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(scheduled_command.SettingsError):
                scheduled_command.parse_entry(raw)

    def test_names_must_be_unique(self) -> None:
        with self.assertRaisesRegex(scheduled_command.SettingsError, "example-job is used more than once"):
            scheduled_command.parse_entries([entry([["x"]]), entry([["y"]])])

    def test_an_unset_key_is_no_entries(self) -> None:
        self.assertEqual(scheduled_command.parse_entries(None), ())


if __name__ == "__main__":
    unittest.main()
