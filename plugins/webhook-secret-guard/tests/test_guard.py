from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "webhook_secret_guard", Path(__file__).resolve().parent.parent / "__init__.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

RESOLVED_SECRET = "s3cret-resolved-value"
SKIP_LINEAR = {"action": "skip", "reason": "route linear secret is the unresolved reference ${LINEAR_WEBHOOK_SECRET}"}


def webhook_config(routes, global_secret=None):
    extra = {"routes": routes}
    if global_secret is not None:
        extra["secret"] = global_secret
    return {"platforms": {"webhook": {"extra": extra}}}


def delivery(route, platform="webhook"):
    return SimpleNamespace(
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            user_id=f"webhook:{route}" if platform == "webhook" else "U123",
            chat_name=f"webhook/{route}",
        )
    )


class FakeCtx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


class HookTest(unittest.TestCase):
    def hook(self, config):
        return guard.make_hook(lambda: config)

    def test_resolved_secret_is_accepted(self):
        config = webhook_config({"github": {"secret": RESOLVED_SECRET}})
        self.assertIsNone(self.hook(config)(delivery("github"), gateway=None, session_store=None))

    def test_unresolved_secret_is_skipped_and_logged_without_values(self):
        config = webhook_config(
            {"linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"}, "github": {"secret": RESOLVED_SECRET}}
        )
        with self.assertLogs(guard.logger, level="ERROR") as logs:
            result = self.hook(config)(delivery("linear"), gateway=None, session_store=None)
        self.assertEqual(result, SKIP_LINEAR)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("linear", logs.output[0])
        self.assertIn("LINEAR_WEBHOOK_SECRET", logs.output[0])
        self.assertNotIn(RESOLVED_SECRET, "\n".join(logs.output))

    def test_env_prefixed_reference_is_unresolved(self):
        config = webhook_config({"github": {"secret": "${env:GITHUB_WEBHOOK_SECRET}"}})
        with self.assertLogs(guard.logger, level="ERROR"):
            result = self.hook(config)(delivery("github"))
        self.assertEqual(
            result,
            {"action": "skip", "reason": "route github secret is the unresolved reference ${GITHUB_WEBHOOK_SECRET}"},
        )

    def test_route_without_secret_uses_global_fallback(self):
        unresolved = webhook_config({"linear": {}}, global_secret="${LINEAR_WEBHOOK_SECRET}")
        with self.assertLogs(guard.logger, level="ERROR"):
            self.assertEqual(self.hook(unresolved)(delivery("linear")), SKIP_LINEAR)
        resolved = webhook_config({"linear": {}}, global_secret=RESOLVED_SECRET)
        self.assertIsNone(self.hook(resolved)(delivery("linear")))

    def test_route_secret_overrides_unresolved_global(self):
        config = webhook_config({"linear": {"secret": RESOLVED_SECRET}}, global_secret="${WEBHOOK_SECRET}")
        self.assertIsNone(self.hook(config)(delivery("linear")))

    def test_secret_that_only_contains_a_reference_is_resolved(self):
        config = webhook_config({"linear": {"secret": "prefix-${LINEAR_WEBHOOK_SECRET}"}})
        self.assertIsNone(self.hook(config)(delivery("linear")))

    def test_non_webhook_event_is_accepted(self):
        config = webhook_config({"linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"}})
        self.assertIsNone(self.hook(config)(delivery("linear", platform="slack")))

    def test_route_absent_from_config_is_accepted(self):
        config = webhook_config({"linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"}})
        self.assertIsNone(self.hook(config)(delivery("dynamic-route")))

    def test_config_without_webhook_platform_is_accepted(self):
        self.assertIsNone(self.hook({})(delivery("linear")))

    def test_config_is_read_per_delivery(self):
        configs = [
            webhook_config({"linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"}}),
            webhook_config({"linear": {"secret": RESOLVED_SECRET}}),
        ]
        hook = guard.make_hook(lambda: configs.pop(0))
        with self.assertLogs(guard.logger, level="ERROR"):
            self.assertEqual(hook(delivery("linear")), SKIP_LINEAR)
        self.assertIsNone(hook(delivery("linear")))


class RegisterTest(unittest.TestCase):
    def test_register_logs_unresolved_routes_and_wires_the_hook(self):
        config = webhook_config(
            {
                "linear": {"secret": "${LINEAR_WEBHOOK_SECRET}"},
                "github": {"secret": "${GITHUB_WEBHOOK_SECRET}"},
                "ok": {"secret": RESOLVED_SECRET},
            }
        )
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_config = types.ModuleType("hermes_cli.config")
        hermes_config.load_config_readonly = lambda: config
        hermes_cli.config = hermes_config
        ctx = FakeCtx()
        with mock.patch.dict(sys.modules, {"hermes_cli": hermes_cli, "hermes_cli.config": hermes_config}):
            with self.assertLogs(guard.logger, level="ERROR") as logs:
                guard.register(ctx)
            self.assertEqual(len(logs.output), 2)
            self.assertTrue(any("linear" in line and "LINEAR_WEBHOOK_SECRET" in line for line in logs.output))
            self.assertTrue(any("github" in line and "GITHUB_WEBHOOK_SECRET" in line for line in logs.output))
            self.assertNotIn(RESOLVED_SECRET, "\n".join(logs.output))

            self.assertEqual([name for name, _ in ctx.hooks], ["pre_gateway_dispatch"])
            callback = ctx.hooks[0][1]
            with self.assertLogs(guard.logger, level="ERROR"):
                self.assertEqual(callback(delivery("linear"), gateway=None, session_store=None), SKIP_LINEAR)
            self.assertIsNone(callback(delivery("ok"), gateway=None, session_store=None))


if __name__ == "__main__":
    unittest.main()
