from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from tests.support import QuickServer, secret_names_manifest

from factory_uploads import broker as broker_module
from factory_uploads import credentials as credentials_module
from factory_uploads import github_app

APP_ID = "12345"
INSTALLATION_ID = "67890"
TOKEN = "ghs_FAKEinstallationtoken"
EXPIRES = "2030-01-01T00:00:00Z"
EXPIRES_AT = 1893456000.0
PERMISSIONS = {"actions": "read", "contents": "write", "metadata": "read"}


def b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeGitHub:
    def __init__(self):
        self.requests = []
        self.reply = (201, {"token": TOKEN, "expires_at": EXPIRES, "permissions": PERMISSIONS})
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                authorization = self.headers.get("Authorization", "")
                outer.requests.append({
                    "path": self.path,
                    "accept": self.headers.get("Accept"),
                    "api_version": self.headers.get("X-GitHub-Api-Version"),
                    "jwt": authorization[len("Bearer "):] if authorization.startswith("Bearer ") else "",
                })
                status, body = outer.reply
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = QuickServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class GitHubAppTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.key = self.tmp / "github-app.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.key), "2048"], check=True, capture_output=True)
        os.chmod(self.key, 0o600)
        self.public_key = self.tmp / "github-app.pub.pem"
        subprocess.run(
            ["openssl", "rsa", "-in", str(self.key), "-pubout", "-out", str(self.public_key)],
            check=True, capture_output=True,
        )
        self.github = FakeGitHub().__enter__()
        self.addCleanup(self.github.__exit__)
        self.now = 1_800_000_000.0

    def app(self, **changes) -> github_app.GitHubApp:
        settings = {"app_id": APP_ID, "installation_id": INSTALLATION_ID, "private_key_path": str(self.key), **changes}
        return github_app.GitHubApp.from_settings(settings, api_url=self.github.url, clock=lambda: self.now)

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


class MintTest(GitHubAppTestCase):
    def test_a_signed_jwt_buys_an_installation_token_with_its_expiry_and_permissions(self):
        token = self.app().installation_token()

        self.assertEqual(token, {"access_token": TOKEN, "expires_at": EXPIRES_AT, "permissions": PERMISSIONS})
        [request] = self.github.requests
        self.assertEqual(request["path"], f"/app/installations/{INSTALLATION_ID}/access_tokens")
        self.assertEqual((request["accept"], request["api_version"]), ("application/vnd.github+json", "2022-11-28"))
        self.assertTrue(self.verify_signature(request["jwt"]))
        header, claims, _ = request["jwt"].split(".")
        self.assertEqual(json.loads(b64url_decode(header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(
            json.loads(b64url_decode(claims)),
            {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": APP_ID},
        )

    def test_only_named_permission_levels_are_kept(self):
        permissions = {"actions": 2, "checks": "read", "nested": {"admin": "write"}}
        self.github.reply = (201, {"token": TOKEN, "expires_at": EXPIRES, "permissions": permissions})

        self.assertEqual(self.app().installation_token()["permissions"], {"checks": "read"})

    def test_integer_ids_are_accepted(self):
        self.app(app_id=int(APP_ID), installation_id=int(INSTALLATION_ID)).installation_token()

        self.assertEqual(self.github.requests[0]["path"], f"/app/installations/{INSTALLATION_ID}/access_tokens")

    def test_a_token_is_reused_until_it_could_expire_before_the_workers_next_refresh(self):
        app = self.app()
        self.now = EXPIRES_AT - 3600
        app.installation_token()

        self.now = EXPIRES_AT - github_app.REFRESH_MARGIN - 1
        app.installation_token()
        self.assertEqual(len(self.github.requests), 1)

        self.now = EXPIRES_AT - github_app.REFRESH_MARGIN
        app.installation_token()
        self.assertEqual(len(self.github.requests), 2)

    def test_the_margin_outlasts_the_thirty_minute_refresh_job(self):
        self.assertGreater(github_app.REFRESH_MARGIN, 30 * 60)


class FailureTest(GitHubAppTestCase):
    def test_a_refusal_names_the_status_and_githubs_message(self):
        self.github.reply = (401, {"message": "A JSON web token could not be decoded"})

        with self.assertRaises(github_app.GitHubAppError) as raised:
            self.app().installation_token()

        self.assertEqual(str(raised.exception), "mint installation token: HTTP 401: A JSON web token could not be decoded")

    def test_a_reply_without_a_token_or_expiry_is_an_error(self):
        for body in ({}, {"token": TOKEN}, {"token": TOKEN, "expires_at": "soon"}, []):
            with self.subTest(body=body):
                self.github.reply = (201, body)
                with self.assertRaises(github_app.GitHubAppError):
                    self.app().installation_token()

    def test_a_missing_key_is_an_error_and_not_ready(self):
        self.key.unlink()
        app = self.app()

        self.assertFalse(app.ready())
        with self.assertRaises(github_app.GitHubAppError):
            app.installation_token()
        self.assertEqual(self.github.requests, [])


class SettingsTest(GitHubAppTestCase):
    def test_settings_without_a_key_path_name_no_app(self):
        for settings in (None, {}, {"app_id": APP_ID, "installation_id": INSTALLATION_ID}, {"private_key_path": ""}):
            with self.subTest(settings=settings):
                self.assertIsNone(github_app.GitHubApp.from_settings(settings))

    def test_settings_with_a_bad_id_or_a_relative_key_path_are_refused(self):
        for changes in ({"app_id": "../1"}, {"installation_id": None}, {"app_id": True}, {"private_key_path": "app.pem"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.app(**changes)


class BrokerContainmentTest(GitHubAppTestCase, unittest.IsolatedAsyncioTestCase):
    """No reply, audit line, or log line the broker writes holds the key or the JWT."""

    async def asyncSetUp(self):
        self.home = self.tmp / "home"
        self.home.mkdir()
        manifest = self.tmp / "secret-names.yaml"
        manifest.write_text(secret_names_manifest("EXAMPLE_PROXY_TOKEN"))
        self.asked = []
        self.secrets = {"EXAMPLE_PROXY_TOKEN": "proxy-FAKE"}
        credentials = credentials_module.GatewayCredentials(
            lambda name: (self.asked.append(name), self.secrets.get(name))[1],
            {"auth_token_env": "EXAMPLE_PROXY_TOKEN"}, self.tmp / "linear.py", self.home, manifest, github_app=self.app(),
        )
        self.broker = broker_module.Broker(
            self.home, [], lambda: None, credentials=credentials, socket_path=self.tmp / "b.sock",
        )
        self.assertTrue(await self.broker.start())
        self.addAsyncCleanup(self.broker.stop)

    async def ask(self, op: str) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
        writer.write(json.dumps({"op": op}).encode() + b"\n")
        await writer.drain()
        reply = json.loads(await reader.readline())
        writer.close()
        return reply

    def assert_contained(self, written: str) -> None:
        key_lines = [line for line in self.key.read_text().splitlines() if "-----" not in line]
        jwts = [request["jwt"] for request in self.github.requests]
        self.assertTrue(jwts)
        for secret in (*key_lines, *jwts, *(jwt.rsplit(".", 1)[1] for jwt in jwts)):
            self.assertNotIn(secret, written)

    async def test_a_granted_token_is_the_only_secret_in_the_reply_and_the_audit_holds_none(self):
        replies = [await self.ask("github_token"), await self.ask("status")]

        self.assertEqual(replies[0], {"ok": True, "token": TOKEN, "expires_at": EXPIRES_AT, "permissions": PERMISSIONS})
        self.assertIs(replies[1]["github_app_ready"], True)
        audit = (self.home / "factory-uploads" / "log.jsonl").read_text()
        self.assertEqual([json.loads(line)["state"] for line in audit.splitlines()], ["ok"])
        self.assertNotIn(TOKEN, audit)
        self.assert_contained(json.dumps(replies) + audit)

    async def test_a_refused_mint_answers_a_fixed_message_and_logs_neither_key_nor_jwt(self):
        self.github.reply = (401, {"message": "Bad credentials"})

        with self.assertLogs("factory_uploads.broker", level="WARNING") as logs:
            reply = await self.ask("github_token")

        self.assertEqual(reply, {"ok": False, "state": "error", "message": broker_module.GITHUB_FAILED})
        audit = (self.home / "factory-uploads" / "log.jsonl").read_text()
        self.assertEqual([json.loads(line)["state"] for line in audit.splitlines()], ["error"])
        self.assert_contained(json.dumps(reply) + audit + "\n".join(logs.output))

    async def test_the_allowlist_neither_blocks_the_github_token_nor_widens_what_a_token_op_serves(self):
        reply = await self.ask("github_token")
        self.assertEqual(reply, {"ok": True, "token": TOKEN, "expires_at": EXPIRES_AT, "permissions": PERMISSIONS})
        self.assertEqual(await self.ask("proxy_token"), {"ok": True, "token": "proxy-FAKE", "expires_at": None})
        self.assertEqual(self.asked, ["EXAMPLE_PROXY_TOKEN"])

    async def test_a_settings_typo_naming_an_app_variable_serves_neither_key_nor_jwt(self):
        manifest = self.tmp / "secret-names.yaml"
        for name in ("GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_JWT"):
            self.secrets[name] = "app-secret-FAKE"
            credentials = credentials_module.GatewayCredentials(
                self.secrets.get, {"auth_token_env": name, "mcp_servers": {"app": {"token_env": name}}},
                self.tmp / "linear.py", self.home, manifest, github_app=self.app(),
            )
            self.assertIsNone(credentials.proxy_token())
            self.assertIsNone(credentials.mcp_token("app"))
            self.assertEqual(credentials.github_token()["access_token"], TOKEN)


if __name__ == "__main__":
    unittest.main()
