from __future__ import annotations

import asyncio
import contextlib
import enum
import io
import json
import os
import sys
import tempfile
import types
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


RESOLVED_SECRET = "s3cret-resolved-value"
SKIP_LINEAR = {"action": "skip", "reason": "route linear secret is the unresolved reference ${LINEAR_WEBHOOK_SECRET}"}


def webhook_config(routes, global_secret=None):
    extra = {"routes": routes}
    if global_secret is not None:
        extra["secret"] = global_secret
    return {"platforms": {"webhook": {"extra": extra}}}


RESOLVED_CONFIG = webhook_config({"linear": {"secret": RESOLVED_SECRET}, "github": {"secret": RESOLVED_SECRET}})


def run_hook(evt, client, route="linear", config=RESOLVED_CONFIG):
    hook = plugin.make_dispatch_hook(lambda: client, lambda: route, lambda: config)
    return asyncio.run(hook(event=evt, gateway=object(), session_store=None))


@contextlib.contextmanager
def hermes_config(config):
    hermes_cli = types.ModuleType("hermes_cli")
    module = types.ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: config
    hermes_cli.config = module
    with mock.patch.dict(sys.modules, {"hermes_cli": hermes_cli, "hermes_cli.config": module}):
        yield


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
        hook = plugin.make_dispatch_hook(lambda: client, lambda: "linear", lambda: RESOLVED_CONFIG)

        async def timed():
            started = time.monotonic()
            result = await hook(event=event(session_event("created")))
            return result, time.monotonic() - started

        with mock.patch.object(plugin, "ACK_TIMEOUT_SECONDS", 0.1), self.assertLogs("linear_agent_session", "ERROR"):
            result, elapsed = asyncio.run(timed())
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.5)


class SecretGuardTest(unittest.TestCase):
    def test_unresolved_secret_skips_without_acknowledging(self):
        config = webhook_config(
            {"linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"}, "github": {"secret": RESOLVED_SECRET}}
        )
        client = RecordingClient()
        with self.assertLogs("linear_agent_session", "ERROR") as logs:
            result = run_hook(event(session_event("created")), client, config=config)
        self.assertEqual(result, SKIP_LINEAR)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("route linear", logs.output[0])
        self.assertIn("${LINEAR_WEBHOOK_SECRET}", logs.output[0])
        self.assertNotIn(RESOLVED_SECRET, logs.output[0])

    def test_unresolved_secret_on_another_route_is_skipped(self):
        config = webhook_config({"github": {"secret": "${env:GITHUB_WEBHOOK_SECRET}"}})
        with self.assertLogs("linear_agent_session", "ERROR"):
            result = run_hook(event({"zen": "hi"}, route="github"), RecordingClient(), config=config)
        reason = "route github secret is the unresolved reference ${GITHUB_WEBHOOK_SECRET}"
        self.assertEqual(result, {"action": "skip", "reason": reason})

    def test_route_without_secret_uses_global_fallback(self):
        client = RecordingClient()
        unresolved = webhook_config({"linear": {}}, global_secret="${LINEAR_WEBHOOK_SECRET}")
        with self.assertLogs("linear_agent_session", "ERROR"):
            self.assertEqual(run_hook(event(session_event("created")), client, config=unresolved), SKIP_LINEAR)
        self.assertEqual(client.calls, [])
        resolved = webhook_config({"linear": {}}, global_secret=RESOLVED_SECRET)
        self.assertIsNone(run_hook(event(session_event("created")), client, config=resolved))
        self.assertEqual(client.calls, [ACK])

    def test_route_secret_overrides_unresolved_global(self):
        config = webhook_config({"linear": {"secret": RESOLVED_SECRET}}, global_secret="${WEBHOOK_SECRET}")
        client = RecordingClient()
        self.assertIsNone(run_hook(event(session_event("created")), client, config=config))
        self.assertEqual(client.calls, [ACK])

    def test_accepted_without_skip(self):
        unresolved = webhook_config({"linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"}})
        cases = {
            "secret only contains a reference": (
                webhook_config({"linear": {"secret": "x-${LINEAR_WEBHOOK_SECRET}"}}),
                event({"type": "Issue"}),
            ),
            "non-webhook event": (unresolved, event({"type": "Issue"}, platform=Platform.SLACK)),
            "route absent from config": (unresolved, event({"type": "Issue"}, route="dynamic")),
            "no webhook platform": ({}, event({"type": "Issue"})),
        }
        for name, (config, evt) in cases.items():
            with self.subTest(name):
                self.assertIsNone(run_hook(evt, RecordingClient(), config=config))

    def test_config_is_read_per_delivery(self):
        configs = [webhook_config({"linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"}}), RESOLVED_CONFIG]
        client = RecordingClient()
        hook = plugin.make_dispatch_hook(lambda: client, lambda: "linear", lambda: configs.pop(0))
        with self.assertLogs("linear_agent_session", "ERROR"):
            self.assertEqual(asyncio.run(hook(event=event(session_event("created")))), SKIP_LINEAR)
        self.assertIsNone(asyncio.run(hook(event=event(session_event("created")))))
        self.assertEqual(client.calls, [ACK])


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
            "HERMES_HOME": self.tmp.name,
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network call"))
        network.start()
        self.addCleanup(network.stop)
        config = hermes_config(RESOLVED_CONFIG)
        config.__enter__()
        self.addCleanup(config.__exit__, None, None, None)

    def test_register_logs_each_unresolved_route(self):
        config = webhook_config(
            {
                "linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"},
                "github": {"secret": "${GITHUB_WEBHOOK_SECRET}"},
                "ok": {"secret": RESOLVED_SECRET},
            }
        )
        ctx = FakeContext()
        with hermes_config(config), self.assertLogs("linear_agent_session", "ERROR") as logs:
            plugin.register(ctx)
            hook = ctx.hooks[0][1]
            self.assertEqual(asyncio.run(hook(event=event(session_event("created")))), SKIP_LINEAR)
        startup = [line for line in logs.output if "will be dropped" in line]
        self.assertEqual(len(startup), 2)
        self.assertTrue(any("route linear" in line and "LINEAR_WEBHOOK_SECRET" in line for line in startup))
        self.assertTrue(any("route github" in line and "GITHUB_WEBHOOK_SECRET" in line for line in startup))
        self.assertNotIn(RESOLVED_SECRET, "\n".join(logs.output))

    def test_registers_tools_and_hook(self):
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertEqual(
            sorted(ctx.tools),
            [
                "linear_agent_activity",
                "linear_agent_session_create",
                "linear_delegated_issues",
                "linear_issue",
                "linear_issue_undelegate",
            ],
        )
        for name, tool in ctx.tools.items():
            self.assertEqual(tool["toolset"], "linear_agent_session")
            self.assertEqual(tool["schema"]["name"], name)
        self.assertEqual(
            [name for name, _ in ctx.hooks],
            ["pre_gateway_dispatch", "kanban_task_blocked", "transform_api_error_classification"],
        )

    def test_register_names_an_empty_provider_key_in_a_worker(self):
        config = {"model": {"provider": "custom:proxy"}, "providers": {"proxy": {"key_env": "PROXY_TOKEN"}}}
        ctx = FakeContext()
        environ = {"HERMES_KANBAN_TASK": "t_1234", "PROXY_TOKEN": ""}
        with hermes_config(config), mock.patch.dict(os.environ, environ), self.assertLogs(
            "linear_agent_session", "ERROR"
        ) as logs:
            plugin.register(ctx)
            classify = dict(ctx.hooks)["transform_api_error_classification"]
            self.assertEqual(classify(status_code=401)["reason"], "timeout")
        self.assertEqual(
            logs.output,
            [
                "ERROR:linear_agent_session:linear-agent-session: PROXY_TOKEN is empty in this worker; "
                "a provider 401 or 403 requeues the card"
            ],
        )

    def test_register_is_quiet_when_the_provider_key_is_present(self):
        config = {"model": {"provider": "custom:proxy"}, "providers": {"proxy": {"key_env": "PROXY_TOKEN"}}}
        ctx = FakeContext()
        environ = {"HERMES_KANBAN_TASK": "t_1234", "PROXY_TOKEN": "a-real-token"}
        with hermes_config(config), mock.patch.dict(os.environ, environ), self.assertNoLogs(
            "linear_agent_session", "ERROR"
        ):
            plugin.register(ctx)
            classify = dict(ctx.hooks)["transform_api_error_classification"]
            self.assertIsNone(classify(status_code=401))

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
        with mock.patch.dict(os.environ, {"LINEAR_CLIENT_ID": ""}):
            output = ctx.tools["linear_issue"]["handler"]({"issue": "ABC-1"})
        self.assertEqual(json.loads(output), {"error": "set LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET"})

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
                "HERMES_HOME": tmp,
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

    def test_delegated_and_undelegate_commands(self):
        page = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
        code, output = self.run_cli(["delegated", "--include-closed"], FakeLinear([(200, {"data": {"issues": page}})]))
        self.assertEqual((code, output), (0, {"issues": []}))
        issue = {"id": "issue-uuid", "identifier": "ABC-1", "delegate": None}
        fake = FakeLinear([(200, {"data": {"viewer": {"id": "app-user-1"}, "issue": issue}})])
        code, output = self.run_cli(["undelegate", "ABC-1"], fake)
        self.assertEqual((code, output), (0, {"identifier": "ABC-1", "removed": False}))


def _load_cli():
    import importlib.util

    from tests import PLUGIN_DIR

    spec = importlib.util.spec_from_file_location("linear_agent_session_cli", os.path.join(PLUGIN_DIR, "cli.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
