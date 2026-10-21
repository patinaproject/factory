from __future__ import annotations

import ast
import asyncio
import importlib
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

from tests.support import PLUGIN, TOKEN, QuickServer

service = importlib.import_module("factory_uploads.service")

REPO_ROOT = PLUGIN.parents[1]
SERVICE_FILES = (
    "plugins/factory-uploads/__init__.py",
    "plugins/factory-uploads/broker.py",
    "plugins/factory-uploads/credentials.py",
    "plugins/factory-uploads/service.py",
    "plugins/factory-uploads/upload_token.py",
    "plugins/linear-agent-session/linear.py",
)


class FakeInfisical:
    def __init__(self, folders: dict, imports: dict | None = None, failed_logins: int = 0):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, body))
                if len([r for r in outer.requests if r[0] == "POST"]) <= failed_logins:
                    self.reply({"message": "unavailable"}, status=503)
                    return
                self.reply({"accessToken": "inf-access"})

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                outer.requests.append(("GET", url.path, query, self.headers["Authorization"]))
                folder = query["secretPath"]
                self.reply({
                    "secrets": [{"secretKey": k, "secretValue": v} for k, v in folders[folder].items()],
                    "imports": [
                        {"secretPath": source, "secrets": [{"secretKey": k, "secretValue": v} for k, v in values.items()]}
                        for source, values in (imports or {}).get(folder, [])
                    ],
                })

            def reply(self, document, status=200):
                body = json.dumps(document).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = QuickServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.run_dir = Path(tmp.name) / "run"
        self.home.mkdir()
        self.run_dir.mkdir()
        self.config_path = Path(tmp.name) / "broker.json"
        self.config = {
            "socket": str(self.run_dir / "factory-uploads.sock"),
            "home": str(self.home),
            "allowed_uids": [os.getuid()],
            "repositories": {"example-org/example-repo": 7},
            "claude_session": {"auth_token_env": "EXAMPLE_PROXY_TOKEN"},
        }

    def write_config(self, **changes) -> Path:
        self.config_path.write_text(json.dumps({**self.config, **changes}))
        return self.config_path


class ConfigTest(ServiceTestCase):
    def test_a_complete_config_loads(self):
        self.assertEqual(service.load_config(self.write_config()), self.config)

    def test_a_config_without_an_allowed_uid_is_refused(self):
        for uids in ([], [0], ["501"]):
            with self.subTest(uids=uids), self.assertRaises(service.ConfigError):
                service.load_config(self.write_config(allowed_uids=uids))

    def test_a_repository_without_its_numeric_id_is_refused(self):
        with self.assertRaises(service.ConfigError):
            service.load_config(self.write_config(repositories={"example-org/example-repo": "7"}))


class SecretsTest(ServiceTestCase):
    def test_without_infisical_the_secrets_are_the_homes_env_file(self):
        (self.home / ".env").write_text(f"# comment\nGITHUB_UPLOAD_TOKEN={TOKEN}\nexport EXAMPLE_PROXY_TOKEN='proxy-FAKE'\n")

        self.assertEqual(
            service.load_secrets(self.config),
            {"GITHUB_UPLOAD_TOKEN": TOKEN, "EXAMPLE_PROXY_TOKEN": "proxy-FAKE"},
        )

    def test_infisical_folders_load_in_order_under_the_env_file_and_drop_the_identity(self):
        infisical = FakeInfisical({
            "/factory": {"EXAMPLE_PROXY_TOKEN": "proxy-from-factory", "LINEAR_CLIENT_ID": "old-id"},
            "/factory/broker": {"LINEAR_CLIENT_ID": "lin-id", "LINEAR_CLIENT_SECRET": "lin-secret"},
        })
        self.addCleanup(infisical.close)
        (self.home / ".env").write_text(
            "BROKER_INFISICAL_CLIENT_ID=inf-id\nBROKER_INFISICAL_CLIENT_SECRET=inf-secret\nEXAMPLE_PROXY_TOKEN=proxy-local\n"
        )
        config = {**self.config, "infisical": {
            "api_url": infisical.url + "/", "project_id": "proj", "environment": "development",
            "paths": ["/factory", "/factory/broker"],
        }}

        secrets = service.load_secrets(config)

        self.assertEqual(secrets, {
            "EXAMPLE_PROXY_TOKEN": "proxy-local", "LINEAR_CLIENT_ID": "lin-id", "LINEAR_CLIENT_SECRET": "lin-secret",
        })
        login, first, second = infisical.requests
        self.assertEqual(login, ("POST", "/api/v1/auth/universal-auth/login", {"clientId": "inf-id", "clientSecret": "inf-secret"}))
        self.assertEqual(first, ("GET", "/api/v4/secrets", {
            "projectId": "proj", "environment": "development", "secretPath": "/factory",
            "includeImports": "true", "recursive": "false", "expandSecretReferences": "true",
            "includePersonalOverrides": "true",
        }, "Bearer inf-access"))
        self.assertEqual(second[2]["secretPath"], "/factory/broker")

    def infisical_config(self, infisical, paths=("/factory/broker",)):
        (self.home / ".env").write_text("BROKER_INFISICAL_CLIENT_ID=inf-id\nBROKER_INFISICAL_CLIENT_SECRET=inf-secret\n")
        return {**self.config, "infisical": {
            "api_url": infisical.url, "project_id": "proj", "environment": "development", "paths": list(paths),
        }}

    def test_an_imported_secret_loads_only_where_the_folder_has_no_such_key(self):
        infisical = FakeInfisical(
            {"/factory/broker": {"LINEAR_CLIENT_ID": "own-id"}},
            imports={"/factory/broker": [
                ("/shared", {"LINEAR_CLIENT_ID": "imported-id", "GITHUB_UPLOAD_TOKEN": "first-import"}),
                ("/later", {"GITHUB_UPLOAD_TOKEN": "later-import"}),
            ]},
        )
        self.addCleanup(infisical.close)

        self.assertEqual(service.load_secrets(self.infisical_config(infisical)), {
            "LINEAR_CLIENT_ID": "own-id", "GITHUB_UPLOAD_TOKEN": "later-import",
        })

    def test_a_failed_attempt_is_tried_again(self):
        infisical = FakeInfisical({"/factory/broker": {"LINEAR_CLIENT_ID": "lin-id"}}, failed_logins=2)
        self.addCleanup(infisical.close)

        with mock.patch.object(service, "INFISICAL_PAUSE", 0), self.assertLogs("factory_uploads.service", "WARNING") as logs:
            secrets = service.load_secrets(self.infisical_config(infisical))

        self.assertEqual(secrets, {"LINEAR_CLIENT_ID": "lin-id"})
        self.assertEqual([r[0] for r in infisical.requests], ["POST", "POST", "POST", "GET"])
        self.assertEqual(len(logs.records), 2)
        self.assertNotIn("inf-secret", "".join(logs.output))

    def test_three_failed_attempts_stop_the_start(self):
        infisical = FakeInfisical({"/factory/broker": {}}, failed_logins=3)
        self.addCleanup(infisical.close)

        with mock.patch.object(service, "INFISICAL_PAUSE", 0), self.assertLogs("factory_uploads.service", "WARNING"):
            with self.assertRaises(urllib.error.HTTPError):
                service.load_secrets(self.infisical_config(infisical))
        self.assertEqual([r[0] for r in infisical.requests], ["POST", "POST", "POST"])

    def test_infisical_without_the_brokers_identity_refuses_to_start(self):
        config = {**self.config, "infisical": {"api_url": "http://127.0.0.1:9", "project_id": "p", "environment": "e", "paths": ["/f"]}}

        with self.assertRaises(service.ConfigError):
            service.load_secrets(config)


class MainTest(ServiceTestCase):
    def test_a_bad_config_exits_1_without_listening(self):
        with self.assertLogs("factory_uploads.service", level="ERROR"):
            self.assertEqual(service.main(["--config", str(self.write_config(allowed_uids=[]))]), 1)
        self.assertFalse((self.run_dir / "factory-uploads.sock").exists())


class ServeTest(ServiceTestCase):
    def test_the_service_answers_its_allowed_uid_on_the_configured_socket(self):
        secrets = {"GITHUB_UPLOAD_TOKEN": TOKEN, "EXAMPLE_PROXY_TOKEN": "proxy-FAKE"}
        broker = service.make_broker(service.load_config(self.write_config()), secrets)

        async def scenario():
            stopping = asyncio.Event()
            serving = asyncio.ensure_future(service.serve(broker, stopping))
            while not broker.socket_path.exists():
                await asyncio.sleep(0.01)
            mode = stat.S_IMODE(os.stat(broker.socket_path).st_mode)
            replies = []
            for op in ("status", "proxy_token"):
                reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
                writer.write(json.dumps({"op": op}).encode() + b"\n")
                replies.append(json.loads(await reader.readline()))
                writer.close()
            stopping.set()
            return mode, replies, await serving

        mode, (status, proxy), code = asyncio.run(scenario())

        self.assertEqual(mode, 0o660)
        self.assertTrue(status["token_present"])
        self.assertFalse(status["linear_ready"])
        self.assertEqual(proxy, {"ok": True, "token": "proxy-FAKE", "expires_at": None})
        self.assertEqual(code, 0)
        self.assertFalse(broker.socket_path.exists())
        self.assertTrue((self.home / "state" / "log.jsonl").exists())


class SystemPythonTest(unittest.TestCase):
    def test_every_file_the_service_runs_parses_as_python_3_9(self):
        for name in SERVICE_FILES:
            with self.subTest(file=name):
                ast.parse((REPO_ROOT / name).read_text(), filename=name, feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
