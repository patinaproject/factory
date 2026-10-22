from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from .test_factory_secrets import CLIENT_ID, CLIENT_SECRET, TOKEN, WRAPPER, FakeInfisical

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
OP_TOKEN = "ops_token-31c7"

REAL_OP = """#!/bin/sh
{{ echo "argv: $*"; env | sed 's/^/env: /'; }} >> {log}
"""

FORBIDDEN_CLI = """#!/bin/sh
echo "infisical was called: $*" >> {log}
exit 1
"""


class OpWrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.server = FakeInfisical()
        self.server.secrets_body = json.dumps(
            {"secrets": [{"secretKey": "OP_SERVICE_ACCOUNT_TOKEN", "secretValue": OP_TOKEN}]}
        ).encode()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.children = self.tmp / "children.log"
        self.op_log = self.tmp / "op.log"
        self.cli_log = self.tmp / "cli.log"
        home, bin_dir, stubs = self.tmp / "home", self.tmp / "bin", self.tmp / "stubs"
        for directory in (home / "bin", bin_dir, stubs):
            directory.mkdir(parents=True)
        (home / ".env").write_text(
            f"FACTORY_INFISICAL_CLIENT_ID={CLIENT_ID}\nFACTORY_INFISICAL_CLIENT_SECRET={CLIENT_SECRET}\n"
        )
        self.write(bin_dir / "op", REAL_OP.format(log=self.op_log))
        self.write(stubs / "infisical", FORBIDDEN_CLI.format(log=self.cli_log))
        replacements = {
            "<api url>": f"http://127.0.0.1:{self.server.server_address[1]}",
            "<project id>": "proj1",
            "<environment>": "dev",
            "<folder>": "/factory",
            "<home>": str(home),
            "<bin dir>": str(bin_dir),
        }
        for real in ("/usr/bin/curl", "/usr/bin/perl"):
            wrapper = self.tmp / Path(real).name
            self.write(wrapper, WRAPPER.format(log=self.children, real=real))
            replacements[real] = str(wrapper)
        self.install(SCRIPTS / "identity-secrets", home / "bin" / "identity-secrets", replacements)
        self.wrapper = self.install(SCRIPTS / "op-wrapper", self.tmp / "op", replacements)
        self.path = f"{stubs}:/usr/bin:/bin"

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o755)

    def install(self, template: Path, path: Path, replacements: dict) -> Path:
        text = template.read_text()
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        self.assertNotRegex(text, "<[a-z ]*>")
        self.write(path, text)
        return path

    def run_wrapper(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.wrapper), *args], env={"PATH": self.path}, capture_output=True, text=True, timeout=60
        )

    def test_runs_op_with_the_token_and_the_same_arguments(self) -> None:
        result = self.run_wrapper("read", "op://vault/item/field")

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        lines = self.op_log.read_text().splitlines()
        self.assertIn("argv: read op://vault/item/field", lines)
        self.assertIn(f"env: OP_SERVICE_ACCOUNT_TOKEN={OP_TOKEN}", lines)

    def test_no_process_holds_the_identity(self) -> None:
        self.assertEqual(self.run_wrapper("whoami").returncode, 0)

        children = self.children.read_text().splitlines()
        self.assertGreater(sum(line.startswith("argv: ") for line in children), 0)
        everything = children + self.op_log.read_text().splitlines()
        for value in (CLIENT_ID, CLIENT_SECRET, TOKEN, "FACTORY_INFISICAL", "INFISICAL_"):
            self.assertEqual([line for line in everything if value in line], [])
        self.assertFalse(self.cli_log.exists())

    def test_fails_with_one_line_when_sign_in_fails(self) -> None:
        self.server.login_status = 401

        result = self.run_wrapper("whoami")

        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith("op wrapper: "))
        self.assertFalse(self.op_log.exists())

    def test_fails_with_one_line_when_the_token_is_missing(self) -> None:
        self.server.secrets_body = json.dumps({"secrets": []}).encode()

        result = self.run_wrapper("whoami")

        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse(self.op_log.exists())


if __name__ == "__main__":
    unittest.main()
