from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "scripts" / "factory-secrets"

FAKE_INFISICAL = """#!/bin/sh
echo "$* token=$INFISICAL_TOKEN id=$INFISICAL_UNIVERSAL_AUTH_CLIENT_ID" >> "$FAKE_INFISICAL_LOG"
case "$1" in
  login) [ -n "$FAKE_LOGIN_FAILS" ] || echo tok ;;
  export) printf 'A=1\\nB=2\\n' ;;
esac
"""


def run_on_a_terminal(command: list, env: dict) -> tuple:
    """Run with stdout on a pty and return the exit code and what reached the terminal."""
    primary, replica = pty.openpty()
    try:
        code = subprocess.run(command, env=env, stdout=replica, stderr=subprocess.PIPE, timeout=30).returncode
        # macOS drops unread pty output once the replica closes, so read while it is open.
        written = os.read(primary, 4096) if select.select([primary], [], [], 0.5)[0] else b""
    finally:
        os.close(replica)
        os.close(primary)
    return code, written


class FactorySecretsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        rendered = TEMPLATE.read_text()
        for placeholder, value in (("<project id>", "proj1"), ("<environment>", "dev"), ("<folder>", "/factory")):
            rendered = rendered.replace(placeholder, value)
        self.helper = self.tmp / "factory-secrets"
        self.helper.write_text(rendered)
        self.helper.chmod(0o700)
        fake = self.tmp / "bin" / "infisical"
        fake.parent.mkdir()
        fake.write_text(FAKE_INFISICAL)
        fake.chmod(0o755)
        self.log = self.tmp / "infisical.log"
        self.env = {
            "PATH": f"{fake.parent}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "FAKE_INFISICAL_LOG": str(self.log),
            "FACTORY_INFISICAL_CLIENT_ID": "cid",
            "FACTORY_INFISICAL_CLIENT_SECRET": "csecret",
        }

    def run_helper(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.helper), *args], env={**self.env, **env}, capture_output=True, text=True, timeout=30
        )

    def test_prints_the_export_through_a_pipe(self) -> None:
        result = self.run_helper()

        self.assertEqual((result.returncode, result.stdout), (0, "A=1\nB=2\n"))
        self.assertEqual(
            self.log.read_text().splitlines(),
            [
                "login --method=universal-auth --silent --plain token= id=cid",
                "export --silent --projectId=proj1 --env=dev --path=/factory --format=dotenv token=tok id=",
            ],
        )

    def test_refuses_any_argument(self) -> None:
        result = self.run_helper("--help")

        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertFalse(self.log.exists())

    def test_refuses_a_terminal_stdout(self) -> None:
        self.assertEqual(run_on_a_terminal([str(self.helper)], self.env), (2, b""))
        self.assertFalse(self.log.exists())

    def test_prints_nothing_when_every_login_fails(self) -> None:
        result = self.run_helper(FAKE_LOGIN_FAILS="1")

        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(len(self.log.read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
