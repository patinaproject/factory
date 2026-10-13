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

from tests.support import PLUGIN, PNG, TOKEN, UUID_URL, FakeUploads

from factory_uploads import broker as broker_module

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


def stub_hermes(home: Path, secrets: dict) -> dict:
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
    }


class AdapterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.secrets = {"GITHUB_UPLOAD_TOKEN": TOKEN}
        patcher = mock.patch.dict(sys.modules, stub_hermes(self.home, self.secrets))
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
