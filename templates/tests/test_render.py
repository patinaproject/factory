from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
SELF_FILTER = {
    "any": [
        {"field": "event", "in": ["check_run", "check_suite", "workflow_run"]},
        {
            "all": [
                {"field": "event", "equals": "pull_request"},
                {"field": "action", "equals": "closed"},
                {"field": "pull_request.merged", "equals": True},
            ]
        },
        {"not": {"field": "sender.login", "equals": "example-app[bot]"}},
    ]
}
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


PROMPTED_EVENT = {
    "type": "AgentSessionEvent",
    "action": "prompted",
    "agentSession": {
        "id": "00000000-0000-4000-8000-0000000000a1",
        "issue": {"id": "00000000-0000-4000-8000-0000000000b1", "identifier": "ABC-1"},
    },
    "agentActivity": {"signal": None, "content": {"type": "prompt", "body": "close"}},
}


def render_like_hermes(template: str, payload: dict) -> str:
    """Fill {dot.path} placeholders the way the Hermes webhook adapter does: a missing key stays as written."""

    def resolve(match: re.Match) -> str:
        value = payload
        for part in match.group(1).split("."):
            if not isinstance(value, dict) or part not in value:
                return match.group(0)
            value = value[part]
        return str(value)

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}", resolve, template)


class LinearPayloadTest(unittest.TestCase):
    def test_a_prompted_reply_reaches_the_prompt(self):
        prompt = render_like_hermes(render.build_routes(SETTINGS)["linear"]["prompt"], PROMPTED_EVENT)

        self.assertIn("- Activity body: close\n", prompt)
        self.assertIn("- Activity signal: None\n", prompt)


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

    def test_own_pull_requests_match_both_forms_of_the_app_login(self):
        self.assertIn(
            "A pull request is the factory's own when its `author.login` is `example-app[bot]` or `app/example-app`.",
            self.prompts["linear"],
        )

    def test_without_an_app_login_no_pull_request_is_the_factorys_own(self):
        routes = render.build_routes(settings(github={"login": ""}))
        self.assertIn(
            "No GitHub App login is configured, so every open pull request counts as someone else's.",
            routes["linear"]["prompt"],
        )
        self.assertNotIn("`author.login` is", routes["linear"]["prompt"])

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
        "kanban_url 7 must be a string": settings(kanban_url=7),
        "github.login 7 must be a string": settings(github={"login": 7}),
    }

    def test_each_invalid_setting_is_rejected(self):
        for message, value in self.CASES.items():
            with self.subTest(message=message):
                with self.assertRaises(render.SettingsError) as raised:
                    render.build_routes(value)
                self.assertIn(message, str(raised.exception))

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


SECRETS = {"LINEAR_WEBHOOK_SECRET": "linear-signing-secret-0123456789", "GITHUB_WEBHOOK_SECRET": "github-signing-secret-9876543210"}

# Stands in for `hermes config get <route> --json --raw`, which expands ${VAR} into the live secret.
FAKE_HERMES = """#!PYTHON
import json, os, re, sys
name = sys.argv[3].rsplit(".", 1)[1]
path = os.path.join(os.environ["HERMES_HOME"], "routes", name + ".json")
if not os.path.exists(path):
    sys.exit("Config key not set: " + sys.argv[3])
secrets = json.loads(os.environ["FAKE_SECRETS"])
text = open(path).read()
print(re.sub(r"\\$\\{([A-Za-z_]+)\\}", lambda m: secrets.get(m.group(1), m.group(0)), text))
"""


class RouteCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        hermes = bin_dir / "hermes"
        hermes.write_text(FAKE_HERMES.replace("PYTHON", sys.executable))
        hermes.chmod(0o755)
        (self.tmp / "routes").mkdir()
        self.routes = render.build_routes(SETTINGS)
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "HERMES_HOME": str(self.tmp),
            "FAKE_SECRETS": json.dumps(SECRETS),
        }

    def install(self, name, route):
        (self.tmp / "routes" / f"{name}.json").write_text(json.dumps(route))

    def write_config(self, text):
        (self.tmp / "config.yaml").write_text(text)

    def check(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(SETTINGS, f)
        self.addCleanup(Path(f.name).unlink)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render.main(["--check", "--settings-json", f.name])
        printed = out.getvalue() + err.getvalue()
        for secret in SECRETS.values():
            self.assertNotIn(secret, printed)
        return code, out.getvalue().splitlines()

    def install_rendered(self):
        for name, route in self.routes.items():
            self.install(name, route)
        self.write_config("secret: ${LINEAR_WEBHOOK_SECRET}\nsecret: ${GITHUB_WEBHOOK_SECRET}\n")

    def test_installed_routes_with_references_match(self):
        self.install_rendered()

        self.assertEqual(self.check(), (0, [
            "route linear: matches; secret is the ${LINEAR_WEBHOOK_SECRET} reference",
            "route github: matches; secret is the ${GITHUB_WEBHOOK_SECRET} reference",
        ]))

    def test_a_changed_route_names_the_differing_keys(self):
        self.install_rendered()
        self.install("github", {**self.routes["github"], "prompt": "old prompt", "events": ["issues"]})

        code, lines = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(lines[1], "route github: differs in events, prompt; secret is the ${GITHUB_WEBHOOK_SECRET} reference")

    def test_a_literal_secret_in_config_is_reported_without_its_value(self):
        self.install_rendered()
        self.install("linear", {**self.routes["linear"], "secret": SECRETS["LINEAR_WEBHOOK_SECRET"]})
        self.write_config(f"secret: {SECRETS['LINEAR_WEBHOOK_SECRET']}\nsecret: ${{GITHUB_WEBHOOK_SECRET}}\n")

        code, lines = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "route linear: matches; secret is a literal value in config.yaml, not the ${LINEAR_WEBHOOK_SECRET} reference")

    def test_a_reference_without_a_value_is_reported(self):
        self.install_rendered()
        self.env["FAKE_SECRETS"] = json.dumps({"GITHUB_WEBHOOK_SECRET": SECRETS["GITHUB_WEBHOOK_SECRET"]})

        code, lines = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "route linear: matches; secret is the reference ${LINEAR_WEBHOOK_SECRET}, but Hermes has no value for it")

    def test_a_missing_route_is_reported(self):
        self.install_rendered()
        (self.tmp / "routes" / "github.json").unlink()

        self.assertEqual(self.check(), (1, [
            "route linear: matches; secret is the ${LINEAR_WEBHOOK_SECRET} reference",
            "route github: not installed",
        ]))


if __name__ == "__main__":
    unittest.main()
