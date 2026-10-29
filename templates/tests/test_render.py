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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "profiles" / "claude-worker" / "scripts"))
import card_transition  # noqa: E402
import reply_inbox  # noqa: E402

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
    "factory_repository": {"full_name": "example-org/factory", "default_branch": "main"},
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
PUSH_FILTER = {
    "all": [
        {"field": "event", "equals": "push"},
        {"field": "repository.full_name", "equals": "example-org/factory"},
        {"field": "ref", "equals": "refs/heads/main"},
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
                    {"field": "data.state.type", "in": ["completed", "canceled", "duplicate"]},
                    {"field": "data.id", "in_file": "owned-refs.json"},
                ]
            },
            {
                "all": [
                    {"field": "data.state.type", "equals": "triage"},
                    {"any": [{"field": "action", "equals": "create"}, {"field": "updatedFrom.stateId", "exists": True}]},
                ]
            },
            {"all": [{"field": "updatedFrom.stateId", "exists": True}, {"field": "data.id", "in_file": "triage-refs.json"}]},
            {
                "all": [
                    {"field": "updatedFrom.labelIds", "exists": True},
                    {"field": "data.delegateId", "equals": "00000000-0000-4000-8000-000000000001"},
                    {"field": "data.labels", "regex": '(?i)"name": *"ready[^a-z0-9"]*for[^a-z0-9"]*agent"'},
                    {"not": {"field": "data.id", "in_file": "owned-refs.json"}},
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
                "events": [*GITHUB_EVENTS, "push"],
                "secret": "${GITHUB_WEBHOOK_SECRET}",
                "filters": [{"any": [PUSH_FILTER, {"all": [REPO_FILTER, EVENT_FILTER, SELF_FILTER]}]}],
                "toolsets": TOOLSETS,
            },
        )

    def test_route_events_list_matches_the_table(self):
        self.assertEqual(self.routes["github"]["events"], [*(row.name for row in render.GITHUB_EVENT_TABLE), "push"])

    def test_prompt_list_and_filter_cover_the_same_event_actions(self):
        prompt = self.routes["github"]["prompt"]
        listed = set()
        for match in re.finditer(r"^- `([a-z_]+)` with action ((?:`[a-z_]+`(?:, |, or | or )?)+), only when ", prompt, re.M):
            listed.update((match.group(1), action) for action in re.findall(r"`([a-z_]+)`", match.group(2)))
        filtered = set()
        push, table = self.routes["github"]["filters"][0]["any"]
        self.assertEqual(push, PUSH_FILTER)
        for clause in table["all"][1]["any"]:
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
    "agentActivity": {
        "id": "00000000-0000-4000-8000-0000000000c1",
        "signal": None,
        "content": {"type": "prompt", "body": "close"},
    },
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
    def prompted(self, body):
        event = {**PROMPTED_EVENT, "agentActivity": {**PROMPTED_EVENT["agentActivity"], "content": {"type": "prompt", "body": body}}}
        return render_like_hermes(render.build_routes(SETTINGS)["linear"]["prompt"], event)

    def test_the_prompted_event_fields_reach_the_prompt(self):
        prompt = self.prompted("close")

        self.assertIn("- Activity signal: None\n", prompt)
        self.assertIn("- Activity id: 00000000-0000-4000-8000-0000000000c1\n", prompt)


class TriageIntentContractTest(unittest.TestCase):
    """`triage_intent.ACTIONS` is the code's list of what a turn can be, and the prompt must handle each one."""

    def test_the_linear_prompt_names_every_action_the_code_can_compute(self):
        plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "linear-agent-session"
        spec = importlib.util.spec_from_file_location(
            "linear_agent_session", plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
        )
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        prompt = render.build_routes(SETTINGS)["linear"]["prompt"]

        missing = [action for action in plugin.triage_intent.ACTIONS if f"`{action}`" not in prompt]

        self.assertEqual(missing, [])
        self.assertNotIn("<triage_intent>", render.build_routes(SETTINGS)["github"]["prompt"])


class ReplyPrefixContractTest(unittest.TestCase):
    """The running worker recognizes a person's reply by `reply_inbox.REPLY_PREFIX`, so triage must write it."""

    def test_the_linear_prompt_has_triage_start_a_card_reply_with_the_prefix(self):
        prompt = render.build_routes(SETTINGS)["linear"]["prompt"]

        self.assertIn(f"`{reply_inbox.REPLY_PREFIX}`", prompt)


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

    def test_the_linear_prompt_archives_cards_through_the_worker_profile_script(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/srv/hermes"}):
            prompt = render.build_routes(SETTINGS)["linear"]["prompt"]
        self.assertIn("`python3 /srv/hermes/profiles/claude-worker/scripts/archive_card.py <task id>`", prompt)

    def test_linear_prompt_names_the_app_user(self):
        self.assertIn("00000000-0000-4000-8000-000000000001", self.prompts["linear"])

    def test_both_triage_prompts_carry_the_one_resume_section(self):
        section = render.triage_resume_section()
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertEqual(prompt.count(section), 1)

    def test_both_triage_prompts_carry_the_one_unblock_section(self):
        section = render.triage_unblock_section()
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertEqual(prompt.count(section), 1)

    def test_the_github_prompt_unblocks_a_blocked_card_through_the_shared_section(self):
        prompt = self.prompts["github"]
        heading = render.triage_unblock_section().splitlines()[0]
        self.assertEqual(heading, "## Unblock a card")
        rules = prompt.split("## Record the event", 1)[1].split(heading, 1)[0]
        blocked = [line for line in rules.splitlines() if "`blocked`" in line and "unblock it as" in line]
        self.assertEqual(len(blocked), 2)
        self.assertIn("`changes requested on GitHub; see the card comments`", rules)
        self.assertIn("`feedback on GitHub; see the card comments`", rules)
        flat = " ".join(rules.split())
        review_rule, comment_rule = flat.split("5. For a", 1)
        self.assertIn("`blocked`, when the sender is a person (not a bot): unblock", review_rule)
        self.assertIn("when the sender is a person (not a bot) and the text asks for a change", comment_rule)
        self.assertIn(
            "`blocked`, when the sender is a person (not a bot): unblock it as "
            '"Unblock a card" says, with the reason '
            "`changes requested on GitHub; see the card comments`, "
            "after step 1 has commented on the card",
            review_rule,
        )
        self.assertIn("`pull_request_review` submitted as a comment", comment_rule)
        self.assertIn("A thanks, an approval, or a bare question is not actionable", comment_rule)
        self.assertNotIn("hermes kanban unblock", rules)

    def test_the_github_prompt_resumes_a_triage_card_on_a_persons_approval(self):
        prompt = self.prompts["github"]
        rules = prompt.split("## Record the event", 1)[1].split("## Unblock a card", 1)[0]
        flat = " ".join(rules.split())
        approval_rule = flat.split("4. For", 1)[1].split("5. For", 1)[0]
        self.assertIn("with review state `approved`", approval_rule)
        self.assertIn(
            "when the sender is a person (not a bot) and the card's status is `triage`, "
            'resume it as "Resume a parked card" says, after step 1 has commented on the card',
            approval_rule,
        )
        self.assertIn("When the sender is a bot, or the card is in any other status, the comment from step 1 is enough.", approval_rule)
        self.assertNotIn("unblock", approval_rule)
        self.assertNotIn("reopen-review", approval_rule)

    def test_only_the_linear_prompt_carries_the_triage_inbox_and_delegation_gate_once(self):
        for section in (render.triage_inbox_section(render.validate(SETTINGS)), render.delegation_gate_section()):
            self.assertEqual(self.prompts["linear"].count(section), 1)
            self.assertEqual(self.prompts["github"].count(section), 0)

    def test_the_triage_inbox_fills_its_markers_from_the_settings(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/srv/hermes"}):
            section = render.triage_inbox_section(render.validate(SETTINGS))
        self.assertNotRegex(section, r"<<[a-z_]+>>")
        self.assertIn("`python3 /srv/hermes/profiles/claude-worker/scripts/archive_card.py --board triage <task id>`", section)
        self.assertIn("- `example-org/app`: `--workspace worktree`", section)
        self.assertIn("- `example-org/site`: `--workspace worktree:/srv/checkouts/site`", section)
        self.assertIn("is `00000000-0000-4000-8000-000000000001`", section)

    def test_a_triage_card_is_created_on_the_triage_board_without_an_assignee_or_contract(self):
        section = render.triage_inbox_section(render.validate(SETTINGS))
        create = section[section.index("hermes kanban --board triage create") : section.index("CARD\n   ````")]
        self.assertNotIn("--assignee", create)
        self.assertNotIn("--completion-contract", create)
        self.assertIn("--idempotency-key 'linear-triage:<issue id>'", create)
        self.assertIn("card: triage\n", create)

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
    def setUp(self):
        routes = render.build_routes(SETTINGS)
        self.section = render.triage_merged_section()
        self.github = routes["github"]["prompt"]
        self.linear = routes["linear"]["prompt"]

    def test_only_the_github_prompt_carries_the_section_once(self):
        self.assertEqual(self.github.count(self.section), 1)
        self.assertNotIn(self.section, self.linear)

    def test_linear_triage_reads_the_marker_that_card_transition_writes(self):
        marker = "Merge refused by Hermes:"
        self.assertTrue(all(case.marker.startswith(marker) for case in card_transition.Case))
        change = self.linear[self.linear.index("## Issue change") :]
        reply = self.linear[self.linear.index("## Prompt") : self.linear.index("## Requested issue change")]
        for step in (change, reply):
            self.assertIn(f"starts `{marker}`", " ".join(step.split()))


class TransitionCommandTest(unittest.TestCase):
    """A card's status and assignee change only through card_transition.py, whose tests pin each sequence."""

    TRANSITION = re.compile(
        r"hermes kanban (?:--board \S+ )?"
        r"(?:(?:specify|assign|promote|request-review|reopen-review|unblock|block|complete)\b|edit <task id> --title)"
    )

    def setUp(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/srv/hermes"}):
            routes = render.build_routes(SETTINGS)
        self.prompts = {name: routes[name]["prompt"] for name in routes}

    def test_no_prompt_runs_a_transition_command_itself(self):
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertEqual(self.TRANSITION.findall(prompt), [])
                self.assertNotIn("kanban_complete", prompt)

    def test_both_prompts_run_the_worker_profile_script(self):
        command = "python3 /srv/hermes/profiles/claude-worker/scripts/card_transition.py"
        for name, prompt in self.prompts.items():
            with self.subTest(route=name):
                self.assertIn(f"`{command} resume <task id>`", prompt)
                self.assertIn(f"`{command} unblock <task id> --repo '<repo>'", prompt)

    def test_the_pattern_finds_each_command_the_prompts_used_to_run(self):
        for command in (
            "hermes kanban assign <task id> none",
            "hermes kanban --board triage unblock <task id>",
            "hermes kanban edit <task id> --title '<kept title>'",
            "hermes kanban block <task id> stopped",
            "hermes kanban reopen-review <task id> --reason stopped",
        ):
            with self.subTest(command):
                self.assertTrue(self.TRANSITION.search(command))
        self.assertFalse(self.TRANSITION.search("hermes kanban edit <task id> --body '<new body>'"))


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
        "factory_repository must be an object": settings(factory_repository=None),
        "factory_repository.full_name 'factory' must look like owner/name": settings(
            factory_repository={"full_name": "factory", "default_branch": "main"}
        ),
        "factory_repository.default_branch None must be a branch name": settings(
            factory_repository={"full_name": "example-org/factory"}
        ),
        "factory_repository.default_branch 'main..x' must be a branch name": settings(
            factory_repository={"full_name": "example-org/factory", "default_branch": "main..x"}
        ),
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
            "platforms.webhook.extra.rate_limit": 1200,
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
        self.install_rate_limit(1200)

    def install_rate_limit(self, value):
        (self.tmp / "rate_limit.json").write_text(json.dumps(value))

    def test_installed_routes_with_references_match(self):
        self.install_rendered()

        self.assertEqual(self.check(), (0, [
            "route linear: matches; secret is the ${LINEAR_WEBHOOK_SECRET} reference",
            "route github: matches; secret is the ${GITHUB_WEBHOOK_SECRET} reference",
            "rate_limit: matches 1200",
        ]))

    def test_a_missing_rate_limit_is_reported(self):
        self.install_rendered()
        (self.tmp / "rate_limit.json").unlink()

        code, lines = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(lines[2], "rate_limit: not installed, so Hermes allows 30 a minute; install 1200")

    def test_a_lower_rate_limit_is_reported(self):
        self.install_rendered()
        self.install_rate_limit(30)

        code, lines = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(lines[2], "rate_limit: 30 differs from 1200")

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
            "rate_limit: matches 1200",
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


def push(ref: str = "refs/heads/main", repo: str = "example-org/factory", sender: str = APP) -> dict:
    return {
        "ref": ref,
        "before": "1" * 40,
        "after": "2" * 40,
        "repository": {"full_name": repo, "default_branch": "main"},
        "pusher": {"name": sender.removesuffix("[bot]")},
        "sender": {"login": sender},
    }


class HermesPushFilterTest(HermesFiltersCase):
    route_name = "github"

    def setUp(self):
        super().setUp()
        (self.home / "owned-refs.json").write_text(json.dumps(OWNED_REFS))

    def test_a_push_to_the_factory_default_branch_is_kept_from_any_sender(self):
        self.assertIn("push", self.route["events"])
        for sender in (APP, HUMAN):
            with self.subTest(sender):
                self.assertIs(self.matches("push", push(sender=sender)), True)

    def test_every_other_push_is_dropped(self):
        for name, body in {
            "another branch": push("refs/heads/goodyear/pat-1-fix"),
            "a tag": push("refs/tags/v1.0.0"),
            "a branch named like the default in a subdirectory": push("refs/heads/feature/main"),
            "an installed repository's default branch": push(repo="example-org/app"),
            "an unconfigured repository": push(repo="other-org/factory"),
        }.items():
            with self.subTest(name):
                self.assertIs(self.matches("push", body), False)

    def test_a_push_shaped_payload_under_another_event_is_not_kept_as_a_push(self):
        body = {**push(), "action": "completed", "check_run": check_run(OWNED_BRANCH, "success")}
        self.assertIs(self.matches("check_run", body), False)


CARD_ISSUES = [f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 5)]
CARDLESS_ISSUES = [f"00000000-0000-4000-8000-{n:012d}" for n in range(101, 144)]


def linear_issue_event(issue_id: str, state_type: str, *, action: str = "update", updated_from=None, **data) -> dict:
    body = {"type": "Issue", "action": action, "data": {"id": issue_id, "state": {"type": state_type}, **data}}
    if updated_from is not None:
        body["updatedFrom"] = updated_from
    return body


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
        (self.home / "triage-refs.json").write_text("[]")

    def test_closures_of_issues_with_a_card_are_kept(self):
        kept = [i for i in CARD_ISSUES if self.matches("Issue", linear_closure(i))]
        self.assertEqual(kept, CARD_ISSUES)
        self.assertIs(self.matches("Issue", linear_closure(CARD_ISSUES[0], "canceled")), True)
        self.assertIs(self.matches("Issue", linear_closure(CARD_ISSUES[0], "duplicate")), True)

    def test_closures_of_issues_without_a_card_are_dropped(self):
        dropped = [i for i in CARDLESS_ISSUES if not self.matches("Issue", linear_closure(i))]
        self.assertEqual(dropped, CARDLESS_ISSUES)
        self.assertIs(self.matches("Issue", linear_closure(CARDLESS_ISSUES[0], "canceled")), False)
        self.assertIs(self.matches("Issue", linear_closure(CARDLESS_ISSUES[0], "duplicate")), False)

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


APP_USER = SETTINGS["app_user_id"]
READY = [{"id": "l1", "name": "ready-for-agent", "color": "#0e8a16", "parentId": None}]
BUG = [{"id": "l2", "name": "bug", "color": "#d73a4a", "parentId": None}]


class LinearTriageReplayTest(HermesFiltersCase):
    route_name = "linear"

    def setUp(self):
        super().setUp()
        (self.home / "owned-refs.json").write_text(json.dumps([CARD_ISSUES[0]]))
        (self.home / "triage-refs.json").write_text(json.dumps([CARD_ISSUES[1]]))

    def test_an_issue_created_in_or_moved_into_triage_is_kept(self):
        new = CARDLESS_ISSUES[0]
        self.assertIs(self.matches("Issue", linear_issue_event(new, "triage", action="create")), True)
        moved = linear_issue_event(new, "triage", updated_from={"stateId": "s-backlog"})
        self.assertIs(self.matches("Issue", moved), True)

    def test_other_changes_of_an_issue_in_triage_are_dropped(self):
        title = linear_issue_event(CARDLESS_ISSUES[0], "triage", updated_from={"title": "old"})
        self.assertIs(self.matches("Issue", title), False)
        self.assertIs(self.matches("Issue", linear_issue_event(CARDLESS_ISSUES[0], "backlog", action="create")), False)

    def test_a_status_change_of_an_issue_with_a_triage_card_is_kept(self):
        left = linear_issue_event(CARD_ISSUES[1], "unstarted", updated_from={"stateId": "s-triage"})
        self.assertIs(self.matches("Issue", left), True)
        cardless = linear_issue_event(CARDLESS_ISSUES[0], "unstarted", updated_from={"stateId": "s-triage"})
        self.assertIs(self.matches("Issue", cardless), False)

    def test_ready_for_agent_on_a_delegated_issue_without_a_card_is_kept(self):
        passed = linear_issue_event(
            CARDLESS_ISSUES[0], "unstarted", updated_from={"labelIds": ["l2"]}, delegateId=APP_USER, labels=READY + BUG,
        )
        self.assertIs(self.matches("Issue", passed), True)

    def test_the_ready_label_matches_whatever_its_case_and_punctuation(self):
        for name in ("Ready for agent", "ready for agent", "READY_FOR_AGENT", "ready-for-agent"):
            with self.subTest(name):
                labels = [{"id": "l1", "name": name, "color": "#0e8a16", "parentId": None}]
                passed = linear_issue_event(
                    CARDLESS_ISSUES[0], "unstarted", updated_from={"labelIds": []}, delegateId=APP_USER, labels=labels,
                )
                self.assertIs(self.matches("Issue", passed), True)

    def test_other_triage_labels_do_not_match_the_ready_label(self):
        for name in ("Ready for human", "Needs info", "Won't fix", "Not ready for agents", "Ready for agent soon"):
            with self.subTest(name):
                labels = [{"id": "l1", "name": name, "color": "#0e8a16", "parentId": None}]
                event = linear_issue_event(
                    CARDLESS_ISSUES[0], "unstarted", updated_from={"labelIds": []}, delegateId=APP_USER, labels=labels,
                )
                self.assertIs(self.matches("Issue", event), False)

    def test_label_changes_that_start_no_implementation_card_are_dropped(self):
        cases = {
            "not delegated": dict(delegateId=None, labels=READY),
            "delegated elsewhere": dict(delegateId="00000000-0000-4000-8000-0000000000ff", labels=READY),
            "not ready": dict(delegateId=APP_USER, labels=BUG),
        }
        for name, data in cases.items():
            with self.subTest(name):
                event = linear_issue_event(CARDLESS_ISSUES[0], "unstarted", updated_from={"labelIds": []}, **data)
                self.assertIs(self.matches("Issue", event), False)
        with_card = linear_issue_event(
            CARD_ISSUES[0], "started", updated_from={"labelIds": []}, delegateId=APP_USER, labels=READY,
        )
        self.assertIs(self.matches("Issue", with_card), False)


if __name__ == "__main__":
    unittest.main()
