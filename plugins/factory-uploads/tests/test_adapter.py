from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import PLUGIN, PNG, REAL_MANIFEST, TOKEN, UUID_URL, FakeUploads, secret_names_manifest

from factory_uploads import broker as broker_module
from factory_uploads.credentials import GATEWAY_ONLY

REPO = "example-org/example-repo"


class StubBase:
    """Just enough of Hermes' BasePlatformAdapter for the adapter to run."""

    def __init__(self, config, platform):
        self.config, self.platform = config, platform
        self.events = []

    def _mark_connected(self):
        self.events.append("connected")

    def _mark_disconnected(self):
        self.events.append("disconnected")

    def _set_fatal_error(self, code, message, *, retryable):
        self.events.append(("fatal", code, retryable))


class StubSendResult:
    def __init__(self, success=False, error=None):
        self.success, self.error = success, error


FAKE_LINEAR = """
def token_cache_path(environ):
    return environ["HERMES_HOME"] + "/plugin-data/linear-agent-session/token.json"


class LinearClient:
    def __init__(self, client_id, client_secret, *, token_cache_path=None):
        self.args = (client_id, client_secret, token_cache_path)

    def issue_token(self):
        return {"access_token": "lin_oauth_FAKE " + " ".join(self.args), "expires_at": 1900000000.0}
"""


def stub_hermes(home: Path, secrets: dict, config: dict | None = None) -> dict:
    def module(name, **attrs):
        made = types.ModuleType(name)
        made.__dict__.update(attrs)
        return made

    return {
        "gateway": module("gateway"),
        "gateway.config": module("gateway.config", Platform=lambda name: f"platform:{name}"),
        "gateway.platforms": module("gateway.platforms"),
        "gateway.platforms._shared": module(
            "gateway.platforms._shared", get_scoped_secret=lambda name, default=None: secrets.get(name, default)
        ),
        "gateway.platforms.base": module(
            "gateway.platforms.base", BasePlatformAdapter=StubBase, SendResult=StubSendResult
        ),
        "hermes_constants": module("hermes_constants", get_hermes_home=lambda: home),
        "hermes_cli": module("hermes_cli"),
        "hermes_cli.config": module("hermes_cli.config", load_config_readonly=lambda: config if config is not None else {}),
    }


def factory_settings(auth_token_env: str, mcp_servers: dict | None = None) -> dict:
    session = {"claude_session": {"auth_token_env": auth_token_env, "mcp_servers": mcp_servers or {}}}
    return {"plugins": {"entries": {"linear-agent-session": {"settings": session}}}}


class AdapterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.secrets = {
            "GITHUB_UPLOAD_TOKEN": TOKEN, "LINEAR_CLIENT_ID": "lin-id", "LINEAR_CLIENT_SECRET": "lin-secret",
            "EXAMPLE_PROXY_TOKEN": "proxy-FAKE-token", "EXAMPLE_SENTRY_TOKEN": "sntryu_FAKE",
            "FACTORY_INFISICAL_CLIENT_ID": "inf-id", "FACTORY_INFISICAL_CLIENT_SECRET": "inf-secret",
            "LINEAR_WEBHOOK_SECRET": "lin-hook", "GITHUB_WEBHOOK_SECRET": "gh-hook",
        }
        self.settings = factory_settings("EXAMPLE_PROXY_TOKEN")
        plugin = self.home / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "linear.py").write_text(FAKE_LINEAR)
        self.manifest = self.home / "plugins" / "factory-secret-names" / "plugin.yaml"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(secret_names_manifest(
            "EXAMPLE_PROXY_TOKEN", "EXAMPLE_SENTRY_TOKEN", "EXAMPLE_POSTHOG_KEY", *sorted(GATEWAY_ONLY),
        ))
        patcher = mock.patch.dict(sys.modules, stub_hermes(self.home, self.secrets, self.settings))
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("factory_uploads.adapter", None)
        self.addCleanup(sys.modules.pop, "factory_uploads.adapter", None)
        self.adapter_module = importlib.import_module("factory_uploads.adapter")
        self.uploads = FakeUploads()
        self.uploads.__enter__()
        self.addCleanup(self.uploads.__exit__, None, None, None)

    def make_adapter(self, repositories=(REPO,)):
        config = types.SimpleNamespace(extra={"repositories": list(repositories)})
        adapter = self.adapter_module.FactoryUploadsAdapter(config)
        adapter.broker._base_url = self.uploads.base
        adapter.broker._resolve_repository_id = lambda repo: 7
        return adapter

    async def ask(self, repo=REPO):
        reader, writer = await asyncio.open_unix_connection(str(self.home / "factory-uploads.sock"))
        header = {"op": "upload", "repo": repo, "name": "a.png", "size": len(PNG)}
        writer.write(json.dumps(header).encode() + b"\n" + PNG)
        await writer.drain()
        reply = json.loads(await reader.readline())
        writer.close()
        return reply

    async def request(self, op: str, **fields) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.home / "factory-uploads.sock"))
        writer.write(json.dumps({"op": op, **fields}).encode() + b"\n")
        await writer.drain()
        reply = json.loads(await reader.readline())
        writer.close()
        return reply

    async def mcp_token(self, server: str) -> dict:
        return await self.request("mcp_token", server=server)

    async def connected(self):
        adapter = self.make_adapter()
        await adapter.connect()
        self.addAsyncCleanup(adapter.disconnect)
        return adapter

    async def test_a_linear_token_comes_from_the_homes_plugin_client_with_the_gateways_credentials(self):
        await self.connected()

        reply = await self.request("linear_token")

        cache = f"{self.home}/plugin-data/linear-agent-session/token.json"
        self.assertEqual(
            reply, {"ok": True, "token": f"lin_oauth_FAKE lin-id lin-secret {cache}", "expires_at": 1900000000.0}
        )

    async def test_a_gateway_without_linear_credentials_issues_no_linear_token(self):
        await self.connected()
        self.secrets.pop("LINEAR_CLIENT_SECRET")

        reply = await self.request("linear_token")

        self.assertEqual(reply["state"], "unavailable")
        self.assertFalse((await self.request("status"))["linear_ready"])

    async def test_the_proxy_token_is_the_variable_the_factory_settings_name(self):
        await self.connected()

        self.assertEqual(await self.request("proxy_token"), {"ok": True, "token": "proxy-FAKE-token", "expires_at": None})
        self.secrets.pop("EXAMPLE_PROXY_TOKEN")
        self.assertEqual((await self.request("proxy_token"))["state"], "unavailable")

    async def test_settings_without_a_token_variable_serve_no_secret_as_the_proxy_token(self):
        self.settings.clear()
        self.settings.update(factory_settings(""))
        await self.connected()

        reply = await self.request("proxy_token")

        self.assertEqual(reply["state"], "unavailable")
        self.assertNotIn("proxy-FAKE-token", json.dumps(reply))

    async def test_an_mcp_token_is_the_variable_the_factory_settings_name_for_that_server(self):
        self.settings.update(factory_settings("EXAMPLE_PROXY_TOKEN", {
            "sentry": {"url": "https://mcp.example/sentry", "token_env": "EXAMPLE_SENTRY_TOKEN"},
            "posthog": {"url": "https://mcp.example/posthog", "token_env": "EXAMPLE_POSTHOG_KEY"},
        }))
        await self.connected()

        self.assertEqual(await self.mcp_token("sentry"), {"ok": True, "token": "sntryu_FAKE", "expires_at": None})
        self.assertEqual((await self.mcp_token("posthog"))["state"], "unavailable")
        self.assertEqual((await self.request("status"))["mcp_tokens_present"], {"posthog": False, "sentry": True})
        self.secrets.pop("EXAMPLE_SENTRY_TOKEN")
        self.assertEqual((await self.mcp_token("sentry"))["state"], "unavailable")

    async def test_a_request_never_names_the_variable_it_gets(self):
        self.settings.update(factory_settings("EXAMPLE_PROXY_TOKEN", {
            "sentry": {"url": "https://mcp.example/sentry", "token_env": "EXAMPLE_SENTRY_TOKEN"},
        }))
        await self.connected()

        for server in ("EXAMPLE_SENTRY_TOKEN", "EXAMPLE_PROXY_TOKEN", "GITHUB_UPLOAD_TOKEN"):
            with self.subTest(server=server):
                reply = await self.mcp_token(server)
                self.assertEqual(reply["state"], "unavailable")
                self.assertNotIn(self.secrets[server], json.dumps(reply))

    async def test_a_token_op_never_serves_the_upload_token_or_an_app_credential(self):
        for name in (
            "GITHUB_UPLOAD_TOKEN", "LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "FACTORY_INFISICAL_CLIENT_ID",
            "FACTORY_INFISICAL_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET",
        ):
            with self.subTest(name=name):
                self.settings.update(factory_settings(name, {"sentry": {"url": "https://mcp.example", "token_env": name}}))
                adapter = await self.connected()

                replies = [await self.request("proxy_token"), await self.mcp_token("sentry")]

                self.assertEqual([reply["state"] for reply in replies], ["unavailable", "unavailable"])
                self.assertNotIn(self.secrets[name], json.dumps(replies))
                await adapter.disconnect()

    async def test_a_token_op_never_serves_a_variable_the_manifest_does_not_declare(self):
        self.secrets.update({
            "SLACK_BOT_TOKEN": "xoxb-FAKE", "OP_SERVICE_ACCOUNT_TOKEN": "ops-FAKE", "EXAMPLE_UNDECLARED_TOKEN": "undeclared-FAKE",
        })
        for name in ("SLACK_BOT_TOKEN", "OP_SERVICE_ACCOUNT_TOKEN", "EXAMPLE_UNDECLARED_TOKEN"):
            with self.subTest(name=name):
                self.settings.update(factory_settings(name, {"sentry": {"url": "https://mcp.example", "token_env": name}}))
                adapter = await self.connected()

                replies = [await self.request("proxy_token"), await self.mcp_token("sentry")]

                self.assertEqual(replies, [{"ok": False, "state": "unavailable", "message": mock.ANY}] * 2)
                self.assertNotIn(self.secrets[name], json.dumps(replies))
                self.assertEqual((await self.request("status"))["mcp_tokens_present"], {"sentry": False})
                await adapter.disconnect()

    async def test_the_installed_manifest_makes_the_proxy_and_sentry_tokens_servable_and_nothing_else(self):
        self.manifest.write_bytes(REAL_MANIFEST.read_bytes())
        self.secrets.update({"CLAUDE_PROXY_TOKEN": "proxy-FAKE", "SENTRY_ACCESS_TOKEN": "sentry-FAKE", "SLACK_BOT_TOKEN": "xoxb-FAKE"})
        self.settings.update(factory_settings("CLAUDE_PROXY_TOKEN", {
            "sentry": {"url": "https://mcp.example/sentry", "token_env": "SENTRY_ACCESS_TOKEN"},
            "slack": {"url": "https://mcp.example/slack", "token_env": "SLACK_BOT_TOKEN"},
        }))
        await self.connected()

        self.assertEqual(await self.request("proxy_token"), {"ok": True, "token": "proxy-FAKE", "expires_at": None})
        self.assertEqual(await self.mcp_token("sentry"), {"ok": True, "token": "sentry-FAKE", "expires_at": None})
        self.assertEqual((await self.mcp_token("slack"))["state"], "unavailable")

    async def test_a_gateway_without_the_manifest_does_not_start(self):
        self.manifest.unlink()

        with self.assertRaises(FileNotFoundError):
            self.make_adapter()

    async def test_settings_without_mcp_servers_serve_no_mcp_token(self):
        for servers in (None, [], "sentry", {"sentry": "EXAMPLE_SENTRY_TOKEN"}, {"sentry": {"url": "https://mcp.example"}}):
            with self.subTest(servers=servers):
                self.settings.clear()
                self.settings.update(factory_settings("EXAMPLE_PROXY_TOKEN"))
                self.settings["plugins"]["entries"]["linear-agent-session"]["settings"]["claude_session"]["mcp_servers"] = servers
                adapter = await self.connected()

                reply = await self.mcp_token("sentry")

                self.assertEqual(reply["state"], "unavailable")
                self.assertNotIn("sntryu_FAKE", json.dumps(reply))
                await adapter.disconnect()

    async def test_status_reports_which_credentials_the_gateway_holds(self):
        await self.connected()

        status = await self.request("status")

        self.assertEqual(
            {key: status[key] for key in ("token_present", "linear_ready", "proxy_token_present")},
            {"token_present": True, "linear_ready": True, "proxy_token_present": True},
        )

    async def test_the_github_app_is_the_one_the_factory_settings_name(self):
        key = self.home / "github-app.pem"
        github = {"app_id": "1", "installation_id": "2", "private_key_path": str(key)}
        self.settings["plugins"]["entries"]["linear-agent-session"]["settings"]["github"] = github
        adapter = await self.connected()

        self.assertFalse((await self.request("status"))["github_app_ready"])
        key.write_text("key")
        self.assertTrue((await self.request("status"))["github_app_ready"])
        self.assertEqual(adapter.broker._credentials._github_app.installation_id, "2")

    async def test_settings_without_a_private_key_serve_no_github_token(self):
        await self.connected()

        self.assertEqual((await self.request("github_token"))["state"], "unavailable")

    async def test_connect_listens_in_the_hermes_home_and_disconnect_removes_the_socket(self):
        adapter = self.make_adapter()

        self.assertTrue(await adapter.connect())
        self.assertEqual(adapter.events, ["connected"])
        self.assertEqual(await self.ask(), {"ok": True, "url": UUID_URL})
        await adapter.disconnect()

        self.assertFalse((self.home / "factory-uploads.sock").exists())
        self.assertEqual(adapter.events, ["connected", "disconnected"])

    async def test_the_repository_allowlist_comes_from_the_platform_config(self):
        adapter = self.make_adapter(repositories=("example-org/other",))
        await adapter.connect()
        self.addAsyncCleanup(adapter.disconnect)

        reply = await self.ask()

        self.assertEqual(reply["message"], broker_module.NOT_ALLOWED)
        self.assertEqual(self.uploads.requests, [])

    async def test_the_token_is_read_from_the_gateway_secrets_for_each_request(self):
        adapter = self.make_adapter()
        await adapter.connect()
        self.addAsyncCleanup(adapter.disconnect)

        self.secrets.pop("GITHUB_UPLOAD_TOKEN")
        self.assertEqual((await self.ask())["state"], "unavailable")
        self.secrets["GITHUB_UPLOAD_TOKEN"] = TOKEN
        self.assertTrue((await self.ask())["ok"])

    async def test_a_second_gateway_does_not_take_the_socket(self):
        first, second = self.make_adapter(), self.make_adapter()
        await first.connect()
        self.addAsyncCleanup(first.disconnect)

        self.assertFalse(await second.connect())
        self.assertEqual(second.events, [("fatal", "already_listening", False)])
        self.assertTrue((await self.ask())["ok"])

    async def test_it_is_not_a_chat_platform(self):
        result = await self.make_adapter().send("chat", "hello")

        self.assertFalse(result.success)
        self.assertEqual(result.error, "factory_uploads is not a chat platform")

    async def test_register_declares_no_delivery_target_and_no_user_surface(self):
        registered = {}
        ctx = types.SimpleNamespace(register_platform=lambda **kwargs: registered.update(kwargs))

        self.adapter_module.register(ctx)

        self.assertEqual(registered["name"], "factory_uploads")
        reaching = {
            "cron_deliver_env_var", "send_message_handler", "standalone_sender_fn", "allowed_users_env",
            "allow_all_env", "required_env", "env_enablement_fn", "trusted_inbound", "parse_target_ref_fn",
        }
        self.assertEqual(reaching & set(registered), set())


class ImportTest(unittest.TestCase):
    def test_importing_the_package_starts_nothing_and_loads_no_hermes_module(self):
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('factory_uploads', {str(PLUGIN / '__init__.py')!r}, "
            f"submodule_search_locations=[{str(PLUGIN)!r}])\n"
            "module = importlib.util.module_from_spec(spec); sys.modules['factory_uploads'] = module\n"
            "spec.loader.exec_module(module)\n"
            "print(sorted(name for name in sys.modules if name.startswith(('gateway', 'hermes', 'factory_uploads.'))))\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)

        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
