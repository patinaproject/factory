from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.support import SCRIPTS

APP_ID = "12345"
INSTALLATION_ID = "67890"
TOKEN = "ghs_installationtoken"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "w") as log:
    json.dump({
        "argv": sys.argv[1:],
        "gh_config_dir": os.environ.get("GH_CONFIG_DIR"),
        "gh_token": os.environ.get("GH_TOKEN"),
        "stdin": sys.stdin.read(),
    }, log)
sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
"""


def b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class GitHubStub(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        stub = self.server
        authorization = self.headers.get("Authorization", "")
        jwt = authorization[len("Bearer "):] if authorization.startswith("Bearer ") else ""
        stub.requests.append({
            "path": self.path,
            "accept": self.headers.get("Accept"),
            "api_version": self.headers.get("X-GitHub-Api-Version"),
            "jwt": jwt,
        })
        status, body = stub.reply
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


class RefreshGhAppLoginTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.key = self.tmp / "app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.key), "2048"], check=True, capture_output=True)
        os.chmod(self.key, 0o600)
        self.public_key = self.tmp / "app.pub.pem"
        subprocess.run(
            ["openssl", "rsa", "-in", str(self.key), "-pubout", "-out", str(self.public_key)],
            check=True, capture_output=True,
        )
        self.fake_gh = self.tmp / "fake-gh"
        self.fake_gh.write_text(FAKE_GH)
        os.chmod(self.fake_gh, 0o755)
        self.gh_log = self.tmp / "gh.json"
        self.gh_config_dir = self.tmp / "gh"

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GitHubStub)
        self.server.requests = []
        self.server.reply = (201, {"token": TOKEN, "expires_at": "2030-01-01T00:00:00Z"})
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def settings(self, **github) -> Path:
        values = {"login": "example-app[bot]", "app_id": APP_ID, "installation_id": INSTALLATION_ID,
                  "private_key_path": str(self.key)}
        values.update(github)
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"repositories": [], "github": {k: v for k, v in values.items() if v is not None}}))
        return path

    def run_script(self, settings: Path, **env_changes) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ["PATH"],
            "GITHUB_API_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
            "GH_BIN": str(self.fake_gh),
            "FAKE_GH_LOG": str(self.gh_log),
            "GH_CONFIG_DIR": str(self.gh_config_dir),
            "GH_TOKEN": "ambient-token",
        }
        env.update(env_changes)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(
            [str(SCRIPTS / "refresh-gh-app-login"), "--settings-json", str(settings)],
            capture_output=True, text=True, env=env,
        )

    def verify_signature(self, jwt: str) -> bool:
        header, claims, signature = jwt.split(".")
        (self.tmp / "signed").write_bytes(f"{header}.{claims}".encode())
        (self.tmp / "signature").write_bytes(b64url_decode(signature))
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(self.public_key),
             "-signature", str(self.tmp / "signature"), str(self.tmp / "signed")],
            capture_output=True,
        )
        return proc.returncode == 0

    def test_mints_a_token_and_signs_gh_in_with_it(self) -> None:
        started = int(time.time())

        proc = self.run_script(self.settings())

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        [request] = self.server.requests
        self.assertEqual(request["path"], f"/app/installations/{INSTALLATION_ID}/access_tokens")
        self.assertEqual(request["accept"], "application/vnd.github+json")
        self.assertEqual(request["api_version"], "2022-11-28")
        self.assertTrue(self.verify_signature(request["jwt"]))
        header, claims, _ = request["jwt"].split(".")
        self.assertEqual(json.loads(b64url_decode(header)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(b64url_decode(claims))
        self.assertEqual(claims["iss"], APP_ID)
        self.assertEqual(claims["exp"] - claims["iat"], 600)
        self.assertLessEqual(abs(claims["iat"] - (started - 60)), 5)
        self.assertEqual(json.loads(self.gh_log.read_text()), {
            "argv": ["auth", "login", "--with-token", "--hostname", "github.com", "--insecure-storage"],
            "gh_config_dir": str(self.gh_config_dir),
            "gh_token": None,
            "stdin": TOKEN,
        })

    def test_integer_ids_are_accepted(self) -> None:
        proc = self.run_script(self.settings(app_id=int(APP_ID), installation_id=int(INSTALLATION_ID)))

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertEqual(self.server.requests[0]["path"], f"/app/installations/{INSTALLATION_ID}/access_tokens")

    def assert_refused(self, proc: subprocess.CompletedProcess, line: str) -> None:
        self.assertEqual((proc.returncode, proc.stdout), (1, f"refresh-gh-app-login: {line}\n"))
        self.assertFalse(self.gh_log.exists())

    def test_missing_settings_keys_are_refused(self) -> None:
        cases = {
            "app_id": "read settings: github.app_id must be a numeric ID",
            "installation_id": "read settings: github.installation_id must be a numeric ID",
            "private_key_path": "read settings: github.private_key_path must be an absolute path",
        }
        for key, line in cases.items():
            with self.subTest(key=key):
                self.assert_refused(self.run_script(self.settings(**{key: None})), line)
        self.assertEqual(self.server.requests, [])

    def test_non_numeric_installation_id_is_refused(self) -> None:
        proc = self.run_script(self.settings(installation_id="../../user"))

        self.assert_refused(proc, "read settings: github.installation_id must be a numeric ID")
        self.assertEqual(self.server.requests, [])

    def test_group_readable_key_is_refused(self) -> None:
        os.chmod(self.key, 0o640)

        proc = self.run_script(self.settings())

        self.assert_refused(proc, f"check private key: {self.key} is readable by group or others; chmod 600 it")
        self.assertEqual(self.server.requests, [])

    def test_missing_key_file_is_refused(self) -> None:
        missing = self.tmp / "missing.pem"

        proc = self.run_script(self.settings(private_key_path=str(missing)))

        self.assert_refused(proc, f"check private key: {missing} does not exist")

    def test_unset_gh_config_dir_is_refused(self) -> None:
        proc = self.run_script(self.settings(), GH_CONFIG_DIR=None)

        self.assert_refused(proc, "check environment: GH_CONFIG_DIR is not set; set it in the worker profile's .env")
        self.assertEqual(self.server.requests, [])

    def test_api_error_is_reported_without_signing_gh_in(self) -> None:
        self.server.reply = (404, {"message": "Not Found"})

        proc = self.run_script(self.settings())

        self.assert_refused(proc, 'mint installation token: HTTP 404: {"message": "Not Found"}')

    def test_gh_failure_is_reported_without_the_token(self) -> None:
        proc = self.run_script(self.settings(), FAKE_GH_EXIT="1")

        self.assertEqual((proc.returncode, proc.stdout), (1, "refresh-gh-app-login: gh auth login: exited 1\n"))
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
