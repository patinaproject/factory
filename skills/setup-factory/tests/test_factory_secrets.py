from __future__ import annotations

import json
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

TEMPLATE = Path(__file__).resolve().parents[1] / "scripts" / "factory-secrets"

CLIENT_ID = "cid-5c1e7d"
CLIENT_SECRET = "csecret-9f3a21"
TOKEN = "tok-4b8e06"
VALUES = {"client id": CLIENT_ID, "client secret": CLIENT_SECRET, "access token": TOKEN}

SECRETS = {
    "secrets": [
        {"secretKey": "ZED", "secretValue": "last", "skipMultilineEncoding": False},
        {"secretKey": "A_EQ", "secretValue": "x=y", "skipMultilineEncoding": False},
        {"secretKey": "QUOTE", "secretValue": "it's \"q\"", "skipMultilineEncoding": False},
        {"secretKey": "PEM", "secretValue": "line1\nline2", "skipMultilineEncoding": True},
        {"secretKey": "RAW", "secretValue": "r1\nr2", "skipMultilineEncoding": False},
        {"secretKey": "SHARED", "secretValue": "folder", "skipMultilineEncoding": False},
        {"secretKey": "UNICODE", "secretValue": "café", "skipMultilineEncoding": False},
    ],
    "imports": [
        {"secretPath": "/a", "secrets": [{"secretKey": "SHARED", "secretValue": "import-a"}, {"secretKey": "IMP", "secretValue": "a"}]},
        {"secretPath": "/b", "secrets": [{"secretKey": "IMP", "secretValue": "b"}]},
    ],
}

DOTENV = (
    "A_EQ='x=y'\n"
    "IMP='b'\n"
    "PEM='line1\\nline2'\n"
    "QUOTE='it's \"q\"'\n"
    "RAW='r1\nr2'\n"
    "SHARED='folder'\n"
    "UNICODE='café'\n"
    "ZED='last'\n"
)


class FakeInfisical(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list = []
        self.login_status = 200
        self.secrets_body = json.dumps(SECRETS).encode()
        self.hold_method = ""
        self.held = threading.Event()
        self.release = threading.Event()


class Handler(BaseHTTPRequestHandler):
    server: FakeInfisical

    def log_message(self, *args) -> None:
        pass

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def wait_if_held(self) -> None:
        if self.command == self.server.hold_method:
            self.server.held.set()
            self.server.release.wait(20)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(("POST", self.path, body))
        self.wait_if_held()
        ok = self.server.login_status == 200 and body == {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}
        if self.path == "/api/v1/auth/universal-auth/login" and ok:
            self.reply(200, json.dumps({"accessToken": TOKEN, "expiresIn": 7200}).encode())
        else:
            self.reply(401, b'{"message":"invalid credentials"}')

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        self.server.requests.append(("GET", url.path, parse_qs(url.query)))
        self.wait_if_held()
        if url.path == "/api/v4/secrets" and self.headers["Authorization"] == f"Bearer {TOKEN}":
            self.reply(200, self.server.secrets_body)
        else:
            self.reply(403, b'{"message":"forbidden"}')


WRAPPER = """#!/bin/sh
{{ echo "argv: $*"; env | sed 's/^/env: /'; }} >> {log}
exec {real} "$@"
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
        self.server = FakeInfisical()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.log = self.tmp / "children.log"
        self.helper = self.render(self.tmp / "factory-secrets", wrapped=True)
        self.env = {
            "PATH": "/usr/bin:/bin",
            "FACTORY_INFISICAL_CLIENT_ID": CLIENT_ID,
            "FACTORY_INFISICAL_CLIENT_SECRET": CLIENT_SECRET,
        }

    def render(self, path: Path, wrapped: bool) -> Path:
        rendered = TEMPLATE.read_text()
        replacements = {
            "<api url>": f"http://127.0.0.1:{self.server.server_address[1]}",
            "<project id>": "proj1",
            "<environment>": "dev",
            "<folder>": "/factory",
        }
        if wrapped:
            for real in ("/usr/bin/curl", "/usr/bin/perl"):
                wrapper = self.tmp / Path(real).name
                wrapper.write_text(WRAPPER.format(log=self.log, real=real))
                wrapper.chmod(0o755)
                replacements[real] = str(wrapper)
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        path.write_text(rendered)
        path.chmod(0o700)
        return path

    def run_helper(self, *args: str, helper: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(helper or self.helper), *args], env=self.env, capture_output=True, text=True, timeout=60
        )

    def test_prints_the_export_through_a_pipe(self) -> None:
        result = self.run_helper()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, DOTENV, ""))
        self.assertEqual(
            self.server.requests,
            [
                ("POST", "/api/v1/auth/universal-auth/login", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}),
                (
                    "GET",
                    "/api/v4/secrets",
                    {
                        "projectId": ["proj1"],
                        "environment": ["dev"],
                        "secretPath": ["/factory"],
                        "includeImports": ["true"],
                        "recursive": ["false"],
                        "expandSecretReferences": ["true"],
                        "includePersonalOverrides": ["true"],
                    },
                ),
            ],
        )

    def test_no_child_holds_the_identity_or_the_token(self) -> None:
        result = self.run_helper()

        self.assertEqual(result.returncode, 0)
        lines = self.log.read_text().splitlines()
        self.assertEqual(sum(line.startswith("argv: ") for line in lines), 5)
        for value in (CLIENT_ID, CLIENT_SECRET, TOKEN):
            self.assertEqual([line for line in lines if value in line], [])
        self.assertEqual([line for line in lines if "FACTORY_INFISICAL" in line], [])

    @unittest.skipUnless(sys.platform == "darwin", "the helper relies on macOS hiding platform binaries' environments")
    def test_no_process_shows_a_secret_while_a_request_is_held(self) -> None:
        helper = self.render(self.tmp / "factory-secrets-real", wrapped=False)
        for method in ("POST", "GET"):
            with self.subTest(held=method):
                self.server.hold_method = method
                self.server.held.clear()
                self.server.release.clear()
                proc = subprocess.Popen([str(helper)], env=self.env, stdout=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(self.server.held.wait(20))
                    listing = subprocess.run(
                        ["/bin/ps", "-axeww", "-o", "pid=,command="], capture_output=True, text=True, check=True
                    ).stdout
                finally:
                    self.server.release.set()
                    out, _ = proc.communicate(timeout=60)
                # Booleans only: the listing holds other processes' environments.
                self.assertTrue(str(helper) in listing and "/usr/bin/curl" in listing, "the helper and curl are running")
                shown = [name for name, value in VALUES.items() if value in listing]
                self.assertEqual(shown, [])
                self.assertEqual((proc.returncode, out), (0, DOTENV))

    def test_refuses_any_argument(self) -> None:
        result = self.run_helper("--help")

        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertEqual(self.server.requests, [])

    def test_refuses_a_terminal_stdout(self) -> None:
        self.assertEqual(run_on_a_terminal([str(self.helper)], self.env), (2, b""))
        self.assertEqual(self.server.requests, [])

    def test_fails_loudly_when_every_login_fails(self) -> None:
        self.server.login_status = 401

        result = self.run_helper()

        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual([method for method, *_ in self.server.requests], ["POST", "POST", "POST"])
        self.assertIn("factory-secrets: Infisical export failed after 3 attempts", result.stderr)
        self.assertIn("returned error: 401", result.stderr)
        for value in (CLIENT_ID, CLIENT_SECRET, TOKEN):
            self.assertNotIn(value, result.stderr)

    def test_never_echoes_a_malformed_secrets_response(self) -> None:
        self.server.secrets_body = b'{"secrets": [{"secretKey": "K", "secretValue": "leaked-value"'

        result = self.run_helper()

        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(
            result.stderr.splitlines()[-2:],
            ["factory-secrets: attempt 3 of 3 failed", "factory-secrets: Infisical export failed after 3 attempts"],
        )
        self.assertNotIn("leaked-value", result.stderr)

    def test_fails_without_a_request_when_the_identity_is_missing(self) -> None:
        del self.env["FACTORY_INFISICAL_CLIENT_SECRET"]

        result = self.run_helper()

        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(self.server.requests, [])
        self.assertIn("FACTORY_INFISICAL_CLIENT_SECRET", result.stderr)


if __name__ == "__main__":
    unittest.main()
