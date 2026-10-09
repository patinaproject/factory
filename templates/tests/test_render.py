from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("render", Path(__file__).resolve().parent.parent / "render.py")
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)

SETTINGS = {
    "app_user_id": "00000000-0000-4000-8000-000000000001",
    "webhook_route": "linear",
    "repositories": [
        {"full_name": "example-org/app", "path": "/srv/checkouts/app", "routing": "default", "worker_entry": ""},
        {"full_name": "example-org/site", "path": "/srv/checkouts/site", "routing": "synced_github", "worker_entry": ""},
    ],
    "github": {
        "login": "example-app[bot]",
        "app_id": "12345",
        "installation_id": 67890,
        "private_key_path": "/srv/keys/example-app.pem",
    },
}

GITHUB_EVENTS = [
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "workflow_run",
    "check_run",
    "check_suite",
]
FAILED = ["failure", "timed_out", "action_required", "startup_failure"]
REPO_FILTER = {"field": "repository.full_name", "in": ["example-org/app", "example-org/site"]}
CHECK_FILTER = {
    "any": [
        {"not": {"field": "event", "in": ["check_run", "check_suite", "workflow_run"]}},
        {"field": "check_run.conclusion", "in": FAILED},
        {"field": "check_suite.conclusion", "in": FAILED},
        {"field": "workflow_run.conclusion", "in": FAILED},
    ]
}
SELF_FILTER = {"not": {"field": "sender.login", "equals": "example-app[bot]"}}
LINEAR_FILTERS = [
    {
        "any": [
            {"field": "event", "equals": "AgentSessionEvent"},
            {"field": "updatedFrom.delegateId", "exists": True},
            {
                "all": [
                    {"field": "updatedFrom.stateId", "exists": True},
                    {"field": "data.state.type", "in": ["completed", "canceled"]},
                ]
            },
        ]
    }
]
TOOLSETS = ["terminal", "kanban", "linear_agent_session"]
PLACEHOLDER = re.compile(r"\{[a-zA-Z0-9_.]+\}")


def settings(**changes):
    value = copy.deepcopy(SETTINGS)
    value.update(changes)
    return value


def with_repo(index, **changes):
    value = copy.deepcopy(SETTINGS)
    value["repositories"][index].update(changes)
    return value


class RouteStructureTest(unittest.TestCase):
    def setUp(self):
        self.routes = render.build_routes(SETTINGS)

    def test_renders_one_route_per_source(self):
        self.assertEqual(sorted(self.routes), ["github", "linear"])

    def test_linear_route(self):
        route = {k: v for k, v in self.routes["linear"].items() if k != "prompt"}
        self.assertEqual(
            route,
            {
                "events": ["AgentSessionEvent", "Issue"],
                "secret": "${LINEAR_WEBHOOK_SECRET}",
                "filters": LINEAR_FILTERS,
                "toolsets": TOOLSETS,
            },
        )

    def test_github_route(self):
        route = {k: v for k, v in self.routes["github"].items() if k != "prompt"}
        self.assertEqual(
            route,
            {
                "events": GITHUB_EVENTS,
                "secret": "${GITHUB_WEBHOOK_SECRET}",
                "filters": [REPO_FILTER, CHECK_FILTER, SELF_FILTER],
                "toolsets": TOOLSETS,
            },
        )

    def test_github_route_without_login_has_no_self_filter(self):
        routes = render.build_routes(
            settings(github={"login": "", "app_id": "", "installation_id": "", "private_key_path": ""})
        )
        self.assertEqual(routes["github"]["filters"], [REPO_FILTER, CHECK_FILTER])

    def test_linear_route_name_follows_settings(self):
        routes = render.build_routes(settings(webhook_route="linear-agent"))
        self.assertEqual(sorted(routes), ["github", "linear-agent"])


class PromptSubstitutionTest(unittest.TestCase):
    def setUp(self):
        routes = render.build_routes(SETTINGS)
        self.prompts = {name: routes[name]["prompt"] for name in routes}

    def test_prompts_list_every_repository(self):
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertIn(
                    "- `example-org/app`: routing `default`, checkout `/srv/checkouts/app`, card flags "
                    "`--workspace worktree --completion-contract example-org/app`",
                    prompt,
                )
                self.assertIn(
                    "- `example-org/site`: routing `synced_github`, checkout `/srv/checkouts/site`, card flags "
                    "`--workspace worktree:/srv/checkouts/site --completion-contract example-org/site`",
                    prompt,
                )

    def test_queued_activity_links_the_kanban_board_when_configured(self):
        routes = render.build_routes(settings(kanban_url="https://factory.example.com/kanban"))
        self.assertIn(
            "labeled `Kanban card <task id>` whose URL is `https://factory.example.com/kanban`.",
            routes["linear"]["prompt"],
        )
        self.assertIn("Leave `external_urls` unset.", self.prompts["linear"])

    def test_linear_prompt_names_the_app_user(self):
        self.assertIn("00000000-0000-4000-8000-000000000001", self.prompts["linear"])

    def test_prompts_keep_only_hermes_placeholders(self):
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertIn("{__raw__}", prompt)
                self.assertNotRegex(prompt, r"<<[a-z_]+>>")
                self.assertNotIn("${", prompt)
                stray = PLACEHOLDER.sub("", prompt)
                self.assertNotIn("{", stray)
                self.assertNotIn("}", stray)


class ValidationTest(unittest.TestCase):
    CASES = {
        "settings must be an object": [],
        "app_user_id must be a non-empty string": settings(app_user_id=" "),
        "webhook_route must be a non-empty string": settings(webhook_route=None),
        "webhook_route 'lin ear' may contain only": settings(webhook_route="lin ear"),
        "webhook_route must not be 'github'": settings(webhook_route="github"),
        "repositories must be a non-empty list": settings(repositories=[]),
        "repositories[0] must be an object": settings(repositories=["example-org/app"]),
        "repositories[1].full_name 'site' must look like owner/name": with_repo(1, full_name="site"),
        "repositories[1].full_name 'Example-Org/App' is listed more than once": with_repo(
            1, full_name="Example-Org/App"
        ),
        "repositories[0].path 'checkouts/app' must be an absolute path": with_repo(0, path="checkouts/app"),
        "repositories[1].routing 'github' must be one of default, synced_github": with_repo(1, routing="github"),
        "exactly one repository must have routing 'default', found 2": with_repo(1, routing="default"),
        "exactly one repository must have routing 'default', found 0": with_repo(0, routing="synced_github"),
        "github must be an object": settings(github="example-factory-bot"),
        "kanban_url 'factory/kanban' must be empty or an http(s) URL": settings(kanban_url="factory/kanban"),
        "github.login 'not a login' must be empty, a GitHub login, or an App's <name>[bot] login": settings(
            github={"login": "not a login"}
        ),
        "github.app_id 'example' must be empty or a numeric ID": settings(github={"app_id": "example"}),
        "github.installation_id True must be empty or a numeric ID": settings(github={"installation_id": True}),
        "github.private_key_path 'keys/app.pem' must be empty or an absolute path": settings(
            github={"private_key_path": "keys/app.pem"}
        ),
        "add Hermes payload placeholders to the prompt: {action}": with_repo(0, path="/srv/{action}"),
        "contains a brace outside a Hermes payload placeholder": with_repo(0, path="/srv/a}b"),
    }

    def test_each_invalid_setting_is_rejected(self):
        for message, value in self.CASES.items():
            with self.subTest(message=message):
                with self.assertRaises(render.SettingsError) as raised:
                    render.build_routes(value)
                self.assertIn(message, str(raised.exception))

    def test_template_without_raw_payload_is_rejected(self):
        with self.assertRaisesRegex(render.SettingsError, "must contain"):
            render.render_prompt("Event {type}", {})

    def test_template_with_env_reference_is_rejected(self):
        with self.assertRaisesRegex(render.SettingsError, "expands as an environment variable"):
            render.render_prompt("Event ${type} {__raw__}", {})

    def test_template_marker_without_value_is_rejected(self):
        with self.assertRaisesRegex(render.SettingsError, "<<missing>> has no value"):
            render.render_prompt("<<missing>> {__raw__}", {})


class CommandLineTest(unittest.TestCase):
    def run_main(self, value):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(value, f)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render.main(["--settings-json", f.name])
        Path(f.name).unlink()
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_routes_as_json(self):
        code, out, err = self.run_main(SETTINGS)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), render.build_routes(SETTINGS))

    def test_invalid_settings_exit_nonzero_with_the_reason(self):
        code, out, err = self.run_main(settings(repositories=[]))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "render.py: invalid settings: repositories must be a non-empty list\n")


if __name__ == "__main__":
    unittest.main()
