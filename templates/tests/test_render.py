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
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
    "workflow_run",
    "check_run",
    "issues",
]
FAILED = ["failure", "timed_out", "action_required", "startup_failure"]
APP_PR = {"field": "pull_request.user.login", "equals": "example-app[bot]"}
OWNED_BRANCH_PR = {"field": "pull_request.head.ref", "in_file": "owned-refs.json"}
OWNED_ISSUE = {"field": "issue.html_url", "in_file": "owned-refs.json"}
REPO_FILTER = {"field": "repository.full_name", "in": ["example-org/app", "example-org/site"]}
EVENT_FILTER = {
    "any": [
        {
            "all": [
                {"field": "event", "equals": "pull_request"},
                {"field": "action", "in": ["closed", "reopened", "converted_to_draft"]},
                APP_PR,
                OWNED_BRANCH_PR,
            ]
        },
        {
            "all": [
                {"field": "event", "equals": "pull_request_review"},
                {"field": "action", "in": ["submitted"]},
                APP_PR,
                OWNED_BRANCH_PR,
            ]
        },
        {
            "all": [
                {"field": "event", "equals": "pull_request_review_comment"},
                {"field": "action", "in": ["created"]},
                APP_PR,
                OWNED_BRANCH_PR,
            ]
        },
        {
            "all": [
                {"field": "event", "equals": "issue_comment"},
                {"field": "action", "in": ["created"]},
                {"not": {"field": "comment.body", "regex": r"\A<!-- linear-linkback -->"}},
                {"not": {"field": "comment.user.login", "in": ["cloudflare-workers-and-pages[bot]", "vercel[bot]", "github-actions[bot]"]}},
                {
                    "any": [
                        {
                            "all": [
                                {"field": "issue.pull_request", "exists": True},
                                {"field": "issue.user.login", "equals": "example-app[bot]"},
                            ]
                        },
                        OWNED_ISSUE,
                    ]
                },
            ]
        },
        {
            "all": [
                {"field": "event", "equals": "workflow_run"},
                {"field": "action", "in": ["completed"]},
                {"field": "workflow_run.triggering_actor.login", "equals": "example-app[bot]"},
                {"field": "workflow_run.head_branch", "in_file": "owned-refs.json"},
                {"field": "workflow_run.conclusion", "in": FAILED},
            ]
        },
        {
            "all": [
                {"field": "event", "equals": "check_run"},
                {"field": "action", "in": ["completed"]},
                {"field": "check_run.check_suite.head_branch", "in_file": "owned-refs.json"},
                {"field": "check_run.app.slug", "not_equals": "github-actions"},
                {"field": "check_run.conclusion", "in": FAILED},
            ]
        },
        {
            "all": [
                {"field": "event", "equals": "issues"},
                {"field": "action", "in": ["closed", "reopened", "edited"]},
                OWNED_ISSUE,
            ]
        },
    ]
}
SELF_FILTER = {
    "any": [
        {"field": "event", "in": ["check_run", "workflow_run"]},
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
                    {"field": "data.id", "in_file": "owned-refs.json"},
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
                "filters": [REPO_FILTER, EVENT_FILTER, SELF_FILTER],
                "toolsets": TOOLSETS,
            },
        )

    def test_route_events_list_matches_the_table(self):
        self.assertEqual(self.routes["github"]["events"], [row.name for row in render.GITHUB_EVENT_TABLE])

    def test_prompt_list_and_filter_cover_the_same_event_actions(self):
        prompt = self.routes["github"]["prompt"]
        listed = set()
        for match in re.finditer(r"^- `([a-z_]+)` with action ((?:`[a-z_]+`(?:, |, or | or )?)+), only when ", prompt, re.M):
            listed.update((match.group(1), action) for action in re.findall(r"`([a-z_]+)`", match.group(2)))
        filtered = set()
        for clause in self.routes["github"]["filters"][1]["any"]:
            event, actions = clause["all"][0], clause["all"][1]
            self.assertEqual((event["field"], actions["field"]), ("event", "action"))
            filtered.update((event["equals"], action) for action in actions["in"])

        self.assertTrue(listed)
        self.assertEqual(listed, filtered)
        self.assertEqual({event for event, _ in listed}, {row.name for row in render.GITHUB_EVENT_TABLE})

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

    def test_the_linear_prompt_runs_card_branch_from_the_worker_profile_as_the_app(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/srv/hermes"}):
            prompt = render.build_routes(SETTINGS)["linear"]["prompt"]
        self.assertIn("python3 /srv/hermes/profiles/claude-worker/scripts/card_branch.py --app 'example-app[bot]'", prompt)

    def test_linear_prompt_names_the_app_user(self):
        self.assertIn("00000000-0000-4000-8000-000000000001", self.prompts["linear"])

    def test_both_triage_prompts_carry_the_one_resume_section(self):
        section = render.triage_resume_section()
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertEqual(prompt.count(section), 1)

    def test_the_resume_section_runs_the_kanban_commands_in_order(self):
        commands = [
            "hermes kanban show <task id> --json",
            "hermes kanban assign <task id> none",
            "hermes kanban specify <task id>",
            "hermes kanban edit <task id> --title",
            "hermes kanban assign <task id> claude-worker",
        ]
        section = render.triage_resume_section()
        positions = [section.rindex(command) for command in commands]
        self.assertEqual(positions, sorted(positions))
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertIn(section, prompt)

    def test_prompts_keep_only_hermes_placeholders(self):
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertIn("{__raw__}", prompt)
                self.assertNotRegex(prompt, r"<<[a-z_]+>>")
                self.assertNotIn("${", prompt)
                stray = PLACEHOLDER.sub("", prompt)
                self.assertNotIn("{", stray)
                self.assertNotIn("}", stray)


class MergedCardTest(unittest.TestCase):
    """The merged-pull-request rule is machine input to the triage agent, so its commands and their order are a contract."""

    def setUp(self):
        routes = render.build_routes(SETTINGS)
        self.section = render.triage_merged_section()
        self.github = routes["github"]["prompt"]
        self.linear = routes["linear"]["prompt"]

    def bullet(self, status: str) -> str:
        match = re.search(rf"^- `{status}`.*?(?=^- |\Z)", self.section, re.M | re.S)
        self.assertIsNotNone(match, f"no rule for a {status} card")
        return match.group(0)

    def positions(self, text: str, commands: list) -> list:
        for command in commands:
            self.assertIn(command, text)
        return [text.index(command) for command in commands]

    def test_only_the_github_prompt_carries_the_section_once(self):
        self.assertEqual(self.github.count(self.section), 1)
        self.assertNotIn(self.section, self.linear)
        self.assertNotRegex(self.github, r"<<[a-z_]+>>")

    def test_the_merged_event_hands_every_status_to_the_section(self):
        self.assertRegex(self.github, r"`pull_request` action `closed` with merged `True`, complete the card as\s+\"Complete a card whose pull request merged\"")
        self.assertIn("merged `True`", self.section.split("\n\n")[1])
        self.assertIn("is not a merge", self.section)

    def test_each_status_has_one_rule(self):
        statuses = set(re.findall(r"^- `([a-z]+)`", self.section, re.M))
        self.assertEqual(statuses, {"running", "review", "todo", "triage"})
        self.assertRegex(self.bullet("review"), r"`review`, `blocked`, or `ready`")

    def test_a_running_card_is_left_to_its_worker(self):
        rule = self.bullet("running")
        self.assertNotIn("kanban_complete", rule)
        self.assertNotIn("hermes kanban complete", rule)
        self.assertNotIn("completion call", rule)
        self.assertIn("`running`: do nothing more.", rule)
        self.assertIn("Never complete, reclaim, or force a running card.", rule)

    def test_review_blocked_and_ready_cards_run_no_command_before_completion(self):
        rule = self.bullet("review")
        self.assertIn("completion call", rule)
        self.assertNotIn("hermes kanban", rule)

    def test_a_todo_card_is_promoted_before_it_is_completed(self):
        rule = self.bullet("todo")
        positions = self.positions(rule, ["hermes kanban promote <task id> pull request merged", "completion call"])
        self.assertEqual(positions, sorted(positions))

    def test_a_triage_card_runs_the_unpark_commands_in_order_then_completes(self):
        rule = self.bullet("triage")
        commands = [
            "hermes kanban show <task id> --json",
            "hermes kanban assign <task id> none",
            "hermes kanban specify <task id>",
            "hermes kanban edit <task id> --title",
            "hermes kanban promote <task id> pull request merged",
            "Make the completion call.",
        ]
        positions = [rule.index(command) for command in commands]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn(
            "hermes kanban assign <task id> claude-worker", rule[rule.index("Make the completion call.") :]
        )

    def test_the_completion_call_names_the_tool_and_its_arguments(self):
        call = self.section[self.section.index("The completion call is") :]
        for needle in ("kanban_complete", "`task_id`", "`summary`", "`published_pr`", "linear_agent_activity", "`Pull request`"):
            self.assertIn(needle, call)
        self.assertNotIn("claude-worker", call)

    def test_a_card_is_reassigned_to_the_worker_only_when_specify_fails(self):
        rule = self.bullet("triage")
        reassign = "hermes kanban assign <task id> claude-worker"
        self.assertEqual(rule.count(reassign), 1)
        specify = rule[rule.index("hermes kanban specify <task id>") :].split("\n  4. ")[0]
        self.assertIn(f"If it fails, run\n     `{reassign}`", specify)
        self.assertNotIn(reassign, self.section[self.section.index("  4. ") :])

    def test_a_refused_promote_or_completion_leaves_the_card_unassigned(self):
        rule = self.bullet("triage")
        promote = rule[rule.index("  5. ") : rule.index("  6. ")]
        self.assertIn("comment the refusal with the pull request URL and stop. Leave the card\n     unassigned", promote)
        self.assertNotIn("claude-worker", promote)
        call = self.section[self.section.index("The completion call is") :]
        self.assertIn("When it is refused, comment\nthe refusal with the pull request URL and stop.", call)
        self.assertIn("Never assign the card after\n`specify` succeeded.", call)

    def test_a_rewritten_card_is_never_completed(self):
        rule = self.bullet("triage")
        self.assertIn("Never complete the card while its title and body are the rewritten ones.", rule)

    def test_the_merge_step_no_longer_limits_completion_to_three_statuses(self):
        step = self.github[self.github.index("2. For `pull_request` action `closed`") :].split("\n3. ")[0]
        self.assertNotIn("when the card is", step)
        self.assertNotIn("kanban_complete", step)


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
    }
    NO_LOGIN = "github.login must be the App's login, such as my-app[bot]; the github route keeps only the App's own events"

    def test_a_missing_app_login_is_rejected(self):
        for name, value in {
            "missing github": settings(github=None),
            "missing login": settings(github={"app_id": "12345"}),
            "empty login": settings(github={"login": " "}),
            "non-string login": settings(github={"login": 7}),
        }.items():
            with self.subTest(name), self.assertRaises(render.SettingsError) as raised:
                render.build_routes(value)
            self.assertEqual(str(raised.exception), self.NO_LOGIN)

    def test_a_login_without_the_bot_suffix_is_rejected(self):
        with self.assertRaises(render.SettingsError) as raised:
            render.build_routes(settings(github={"login": "example-app"}))
        self.assertEqual(str(raised.exception), self.NO_LOGIN)

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

    def test_prints_each_config_key_with_its_value(self):
        code, out, err = self.run_main(SETTINGS)
        self.assertEqual((code, err), (0, ""))
        routes = render.build_routes(SETTINGS)
        self.assertEqual(json.loads(out), {
            "platforms.webhook.extra.rate_limit": 600,
            "platforms.webhook.extra.routes.linear": routes["linear"],
            "platforms.webhook.extra.routes.github": routes["github"],
        })

    def test_invalid_settings_exit_nonzero_with_the_reason(self):
        code, out, err = self.run_main(settings(repositories=[]))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "render.py: invalid settings: repositories must be a non-empty list\n")


SECRETS = {"LINEAR_WEBHOOK_SECRET": "linear-signing-secret-0123456789", "GITHUB_WEBHOOK_SECRET": "github-signing-secret-9876543210"}

# Stands in for `hermes config get <key> --json --raw`, which expands ${VAR} into the live secret.
FAKE_HERMES = """#!PYTHON
import json, os, re, sys
name = sys.argv[3].rsplit(".", 1)[1]
folder = "." if name == "rate_limit" else "routes"
path = os.path.join(os.environ["HERMES_HOME"], folder, name + ".json")
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
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.tmp)}):
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
        self.install_rate_limit(600)

    def install_rate_limit(self, value):
        (self.tmp / "rate_limit.json").write_text(json.dumps(value))

    def test_installed_routes_with_references_match(self):
        self.install_rendered()

        self.assertEqual(self.check(), (0, [
            "route linear: matches; secret is the ${LINEAR_WEBHOOK_SECRET} reference",
            "route github: matches; secret is the ${GITHUB_WEBHOOK_SECRET} reference",
            "rate_limit: matches 600",
        ]))

    def test_a_missing_rate_limit_is_reported(self):
        self.install_rendered()
        (self.tmp / "rate_limit.json").unlink()

        code, lines = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(lines[2], "rate_limit: not installed, so Hermes allows 30 a minute; install 600")

    def test_a_lower_rate_limit_is_reported(self):
        self.install_rendered()
        self.install_rate_limit(30)

        code, lines = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(lines[2], "rate_limit: 30 differs from 600")

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
            "rate_limit: matches 600",
        ]))


HERMES_AGENT_DIR = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent")
WEBHOOK_FILTERS = HERMES_AGENT_DIR / "gateway" / "platforms" / "webhook_filters.py"

APP = "example-app[bot]"
HUMAN = "octocat"
OWNED_BRANCH = "goodyear/pat-1-fix"
OWNED_ISSUE_URL = "https://github.com/example-org/site/issues/7"
OWNED_REFS = [OWNED_BRANCH, OWNED_ISSUE_URL]


def payload(action: str, sender: str = HUMAN, repo: str = "example-org/app", **fields) -> dict:
    return {"action": action, "repository": {"full_name": repo}, "sender": {"login": sender}, **fields}


def pull_request(author: str, merged: bool = False, branch: str = OWNED_BRANCH) -> dict:
    return {
        "number": 12,
        "state": "closed" if merged else "open",
        "user": {"login": author, "type": "Bot" if author.endswith("[bot]") else "User"},
        "merged": merged,
        "html_url": "https://github.com/example-org/app/pull/12",
        "head": {"ref": branch, "sha": "0" * 40},
        "base": {"ref": "main"},
    }


def issue(author: str, html_url: str = "https://github.com/example-org/app/pull/12", is_pull: bool = True) -> dict:
    fields = {"number": 12, "state": "open", "user": {"login": author}, "html_url": html_url}
    if is_pull:
        fields["pull_request"] = {"html_url": html_url, "merged_at": None}
    return fields


def comment(author: str, body: str) -> dict:
    return {
        "id": 1,
        "user": {"login": author, "type": "Bot" if author.endswith("[bot]") else "User"},
        "body": body,
        "html_url": "https://github.com/example-org/app/pull/12#issuecomment-1",
    }


LINEAR_BOT = "linear-code[bot]"
LINKBACK = '<!-- linear-linkback -->\n<p><a href="https://linear.app/example/issue/EX-1">EX-1</a></p>'
UNOWNED_BRANCH = "86-work-outside-the-board"
UNOWNED_ISSUE_URL = "https://github.com/example-org/app/issues/86"


def workflow_run(actor: str, conclusion: str, branch: str = OWNED_BRANCH) -> dict:
    return {
        "name": "CI",
        "conclusion": conclusion,
        "head_branch": branch,
        "triggering_actor": {"login": actor},
        "html_url": "https://github.com/example-org/app/actions/runs/1",
    }


def check_run(branch: str, conclusion: str, app_slug: str = "vercel") -> dict:
    return {
        "name": "Vercel",
        "conclusion": conclusion,
        "app": {"slug": app_slug},
        "check_suite": {"head_branch": branch},
        "html_url": "https://github.com/example-org/app/runs/1",
    }


FILTER_CASES = [
    ("review submitted on an App PR", "pull_request_review", payload("submitted", pull_request=pull_request(APP)), True),
    ("review submitted on a human PR", "pull_request_review", payload("submitted", pull_request=pull_request(HUMAN)), False),
    ("review edited on an App PR", "pull_request_review", payload("edited", pull_request=pull_request(APP)), False),
    ("review comment on an App PR", "pull_request_review_comment", payload("created", pull_request=pull_request(APP)), True),
    ("review comment on a human PR", "pull_request_review_comment", payload("created", pull_request=pull_request(HUMAN)), False),
    ("issue comment on an App PR", "issue_comment", payload("created", issue=issue(APP), comment=comment(HUMAN, "Please rename this.")), True),
    (
        "GitHub Actions comment on an App PR",
        "issue_comment",
        payload("created", sender="github-actions[bot]", issue=issue(APP), comment=comment("github-actions[bot]", "### Diff")),
        False,
    ),
    (
        "Vercel deploy notice on an App PR",
        "issue_comment",
        payload("created", sender="vercel[bot]", issue=issue(APP), comment=comment("vercel[bot]", "[vc]: #abc")),
        False,
    ),
    (
        "Cloudflare deploy notice on an App PR",
        "issue_comment",
        payload(
            "created",
            sender="cloudflare-workers-and-pages[bot]",
            issue=issue(APP),
            comment=comment("cloudflare-workers-and-pages[bot]", "## Deploying with Cloudflare Workers"),
        ),
        False,
    ),
    (
        "comment that quotes the linkback marker",
        "issue_comment",
        payload("created", issue=issue(APP), comment=comment(HUMAN, "Why is `<!-- linear-linkback -->` here?")),
        True,
    ),
    (
        "Linear linkback on an App PR",
        "issue_comment",
        payload("created", sender=LINEAR_BOT, issue=issue(APP), comment=comment(LINEAR_BOT, LINKBACK)),
        False,
    ),
    (
        "Linear linkback on a card's GitHub issue",
        "issue_comment",
        payload(
            "created",
            sender=LINEAR_BOT,
            repo="example-org/site",
            issue=issue(HUMAN, OWNED_ISSUE_URL, is_pull=False),
            comment=comment(LINEAR_BOT, LINKBACK),
        ),
        False,
    ),
    (
        "comment on an issue the App opened that no card has",
        "issue_comment",
        payload("created", issue=issue(APP, UNOWNED_ISSUE_URL, is_pull=False), comment=comment(HUMAN, "Done?")),
        False,
    ),
    (
        "review submitted on an App PR from a branch no card has",
        "pull_request_review",
        payload("submitted", pull_request=pull_request(APP, branch=UNOWNED_BRANCH), review={"state": "approved"}),
        False,
    ),
    (
        "review comment on an App PR from a branch no card has",
        "pull_request_review_comment",
        payload("created", pull_request=pull_request(APP, branch=UNOWNED_BRANCH), comment=comment(HUMAN, "Nit.")),
        False,
    ),
    (
        "the App merges its PR from a branch no card has",
        "pull_request",
        payload("closed", sender=APP, pull_request=pull_request(APP, merged=True, branch=UNOWNED_BRANCH)),
        False,
    ),
    ("issue comment on a human PR", "issue_comment", payload("created", issue=issue(HUMAN)), False),
    (
        "issue comment on a card's GitHub issue",
        "issue_comment",
        payload("created", repo="example-org/site", issue=issue(HUMAN, OWNED_ISSUE_URL, is_pull=False)),
        True,
    ),
    ("issue comment edited on an App PR", "issue_comment", payload("edited", issue=issue(APP)), False),
    ("the App's own comment on its PR", "issue_comment", payload("created", sender=APP, issue=issue(APP)), False),
    ("failed workflow run triggered by the App", "workflow_run", payload("completed", sender=APP, workflow_run=workflow_run(APP, "failure")), True),
    ("failed workflow run triggered by a human", "workflow_run", payload("completed", workflow_run=workflow_run(HUMAN, "failure")), False),
    (
        "failed workflow run triggered by the App on a branch no card has",
        "workflow_run",
        payload("completed", sender=APP, workflow_run=workflow_run(APP, "failure", "main")),
        False,
    ),
    ("successful workflow run triggered by the App", "workflow_run", payload("completed", sender=APP, workflow_run=workflow_run(APP, "success")), False),
    ("failed check run on an owned branch", "check_run", payload("completed", sender=APP, check_run=check_run(OWNED_BRANCH, "failure")), True),
    ("failed check run on another branch", "check_run", payload("completed", sender=APP, check_run=check_run("main", "failure")), False),
    ("failed Actions check run on an owned branch", "check_run", payload("completed", sender=APP, check_run=check_run(OWNED_BRANCH, "failure", "github-actions")), False),
    ("successful check run on an owned branch", "check_run", payload("completed", sender=APP, check_run=check_run(OWNED_BRANCH, "success")), False),
    ("the App merges its own PR", "pull_request", payload("closed", sender=APP, pull_request=pull_request(APP, merged=True)), True),
    ("a person merges an App PR", "pull_request", payload("closed", sender=HUMAN, pull_request=pull_request(APP, merged=True)), True),
    ("a person closes an App PR without merging", "pull_request", payload("closed", sender=HUMAN, pull_request=pull_request(APP)), True),
    ("the App closes its own PR without merging", "pull_request", payload("closed", sender=APP, pull_request=pull_request(APP)), False),
    ("push to an App PR", "pull_request", payload("synchronize", sender=APP, pull_request=pull_request(APP)), False),
    ("human PR closed", "pull_request", payload("closed", pull_request=pull_request(HUMAN)), False),
    ("card's GitHub issue closed", "issues", payload("closed", repo="example-org/site", issue=issue(HUMAN, OWNED_ISSUE_URL, is_pull=False)), True),
    ("another issue closed", "issues", payload("closed", issue=issue(HUMAN, "https://github.com/example-org/app/issues/8", is_pull=False)), False),
    ("card's GitHub issue labeled", "issues", payload("labeled", repo="example-org/site", issue=issue(HUMAN, OWNED_ISSUE_URL, is_pull=False)), False),
    ("failed check suite", "check_suite", payload("completed", sender=APP, check_suite={"conclusion": "failure", "head_branch": OWNED_BRANCH}), False),
    ("review on an App PR in an unconfigured repository", "pull_request_review", payload("submitted", repo="other-org/app", pull_request=pull_request(APP)), False),
]


@unittest.skipUnless(WEBHOOK_FILTERS.is_file(), f"needs the Hermes checkout at {HERMES_AGENT_DIR}; CI skips this class")
class HermesFiltersCase(unittest.TestCase):
    """Runs a rendered route through Hermes' own filter engine; subclasses name the route."""

    route_name = ""

    @classmethod
    def setUpClass(cls):
        # webhook_filters imports hermes_constants lazily from the checkout root.
        if str(HERMES_AGENT_DIR) not in sys.path:
            sys.path.insert(0, str(HERMES_AGENT_DIR))
        spec = importlib.util.spec_from_file_location("webhook_filters", WEBHOOK_FILTERS)
        cls.filters = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.filters)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.home)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.route = render.build_routes(SETTINGS)[self.route_name]
        self.processor = self.filters.WebhookRouteProcessor()

    def matches(self, event_type: str, body: dict) -> bool:
        return self.processor.route_filters_match(self.route, body, event_type, {})


class HermesFilterTest(HermesFiltersCase):
    route_name = "github"

    def test_each_event_is_kept_or_dropped(self):
        (self.home / "owned-refs.json").write_text(json.dumps(OWNED_REFS))
        self.assertNotIn("check_suite", self.route["events"])
        for name, event_type, body, expected in FILTER_CASES:
            with self.subTest(name):
                self.assertIs(self.matches(event_type, body), expected)

    def test_a_missing_owned_refs_file_drops_only_ref_keyed_events(self):
        with self.assertLogs(self.filters.logger, "WARNING"):
            dropped = self.matches("check_run", payload("completed", sender=APP, check_run=check_run(OWNED_BRANCH, "failure")))
            review = self.matches("pull_request_review", payload("submitted", pull_request=pull_request(APP)))
        self.assertIs(dropped, False)
        self.assertIs(review, False)
        self.assertIs(self.matches("issue_comment", payload("created", issue=issue(APP), comment=comment(HUMAN, "Hi."))), True)


CARD_ISSUES = [f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 5)]
CARDLESS_ISSUES = [f"00000000-0000-4000-8000-{n:012d}" for n in range(101, 144)]


def linear_closure(issue_id: str, state_type: str = "completed", changed: str = "stateId") -> dict:
    return {
        "type": "Issue",
        "action": "update",
        "data": {"id": issue_id, "state": {"type": state_type}},
        "updatedFrom": {changed: "00000000-0000-4000-8000-0000000000f0"},
    }


class LinearClosureReplayTest(HermesFiltersCase):
    """Replays a 24 hour window: 4 closures of issues with a card, 43 of issues without one."""

    route_name = "linear"

    def setUp(self):
        super().setUp()
        (self.home / "owned-refs.json").write_text(json.dumps([OWNED_BRANCH, *CARD_ISSUES]))

    def test_closures_of_issues_with_a_card_are_kept(self):
        kept = [i for i in CARD_ISSUES if self.matches("Issue", linear_closure(i))]
        self.assertEqual(kept, CARD_ISSUES)
        self.assertIs(self.matches("Issue", linear_closure(CARD_ISSUES[0], "canceled")), True)

    def test_closures_of_issues_without_a_card_are_dropped(self):
        dropped = [i for i in CARDLESS_ISSUES if not self.matches("Issue", linear_closure(i))]
        self.assertEqual(dropped, CARDLESS_ISSUES)
        self.assertIs(self.matches("Issue", linear_closure(CARDLESS_ISSUES[0], "canceled")), False)

    def test_session_events_and_delegate_changes_are_kept_without_a_card(self):
        cardless = CARDLESS_ISSUES[0]
        for action in ("created", "prompted"):
            session = {"type": "AgentSessionEvent", "action": action, "agentSession": {"issue": {"id": cardless}}}
            self.assertIs(self.matches("AgentSessionEvent", session), True)
        delegate = linear_closure(cardless, "started", changed="delegateId")
        self.assertIs(self.matches("Issue", delegate), True)
        undelegate = linear_closure(cardless, "completed", changed="delegateId")
        self.assertIs(self.matches("Issue", undelegate), True)

    def test_a_state_that_is_not_a_closure_is_dropped_even_with_a_card(self):
        self.assertIs(self.matches("Issue", linear_closure(CARD_ISSUES[0], "started")), False)
        self.assertIs(self.matches("Issue", linear_closure(CARD_ISSUES[0], changed="title")), False)

    def test_a_missing_owned_refs_file_drops_every_closure_and_keeps_sessions(self):
        (self.home / "owned-refs.json").unlink()
        with self.assertLogs(self.filters.logger, "WARNING"):
            self.assertIs(self.matches("Issue", linear_closure(CARD_ISSUES[0])), False)
        session = {"type": "AgentSessionEvent", "action": "created"}
        self.assertIs(self.matches("AgentSessionEvent", session), True)


if __name__ == "__main__":
    unittest.main()
