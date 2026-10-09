from __future__ import annotations

import asyncio
import contextlib
import enum
import io
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import FakeLinear, activity_ok, load_plugin

plugin = load_plugin()


class Platform(enum.Enum):
    WEBHOOK = "webhook"
    SLACK = "slack"


def event(payload, route="linear", platform=Platform.WEBHOOK):
    source = SimpleNamespace(platform=platform, user_id=f"webhook:{route}", chat_type="webhook")
    return SimpleNamespace(source=source, raw_message=payload, text="rendered prompt")


def session_event(action, signal=None):
    payload = {
        "type": "AgentSessionEvent",
        "action": action,
        "appUserId": "app-user-1",
        "agentSession": {"id": "session-1", "issue": {"id": "issue-1", "identifier": "ABC-1"}},
    }
    if action == "prompted":
        payload["agentActivity"] = {"content": {"type": "prompt", "body": "please continue"}, "signal": signal}
    return payload


class RecordingClient:
    def __init__(self, error=None, delay=0.0):
        self.calls = []
        self.error = error
        self.delay = delay

    def create_activity(self, agent_session_id, content, *, ephemeral=False):
        time.sleep(self.delay)
        if self.error:
            raise self.error
        self.calls.append((agent_session_id, content, ephemeral))
        return "activity-1"


def run_hook(evt, client, route="linear"):
    hook = plugin.make_dispatch_hook(lambda: client, lambda: route)
    return asyncio.run(hook(event=evt, gateway=object(), session_store=None))


ACK = ("session-1", {"type": "thought", "body": "Received. Triaging now."}, True)


class DispatchHookTest(unittest.TestCase):
    def test_created_posts_ephemeral_thought(self):
        client = RecordingClient()
        self.assertIsNone(run_hook(event(session_event("created")), client))
        self.assertEqual(client.calls, [ACK])

    def test_prompted_posts_ephemeral_thought(self):
        client = RecordingClient()
        self.assertIsNone(run_hook(event(session_event("prompted")), client))
        self.assertEqual(client.calls, [ACK])

    def test_ignored_events(self):
        cases = {
            "stop signal": event(session_event("prompted", signal="stop")),
            "other route": event(session_event("created"), route="github"),
            "other platform": event(session_event("created"), platform=Platform.SLACK),
            "other payload type": event({**session_event("created"), "type": "Issue"}),
            "other action": event({**session_event("created"), "action": "removed"}),
            "non-dict payload": event("not json"),
        }
        for name, evt in cases.items():
            with self.subTest(name):
                client = RecordingClient()
                self.assertIsNone(run_hook(evt, client))
                self.assertEqual(client.calls, [])

    def test_configured_route(self):
        client = RecordingClient()
        run_hook(event(session_event("created"), route="linear-agent"), client, route="linear-agent")
        self.assertEqual(client.calls, [ACK])

    def test_linear_failure_returns_none(self):
        client = RecordingClient(error=plugin.linear.LinearError("boom"))
        with self.assertLogs("linear_agent_session", "ERROR"):
            self.assertIsNone(run_hook(event(session_event("created")), client))

    def test_slow_linear_is_abandoned_at_the_deadline(self):
        client = RecordingClient(delay=1.0)
        hook = plugin.make_dispatch_hook(lambda: client, lambda: "linear")

        async def timed():
            started = time.monotonic()
            result = await hook(event=event(session_event("created")))
            return result, time.monotonic() - started

        with mock.patch.object(plugin, "ACK_TIMEOUT_SECONDS", 0.1), self.assertLogs("linear_agent_session", "ERROR"):
            result, elapsed = asyncio.run(timed())
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.5)


class FakeContext:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.tools = {}
        self.hooks = []

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {
            "LINEAR_CLIENT_ID": "client-id",
            "LINEAR_CLIENT_SECRET": "client-secret",
            "LINEAR_TOKEN_CACHE": os.path.join(self.tmp.name, "token.json"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HERMES_HOME", None)
        network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network call"))
        network.start()
        self.addCleanup(network.stop)

    def test_registers_tools_and_hook(self):
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertEqual(sorted(ctx.tools), ["linear_agent_activity", "linear_agent_session_create", "linear_issue"])
        for name, tool in ctx.tools.items():
            self.assertEqual(tool["toolset"], "linear_agent_session")
            self.assertEqual(tool["schema"]["name"], name)
        self.assertEqual([name for name, _ in ctx.hooks], ["pre_gateway_dispatch"])

    def test_registered_handlers_call_linear(self):
        fake = FakeLinear([activity_ok("activity-9")])
        ctx = FakeContext()
        plugin.register(ctx)
        handler = ctx.tools["linear_agent_activity"]["handler"]
        with mock.patch("urllib.request.urlopen", fake):
            output = handler({"agent_session_id": "session-1", "type": "thought", "body": "Working"}, task_id="t")
        self.assertEqual(json.loads(output), {"ok": True, "agent_activity_id": "activity-9"})

    def test_registered_handler_reports_missing_credentials(self):
        ctx = FakeContext()
        plugin.register(ctx)
        with mock.patch.dict(os.environ, {"LINEAR_CLIENT_ID": "", "LINEAR_ACCESS_TOKEN": ""}):
            output = ctx.tools["linear_issue"]["handler"]({"issue": "ABC-1"})
        self.assertEqual(json.loads(output), {"error": "set LINEAR_ACCESS_TOKEN, or LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET"})

    def test_registered_hook_uses_configured_route(self):
        fake = FakeLinear([activity_ok()])
        ctx = FakeContext({"webhook_route": "agent"})
        plugin.register(ctx)
        hook = ctx.hooks[0][1]
        with mock.patch("urllib.request.urlopen", fake):
            asyncio.run(hook(event=event(session_event("created"), route="linear")))
            self.assertEqual(fake.graphql_requests, [])
            self.assertIsNone(asyncio.run(hook(event=event(session_event("created"), route="agent"))))
        self.assertEqual(
            fake.graphql_requests[0]["variables"],
            {
                "input": {
                    "agentSessionId": "session-1",
                    "content": {"type": "thought", "body": "Received. Triaging now."},
                    "ephemeral": True,
                }
            },
        )


class CliTest(unittest.TestCase):
    def run_cli(self, argv, fake):
        cli = _load_cli()
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "LINEAR_CLIENT_ID": "client-id",
                "LINEAR_CLIENT_SECRET": "client-secret",
                "LINEAR_TOKEN_CACHE": os.path.join(tmp, "token.json"),
            },
        ), mock.patch("urllib.request.urlopen", fake), contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, json.loads(out.getvalue())

    def test_activity_command(self):
        fake = FakeLinear([activity_ok("activity-5"), (200, {"data": {"agentSessionUpdate": {"success": True}}})])
        code, output = self.run_cli(
            [
                "activity", "--agent-session-id", "session-1", "--type", "action", "--action", "Queued",
                "--parameter", "t_1", "--ephemeral", "--external-url", "Card=https://example.test/t_1",
            ],
            fake,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            output, {"ok": True, "agent_activity_id": "activity-5", "external_urls": ["https://example.test/t_1"]}
        )
        self.assertEqual(
            fake.graphql_requests[0]["variables"],
            {
                "input": {
                    "agentSessionId": "session-1",
                    "content": {"type": "action", "action": "Queued", "parameter": "t_1"},
                    "ephemeral": True,
                }
            },
        )
        self.assertEqual(
            fake.graphql_requests[1]["variables"],
            {"id": "session-1", "input": {"externalUrls": [{"label": "Card", "url": "https://example.test/t_1"}]}},
        )

    def test_issue_command_error_exits_non_zero(self):
        fake = FakeLinear([(200, {"errors": [{"message": "Entity not found: Issue"}], "data": None})])
        code, output = self.run_cli(["issue", "ABC-404"], fake)
        self.assertEqual(code, 1)
        self.assertEqual(output, {"error": "Entity not found: Issue"})


def _load_cli():
    import importlib.util

    from tests import PLUGIN_DIR

    spec = importlib.util.spec_from_file_location("linear_agent_session_cli", os.path.join(PLUGIN_DIR, "cli.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
