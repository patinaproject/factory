from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-secret-match"
FOLDER_VALUE = "whsec-folder-7d41"
OTHER_VALUE = "whsec-stale-02bc"
CLIENT_ID = "cid-5c1e7d"
CLIENT_SECRET = "csecret-9f3a21"

FAKE_HERMES = """\
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
if "--raw" in args:
    sys.exit(99)
key = args[2]
if key == "secrets.command":
    print("Command helper: applied 1 secrets")
    print(json.dumps({"command": os.environ["FAKE_HELPER"]}))
elif key.endswith(".secret"):
    print(json.dumps(os.environ["FAKE_ROUTE_READ"]))
else:
    sys.exit(1)
"""

FAKE_INFISICAL = """\
#!/bin/sh
printf '%s\\n' "$FAKE_FOLDER"
"""

FAKE_HELPER = """\
#!/bin/sh
: > "{ran}"
[ "$FACTORY_INFISICAL_CLIENT_ID" = "{id}" ] && [ "$FACTORY_INFISICAL_CLIENT_SECRET" = "{secret}" ] || exit 1
cat "{loaded}"
"""


class CheckSecretMatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        hermes = stubs / "hermes"
        hermes.write_text(f"#!{sys.executable}\n{FAKE_HERMES}")
        infisical = stubs / "infisical"
        infisical.write_text(FAKE_INFISICAL)
        self.ran = self.tmp / "helper-ran"
        self.helper = self.tmp / "helper"
        self.helper.write_text(FAKE_HELPER.format(id=CLIENT_ID, secret=CLIENT_SECRET, loaded=self.tmp / "loaded", ran=self.ran))
        for path in (hermes, infisical, self.helper):
            path.chmod(0o755)
        self.env_file = self.tmp / ".env"
        self.dotenv("")
        self.log = self.tmp / "hermes.log"
        self.load(f"OTHER='x'\nLINEAR_WEBHOOK_SECRET='{FOLDER_VALUE}'\n")
        self.env = {
            **os.environ,
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_HELPER": str(self.helper),
            "FAKE_FOLDER": json.dumps([{"key": "LINEAR_WEBHOOK_SECRET", "value": FOLDER_VALUE}]),
            "FAKE_ROUTE_READ": "***",
        }
        self.config("${LINEAR_WEBHOOK_SECRET}")

    def config(self, secret: str) -> None:
        (self.tmp / "config.yaml").write_text(
            f"platforms:\n  webhook:\n    extra:\n      routes:\n        linear:\n          secret: {secret}\n"
        )

    def dotenv(self, extra: str) -> None:
        self.env_file.write_text(
            f"FACTORY_INFISICAL_CLIENT_ID={CLIENT_ID}\nFACTORY_INFISICAL_CLIENT_SECRET={CLIENT_SECRET}\n{extra}"
        )

    def load(self, text: str) -> None:
        (self.tmp / "loaded").write_text(text)

    def check(self, terminal: bool = True, **overrides: str) -> subprocess.CompletedProcess:
        controller, follower = pty.openpty() if terminal else (None, subprocess.DEVNULL)
        try:
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(self.env_file), "proj1", "dev", "/factory", "LINEAR_WEBHOOK_SECRET", "linear"],
                env={**self.env, **overrides},
                stdin=follower,
                capture_output=True,
                text=True,
                timeout=60,
            )
        finally:
            if terminal:
                os.close(controller)
                os.close(follower)

    def assert_quiet(self, result: subprocess.CompletedProcess) -> None:
        for value in (FOLDER_VALUE, OTHER_VALUE, CLIENT_ID, CLIENT_SECRET):
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_reports_a_match(self) -> None:
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (0, "1\n"))
        self.assert_quiet(result)

    def test_reports_a_mismatch(self) -> None:
        self.load(f"LINEAR_WEBHOOK_SECRET='{OTHER_VALUE}'\n")
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))
        self.assert_quiet(result)

    def test_reports_a_mismatch_when_the_helper_omits_the_key(self) -> None:
        self.load("OTHER='x'\n")
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))

    def test_reports_a_mismatch_when_the_route_reads_another_key(self) -> None:
        self.config("${GITHUB_WEBHOOK_SECRET}")
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))

    def test_reports_a_mismatch_when_the_route_holds_a_literal(self) -> None:
        self.config(OTHER_VALUE)
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))
        self.assert_quiet(result)

    def test_reports_a_match_when_hermes_expands_the_route_secret(self) -> None:
        result = self.check(FAKE_ROUTE_READ=FOLDER_VALUE)

        self.assertEqual((result.returncode, result.stdout), (0, "1\n"))
        self.assert_quiet(result)

    def test_reports_a_mismatch_when_the_folder_has_no_such_key(self) -> None:
        result = self.check(FAKE_FOLDER="[]")

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))

    def test_compares_the_env_file_value_when_the_env_file_sets_the_key(self) -> None:
        self.dotenv(f"LINEAR_WEBHOOK_SECRET={OTHER_VALUE}\n")
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))
        self.assert_quiet(result)

    def test_reports_a_match_from_the_env_file_without_running_the_helper(self) -> None:
        self.dotenv(f"export LINEAR_WEBHOOK_SECRET='{FOLDER_VALUE}'\n")
        self.load(f"LINEAR_WEBHOOK_SECRET='{OTHER_VALUE}'\n")
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (0, "1\n"))
        self.assertFalse(self.ran.exists())
        self.assert_quiet(result)

    def test_compares_the_helper_value_when_the_env_file_sets_the_key_empty(self) -> None:
        self.dotenv("LINEAR_WEBHOOK_SECRET=\n")
        result = self.check()

        self.assertEqual((result.returncode, result.stdout), (0, "1\n"))
        self.assertTrue(self.ran.exists())

    def test_refuses_to_run_without_a_terminal(self) -> None:
        result = self.check(terminal=False)

        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertFalse(self.ran.exists())
        self.assertFalse(self.log.exists())
        self.assert_quiet(result)

    def test_never_hands_the_identity_to_a_shell_found_on_path(self) -> None:
        spy = self.tmp / "spy"
        spy.mkdir()
        sniffed = self.tmp / "sniffed"
        fake_sh = spy / "sh"
        fake_sh.write_text(f'#!/bin/sh\nenv >> "{sniffed}"\nexec /bin/sh "$@"\n')
        fake_sh.chmod(0o755)
        result = self.check(PATH=f"{spy}{os.pathsep}{self.env['PATH']}")

        self.assertEqual((result.returncode, result.stdout), (0, "1\n"))
        leaked = sniffed.read_text() if sniffed.exists() else ""
        self.assertNotIn(CLIENT_ID, leaked)
        self.assertNotIn(CLIENT_SECRET, leaked)

    def test_never_asks_hermes_for_a_raw_value(self) -> None:
        self.check()

        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertEqual([call for call in calls if "--raw" in call], [])


if __name__ == "__main__":
    unittest.main()
