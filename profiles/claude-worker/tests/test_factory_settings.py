from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import factory_settings  # noqa: E402

CREDENTIALS = REPO / "plugins" / "factory-uploads" / "credentials.py"
MANIFEST = REPO / "plugins" / "factory-secret-names" / "plugin.yaml"

SERVER = {"url": "https://mcp.example/mcp", "token_env": "SENTRY_ACCESS_TOKEN"}


def settings(transport: dict | None = None, **repository) -> dict:
    return {"claude_session": transport or {}, "repositories": [{"full_name": "o/r", "path": "/r", **repository}]}


class ValidateTest(unittest.TestCase):
    def test_settings_that_run_have_no_problems(self) -> None:
        problems = factory_settings.validate(settings({
            "fresh_after_tokens": 50_000, "max_turns": 0, "tier_models": {"sonnet": "m"},
            "mcp_servers": {"sentry": SERVER}, "disabled_mcp_servers": ["figma"],
        }, worker_entry="/patina-mode", toolchain_checks=["node --version"]))
        self.assertEqual(problems, factory_settings.Problems((), {}))

    def test_every_global_value_that_refuses_a_run_is_reported_in_field_order(self) -> None:
        problems = factory_settings.validate(settings({
            "fresh_after_tokens": -1, "max_turns": "x", "tier_models": ["sonnet"],
            "mcp_servers": {"linear": SERVER}, "disabled_mcp_servers": "figma",
        }))
        self.assertEqual(problems.global_errors, (
            "claude_session.fresh_after_tokens -1 must be a positive integer",
            "claude_session.max_turns 'x' must be a positive integer, or 0 for no limit",
            "claude_session.tier_models ['sonnet'] must map sonnet, haiku, fable to non-empty model ids",
            "claude_session.mcp_servers.linear: the factory owns the linear server",
            "claude_session.disabled_mcp_servers 'figma' must be a list of MCP server names",
        ))

    def test_the_replaced_sentry_setting_is_reported(self) -> None:
        problems = factory_settings.validate(settings({"sentry_token_env": "X"}))
        self.assertEqual(
            problems.global_errors, ("claude_session.sentry_token_env is replaced by claude_session.mcp_servers.sentry",)
        )

    def test_repository_errors_are_keyed_by_full_name_and_reach_only_that_repository(self) -> None:
        both = {"claude_session": {"max_turns": -2}, "repositories": [
            {"full_name": "o/bad", "path": "/nonexistent", "worker_entry": "{x}", "toolchain_checks": "make"},
            {"full_name": "o/good", "path": "/nonexistent"},
        ]}
        problems = factory_settings.validate(both)
        self.assertEqual(problems.repository_errors, {"o/bad": (
            "worker_entry '{x}' must be a plain prefix such as /patina-mode; "
            "claude-session adds the issue, URL, repository, and branch itself",
            "toolchain_checks 'make' must be a list of shell commands",
        )})
        global_error = "claude_session.max_turns -2 must be a positive integer, or 0 for no limit"
        self.assertEqual(problems.for_repository("o/good"), (global_error,))
        self.assertEqual(problems.for_repository("o/bad"), (global_error, *problems.repository_errors["o/bad"]))
        self.assertEqual(problems.for_repository("o/unknown"), (global_error,))

    def test_a_claude_session_that_is_not_an_object_counts_as_empty(self) -> None:
        self.assertEqual(factory_settings.validate({"claude_session": "x", "repositories": None}).global_errors, ())

    def test_a_missing_turn_limit_and_zero_mean_no_limit(self) -> None:
        limits = [factory_settings.max_turns(transport) for transport in ({}, {"max_turns": 0}, {"max_turns": 7})]
        self.assertEqual(limits, [None, None, 7])

    def test_the_default_fresh_threshold_applies_when_unset(self) -> None:
        self.assertEqual(factory_settings.fresh_after_tokens({}), 100_000)


class NoValueInErrorTest(unittest.TestCase):
    def test_no_error_quotes_a_refused_credential_shape(self) -> None:
        prefixes = (
            "ghp_", "gho_", "ghs_", "ghu_", "github_pat_", "glpat-", "sntrys_", "sntryu_", "lin_api_", "phx_", "xoxb-", "xoxp-",
        )
        planted = [f"{prefix}SECRETVALUE" for prefix in prefixes]
        planted += ["sk-SECRETVALUE" + "a" * 20, "Bearer SECRETVALUE", "Basic SECRETVALUE"]
        cases = []
        for value in planted:
            cases.append({"headers": {"X-Region": value}})
            cases.append({"url": f"https://mcp.example/mcp?region={value}"})
        cases += [
            {"url": "https://user:SECRETVALUE@mcp.example/mcp"},
            {"url": "https://mcp.example/mcp?api_key=SECRETVALUE"},
            {"headers": {"X-Api-Key": "SECRETVALUE"}},
        ]
        for case in cases:
            with self.subTest(case=case):
                server = {"url": "https://mcp.example/mcp", "token_env": "T", **case}
                problems = factory_settings.validate(settings({"mcp_servers": {"s": server}}))
                self.assertEqual(len(problems.global_errors), 1)
                self.assertNotIn("SECRETVALUE", problems.global_errors[0])


class TokenVariablesTest(unittest.TestCase):
    def test_the_gateway_broker_serves_the_variables_this_module_names(self) -> None:
        transport = {"auth_token_env": "CLAUDE_PROXY_TOKEN", "mcp_servers": {
            "sentry": SERVER, "empty": {**SERVER, "token_env": ""}, "typo": "x",
        }}
        auth, servers = factory_settings.token_variables({"claude_session": transport})
        self.assertEqual((auth, servers), ("CLAUDE_PROXY_TOKEN", {"sentry": "SENTRY_ACCESS_TOKEN"}))
        spec = importlib.util.spec_from_file_location("factory_uploads_credentials", CREDENTIALS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            credentials = module.GatewayCredentials(lambda name: name, transport, Path(tmp) / "linear.py", Path(tmp), MANIFEST)
            self.assertEqual(credentials.proxy_token(), auth)
            served = {name: credentials.mcp_token(name) for name in ("sentry", "empty", "typo")}
        self.assertEqual(served, {"sentry": servers["sentry"], "empty": None, "typo": None})

    def test_settings_without_a_claude_session_name_no_variables(self) -> None:
        self.assertEqual(factory_settings.token_variables({}), ("", {}))
        self.assertEqual(factory_settings.token_variables(None), ("", {}))


if __name__ == "__main__":
    unittest.main()
