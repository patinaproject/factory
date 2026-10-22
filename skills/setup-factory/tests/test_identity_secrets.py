from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from .test_factory_secrets import CLIENT_ID, CLIENT_SECRET, TOKEN, WRAPPER, FakeInfisical

LIBRARY = Path(__file__).resolve().parents[1] / "scripts" / "identity-secrets"

SECRETS = {
    "secrets": [
        {"secretKey": "ALPHA", "secretValue": "alpha-value"},
        {"secretKey": "OP_SERVICE_ACCOUNT_TOKEN", "secretValue": "ops_token-31c7"},
    ]
}

CALL = """\
. "$1"
fn=$2
shift 2
"$fn" "$@"
code=$?
printf 'LEFT=%s\\n' "${_out+_out }${_id+_id }${_secret+_secret }${_tok+_tok }" >&2
exit $code
"""


class IdentitySecretsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.server = FakeInfisical()
        self.server.secrets_body = __import__("json").dumps(SECRETS).encode()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.log = self.tmp / "children.log"
        self.library = self.render()
        self.env_file = self.tmp / ".env"
        self.env_file.write_text(
            f"OTHER=unrelated\nFACTORY_INFISICAL_CLIENT_ID={CLIENT_ID}\nFACTORY_INFISICAL_CLIENT_SECRET={CLIENT_SECRET}\n"
        )

    def render(self) -> Path:
        rendered = LIBRARY.read_text()
        replacements = {
            "<api url>": f"http://127.0.0.1:{self.server.server_address[1]}",
            "<project id>": "proj1",
        }
        for real in ("/usr/bin/curl", "/usr/bin/perl"):
            wrapper = self.tmp / Path(real).name
            wrapper.write_text(WRAPPER.format(log=self.log, real=real))
            wrapper.chmod(0o755)
            replacements[real] = str(wrapper)
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        path = self.tmp / "identity-secrets"
        path.write_text(rendered)
        return path

    def call(self, function: str, *args: str) -> tuple:
        result = subprocess.run(
            ["sh", "-c", CALL, "sh", str(self.library), function, str(self.env_file), *args],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        left = [line for line in result.stderr.splitlines() if line.startswith("LEFT=")]
        stderr = "\n".join(line for line in result.stderr.splitlines() if not line.startswith("LEFT="))
        return result.returncode, result.stdout, stderr, left[0].removeprefix("LEFT=").split()

    def test_identity_keys_prints_the_names_only(self) -> None:
        code, out, err, left = self.call("identity_keys", "dev", "/factory")

        self.assertEqual((code, out, err), (0, "ALPHA\nOP_SERVICE_ACCOUNT_TOKEN\n", ""))
        self.assertEqual(left, [])

    def test_identity_value_prints_one_value(self) -> None:
        code, out, err, left = self.call("identity_value", "dev", "/factory", "OP_SERVICE_ACCOUNT_TOKEN")

        self.assertEqual((code, out, err), (0, "ops_token-31c7\n", ""))
        self.assertEqual(left, [])

    def test_identity_value_fails_for_a_missing_key(self) -> None:
        code, out, err, left = self.call("identity_value", "dev", "/factory", "NOPE")

        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("alpha-value", err)
        self.assertEqual(left, [])

    def test_nothing_is_left_behind_when_sign_in_fails(self) -> None:
        self.server.login_status = 401
        for function, args in (("identity_keys", ()), ("identity_value", ("ALPHA",))):
            with self.subTest(function):
                code, out, err, left = self.call(function, "dev", "/factory", *args)

                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"{function}: sign-in failed", err)
                self.assertEqual(left, [])

    def test_nothing_is_left_behind_when_the_folder_cannot_be_read(self) -> None:
        self.server.secrets_status = 403
        for function, args in (("identity_keys", ()), ("identity_value", ("ALPHA",))):
            with self.subTest(function):
                code, out, err, left = self.call(function, "dev", "/factory", *args)

                self.assertEqual((code, out, err), (1, "", ""))
                self.assertEqual(left, [])

    def test_no_child_holds_the_identity_or_the_token(self) -> None:
        for function, args in (("identity_keys", ()), ("identity_value", ("ALPHA",))):
            self.log.write_text("")
            with self.subTest(function):
                self.assertEqual(self.call(function, "dev", "/factory", *args)[0], 0)

                lines = self.log.read_text().splitlines()
                self.assertEqual(sum(line.startswith("argv: ") for line in lines), 5)
                for value in (CLIENT_ID, CLIENT_SECRET, TOKEN, "FACTORY_INFISICAL"):
                    self.assertEqual([line for line in lines if value in line], [])


if __name__ == "__main__":
    unittest.main()
