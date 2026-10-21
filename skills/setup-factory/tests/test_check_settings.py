from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-settings"
SECRET = "lin_wh_s3cr3t-value"

# Stands in for `hermes [-p <profile>] config get <key> --json --raw`: prints the
# profile's settings from $FAKE_SETTINGS_DIR and, like --raw, expands ${NAME}
# references from the environment.
FAKE_HERMES = """\
import os, re, sys
args = sys.argv[1:]
profile = args[1] if args[:1] == ["-p"] else "default"
path = os.path.join(os.environ["FAKE_SETTINGS_DIR"], profile + ".json")
if not os.path.exists(path):
    print("no such key " + os.environ.get("LEAK", ""), file=sys.stderr)
    sys.exit(1)
text = open(path).read()
if "--raw" in args:
    text = re.sub(r"\\$\\{([A-Za-z_]+)\\}", lambda m: os.environ[m.group(1)], text)
print(text)
"""

SETTINGS = {
    "app_user_id": "app-user",
    "webhook_route": "linear",
    "github": {"login": "factory[bot]", "app_id": "1"},
    "claude_session": {"model": "", "tier_models": {}},
}


class CheckSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        hermes = bin_dir / "hermes"
        hermes.write_text(f"#!{sys.executable}\n{FAKE_HERMES}")
        hermes.chmod(0o755)
        self.homes = self.root / "homes"
        self.homes.mkdir()
        self.expected = self.root / "settings.json"
        self.expected.write_text(json.dumps(SETTINGS))
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_SETTINGS_DIR": str(self.homes),
            "SECRET_VALUE": SECRET,
            "LEAK": SECRET,
        }

    def install(self, home: str, settings) -> None:
        text = settings if isinstance(settings, str) else json.dumps(settings)
        (self.homes / f"{home}.json").write_text(text)

    def run_check(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(args or [str(self.expected)])],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def test_matching_homes_exit_zero_and_print_nothing(self) -> None:
        self.install("default", SETTINGS)
        self.install("claude-worker", {**SETTINGS})

        proc = self.run_check()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_names_the_home_and_each_differing_key(self) -> None:
        self.install("default", SETTINGS)
        worker = json.loads(json.dumps(SETTINGS))
        worker["claude_session"]["model"] = "other-model"
        worker["github"].pop("app_id")
        worker["kanban_url"] = "https://example.invalid/kanban"
        self.install("claude-worker", worker)

        proc = self.run_check()

        self.assertEqual(
            (proc.returncode, proc.stdout),
            (
                1,
                "claude-worker: claude_session.model differs\n"
                "claude-worker: github.app_id differs\n"
                "claude-worker: kanban_url differs\n",
            ),
        )

    def test_never_prints_a_secret_that_a_reference_resolves_to(self) -> None:
        with_reference = json.dumps({**SETTINGS, "app_user_id": "${SECRET_VALUE}"})
        self.install("default", with_reference)
        self.install("claude-worker", with_reference)

        proc = self.run_check()

        self.assertEqual(
            (proc.returncode, proc.stdout),
            (1, "default: app_user_id differs\nclaude-worker: app_user_id differs\n"),
        )
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)

    def test_reports_a_home_without_settings_without_its_error_output(self) -> None:
        self.install("default", SETTINGS)

        proc = self.run_check()

        self.assertEqual(
            (proc.returncode, proc.stdout), (1, "claude-worker: `hermes config get` exited 1\n")
        )
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)

    def test_reports_output_that_is_not_json_without_printing_it(self) -> None:
        self.install("default", SETTINGS)
        self.install("claude-worker", "token ${SECRET_VALUE}")

        proc = self.run_check()

        self.assertEqual(
            (proc.returncode, proc.stdout), (1, "claude-worker: `hermes config get` did not print JSON\n")
        )
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)

    def run_against_root(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT)], env=self.env, capture_output=True, text=True
        )

    def test_without_a_file_a_worker_matching_the_root_exits_zero(self) -> None:
        self.install("default", {**SETTINGS, "app_user_id": "${SECRET_VALUE}"})
        self.install("claude-worker", {**SETTINGS, "app_user_id": "${SECRET_VALUE}"})

        proc = self.run_against_root()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_without_a_file_names_each_worker_key_that_differs_from_the_root(self) -> None:
        self.install("default", {**SETTINGS, "app_user_id": "${SECRET_VALUE}"})
        worker = json.loads(json.dumps(SETTINGS))
        worker["claude_session"]["model"] = "other-model"
        self.install("claude-worker", worker)

        proc = self.run_against_root()

        self.assertEqual(
            (proc.returncode, proc.stdout),
            (1, "claude-worker: app_user_id differs\nclaude-worker: claude_session.model differs\n"),
        )
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)

    def test_without_a_file_reports_a_root_without_settings(self) -> None:
        self.install("claude-worker", SETTINGS)

        proc = self.run_against_root()

        self.assertEqual((proc.returncode, proc.stdout), (1, "default: `hermes config get` exited 1\n"))
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)

    def test_refuses_more_than_one_settings_file(self) -> None:
        proc = self.run_check(str(self.expected), str(self.expected))

        self.assertEqual(
            (proc.returncode, proc.stdout, proc.stderr),
            (2, "", "usage: check-settings [<settings.json>]\n"),
        )

    def test_refuses_a_settings_file_it_cannot_read(self) -> None:
        proc = self.run_check(str(self.root / "missing.json"))

        self.assertEqual(
            (proc.returncode, proc.stdout, proc.stderr),
            (2, "", f"check-settings: could not read {self.root / 'missing.json'}: FileNotFoundError\n"),
        )


if __name__ == "__main__":
    unittest.main()
