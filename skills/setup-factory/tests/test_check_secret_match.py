from __future__ import annotations

import json
import os
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
    print(json.dumps(os.environ["FAKE_ROUTE_SECRET"]))
else:
    sys.exit(1)
"""

FAKE_INFISICAL = """\
#!/bin/sh
printf '%s\\n' "$FAKE_FOLDER"
"""

FAKE_HELPER = """\
#!/bin/sh
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
        self.helper = self.tmp / "helper"
        self.helper.write_text(FAKE_HELPER.format(id=CLIENT_ID, secret=CLIENT_SECRET, loaded=self.tmp / "loaded"))
        for path in (hermes, infisical, self.helper):
            path.chmod(0o755)
        self.env_file = self.tmp / ".env"
        self.env_file.write_text(
            f"FACTORY_INFISICAL_CLIENT_ID={CLIENT_ID}\nFACTORY_INFISICAL_CLIENT_SECRET={CLIENT_SECRET}\n"
        )
        self.log = self.tmp / "hermes.log"
        self.load(f"OTHER='x'\nLINEAR_WEBHOOK_SECRET='{FOLDER_VALUE}'\n")
        self.env = {
            **os.environ,
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_HELPER": str(self.helper),
            "FAKE_FOLDER": json.dumps([{"key": "LINEAR_WEBHOOK_SECRET", "value": FOLDER_VALUE}]),
            "FAKE_ROUTE_SECRET": "${LINEAR_WEBHOOK_SECRET}",
        }

    def load(self, text: str) -> None:
        (self.tmp / "loaded").write_text(text)

    def check(self, **overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.env_file), "proj1", "dev", "/factory", "LINEAR_WEBHOOK_SECRET", "linear"],
            env={**self.env, **overrides},
            capture_output=True,
            text=True,
            timeout=60,
        )

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
        result = self.check(FAKE_ROUTE_SECRET="${GITHUB_WEBHOOK_SECRET}")

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))

    def test_reports_a_mismatch_when_the_route_holds_a_literal(self) -> None:
        result = self.check(FAKE_ROUTE_SECRET=OTHER_VALUE)

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))
        self.assert_quiet(result)

    def test_reports_a_mismatch_when_the_folder_has_no_such_key(self) -> None:
        result = self.check(FAKE_FOLDER="[]")

        self.assertEqual((result.returncode, result.stdout), (1, "0\n"))

    def test_never_asks_hermes_for_a_raw_value(self) -> None:
        self.check()

        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertEqual([call for call in calls if "--raw" in call], [])


if __name__ == "__main__":
    unittest.main()
