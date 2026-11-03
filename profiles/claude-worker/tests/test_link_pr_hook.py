from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, FakeGithub, GitFixture

sys.path.insert(0, str(SCRIPTS))
import session_links  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("link_pr_hook", str(SCRIPTS / "link-pr-hook"))
_spec = importlib.util.spec_from_loader("link_pr_hook", _loader)
HOOK = importlib.util.module_from_spec(_spec)
_loader.exec_module(HOOK)

URL = "https://github.com/example-org/example-repo/pull/42"
REVIEW_URL = "https://linear.review/example-org/example-repo/pull/42"
CREATED = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "gh pr create --title 'fix: #12 the thing' --body-file body.md", "description": "Open the PR"},
    "tool_response": {"stdout": f"{URL}\n", "stderr": "Warning: 1 uncommitted change\n", "interrupted": False},
}
ALREADY_EXISTS = {
    **CREATED,
    "tool_response": {
        "stdout": "",
        "stderr": f"a pull request for branch \"abc-123-fix\" into branch \"main\" already exists:\n{URL}\n",
        "interrupted": False,
    },
}


class FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = []
        self.error = error

    def add_external_urls(self, session_id, urls):
        if self.error:
            raise self.error
        self.calls.append((session_id, urls))
        return [u["url"] for u in urls]


class FakeLinear:
    CLIENT_ERRORS = (RuntimeError,)


class LinkCreatedPullTest(unittest.TestCase):
    def link(self, event, client=None):
        client = client or FakeClient()
        return client, HOOK.link_created_pull(FakeLinear, client, "live-session", event)

    def test_a_created_pull_request_is_added_with_its_number_in_the_label(self) -> None:
        client, report = self.link(CREATED)

        self.assertEqual(client.calls, [("live-session", [{"label": "Pull request #42", "url": URL}])])
        self.assertEqual(report, {"added": [URL], "action": "linked"})

    def test_a_failed_create_sends_nothing(self) -> None:
        client, report = self.link(ALREADY_EXISTS)

        self.assertEqual((client.calls, report), ([], None))

    def test_an_interrupted_create_sends_nothing(self) -> None:
        interrupted = {**CREATED, "tool_response": {**CREATED["tool_response"], "interrupted": True}}

        client, report = self.link(interrupted)

        self.assertEqual((client.calls, report), ([], None))

    def test_a_call_that_is_not_a_create_sends_nothing(self) -> None:
        view = {**CREATED, "tool_input": {"command": "gh pr view 42 --json url -q .url"}}
        other_tool = {**CREATED, "tool_name": "Read"}

        for event in (view, other_tool, {}):
            with self.subTest(event=event):
                client, report = self.link(event)
                self.assertEqual((client.calls, report), ([], None))

    def test_the_last_pull_request_url_in_the_output_is_the_created_one(self) -> None:
        other = "https://github.com/example-org/example-repo/pull/7"
        event = {**CREATED, "tool_response": {"stdout": f"Closing {other}\n{URL}\n", "interrupted": False}}

        self.assertEqual(session_links.created_pull_url(event), URL)

    def test_a_linear_error_is_reported_not_raised(self) -> None:
        client, report = self.link(CREATED, FakeClient(RuntimeError("Entity not found")))

        self.assertEqual(
            report, {"added": [], "action": "failed", "error": "linking the agent session failed: Entity not found"}
        )


class LinkPrHookProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.home.mkdir()
        GitFixture.install_linear_plugin(GitFixture, self.home, cached_token=None, accepted=("run-token",))

    def run_hook(self, stdin: str, token: str | None = "run-token"):
        env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HERMES_HOME")}
        env["HERMES_HOME"] = str(self.home)
        if token:
            env["LINEAR_API_KEY"] = token
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "link-pr-hook"), "--session-id", "live-session"],
            input=stdin, capture_output=True, text=True, env=env, timeout=60,
        )

    def updates(self) -> list[dict]:
        return [e["session_update"] for e in GitFixture.linear_requests(self.home) if "session_update" in e]

    def set_state(self, **fields) -> None:
        path = self.home / "linear-fake.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))

    def test_a_created_pull_request_is_added_to_the_session_and_the_hook_prints_nothing(self) -> None:
        self.set_state(session_urls=["https://example.test/t_1"])

        result = self.run_hook(json.dumps(CREATED))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(
            self.updates(),
            [{"id": "live-session", "input": {"addedExternalUrls": [
                {"label": "Pull request #42", "url": URL},
                {"label": "Pull request #42", "url": REVIEW_URL},
            ]}}],
        )

    def test_a_session_that_holds_the_pull_request_gets_no_update(self) -> None:
        self.set_state(session_urls=[URL, REVIEW_URL])

        result = self.run_hook(json.dumps(CREATED))

        self.assertEqual((result.returncode, self.updates()), (0, []))

    def test_a_failed_create_sends_nothing(self) -> None:
        result = self.run_hook(json.dumps(ALREADY_EXISTS))

        self.assertEqual((result.returncode, self.updates()), (0, []))

    def test_every_error_exits_zero(self) -> None:
        self.set_state(session_update_error="Entity not found")
        cases = {
            "linear rejects the update": (json.dumps(CREATED), "run-token"),
            "unparseable event": ("not json", "run-token"),
            "no token": (json.dumps(CREATED), None),
            "token linear refuses": (json.dumps(CREATED), "stale-token"),
        }

        for name, (stdin, token) in cases.items():
            with self.subTest(name):
                result = self.run_hook(stdin, token)
                self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_a_failed_session_read_still_adds_the_pull_request(self) -> None:
        self.set_state(session_urls_error="boom")

        result = self.run_hook(json.dumps(CREATED))

        self.assertEqual(result.returncode, 0)
        self.assertEqual([u["url"] for u in self.updates()[0]["input"]["addedExternalUrls"]], [URL, REVIEW_URL])


REPO = "example-org/example-repo"
MUTATION_ARGV = [
    "api", "graphql",
    "-f", "query=mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH}) "
          "{ pullRequest { number } } }",
    "-f", "id=PR_node42",
]
CHECKS_RULE = [{"type": "pull_request"}, {"type": "required_status_checks"}]


class ArmAutoMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.home = self.dir / "home"
        self.home.mkdir()
        GitFixture.install_linear_plugin(GitFixture, self.home, cached_token=None, accepted=("run-token",))
        self.github = FakeGithub(self.dir, REPO)
        self.github.add_pull(42, "abc-123-fix")
        self.configure()

    def configure(self, *, draft=False, allow=True, rules=None, base="main", **extra) -> None:
        self.github.state["pulls"]["42"].update(draft=draft, base={"ref": base}, node_id="PR_node42")
        self.github.state.update(
            repository={"allow_auto_merge": allow}, rules={base: CHECKS_RULE if rules is None else rules}, **extra
        )
        self.github.save()

    def run_hook(self, *flags: str, event: dict = CREATED, token: str | None = None):
        env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HERMES_HOME")}
        env.update(self.github.env(), HERMES_HOME=str(self.home))
        if token:
            env["LINEAR_API_KEY"] = token
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "link-pr-hook"), *flags],
            input=json.dumps(event), capture_output=True, text=True, env=env, timeout=60,
        )

    def arm(self, **kwargs):
        return self.run_hook("--auto-merge-repo", REPO, **kwargs)

    def assert_nothing_merged(self) -> None:
        for call in self.github.calls():
            if call != MUTATION_ARGV:
                self.assertEqual([arg for arg in call if "merge" in arg.lower()], [], call)

    def test_a_ready_pull_request_in_a_repository_with_required_checks_is_armed_once_with_squash(self) -> None:
        result = self.arm()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.github.calls(), [
            ["api", f"repos/{REPO}/pulls/42"],
            ["api", f"repos/{REPO}"],
            ["api", f"repos/{REPO}/rules/branches/main"],
            MUTATION_ARGV,
        ])
        self.assert_nothing_merged()

    def test_the_base_branch_is_quoted_in_the_rules_path(self) -> None:
        self.configure(base="release/1")

        self.arm()

        self.assertIn(["api", f"repos/{REPO}/rules/branches/release%2F1"], self.github.calls())
        self.assertIn(MUTATION_ARGV, self.github.calls())

    def test_the_repository_is_matched_ignoring_case(self) -> None:
        result = self.run_hook("--auto-merge-repo", REPO.upper())

        self.assertEqual((result.stderr, MUTATION_ARGV in self.github.calls()), ("", True))

    def test_each_failed_condition_is_logged_as_a_skip_and_arms_nothing(self) -> None:
        cases = {
            "draft": (dict(draft=True), "the pull request is a draft"),
            "auto-merge not allowed": (dict(allow=False), "the repository does not allow auto-merge"),
            "no rules": (dict(rules=[]), "main has no required status checks rule"),
            "rules without checks": (dict(rules=[{"type": "pull_request"}]), "main has no required status checks rule"),
            "clean status": (
                dict(graphql_error="gh: Pull request is in clean status (enablePullRequestAutoMerge)"),
                "the pull request is already mergeable (clean status)",
            ),
        }
        for name, (settings, reason) in cases.items():
            with self.subTest(name):
                self.configure(**settings)
                Path(str(self.github.state_path) + ".log").unlink(missing_ok=True)

                result = self.arm()

                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (0, "", f"link-pr-hook: auto-merge skipped: {reason}\n"),
                )
                self.assertEqual(MUTATION_ARGV in self.github.calls(), name == "clean status")
                self.assert_nothing_merged()

    def test_a_draft_is_skipped_after_reading_only_the_pull_request(self) -> None:
        self.configure(draft=True)

        self.arm()

        self.assertEqual(self.github.calls(), [["api", f"repos/{REPO}/pulls/42"]])

    def test_another_repository_is_skipped_without_reading_github(self) -> None:
        result = self.run_hook("--auto-merge-repo", "example-org/other")

        self.assertEqual(
            (result.returncode, result.stderr, self.github.calls()),
            (0, "link-pr-hook: auto-merge skipped: example-org/example-repo is not example-org/other\n", []),
        )

    def test_without_the_flag_nothing_is_read_or_armed(self) -> None:
        result = self.run_hook()

        self.assertEqual((result.returncode, result.stderr, self.github.calls()), (0, "", []))

    def test_a_call_that_created_no_pull_request_arms_nothing(self) -> None:
        result = self.arm(event=ALREADY_EXISTS)

        self.assertEqual((result.returncode, result.stderr, self.github.calls()), (0, "", []))

    def test_an_api_failure_is_logged_and_exits_zero(self) -> None:
        self.configure(graphql_error="gh: Resource not accessible by integration (HTTP 403)")

        result = self.arm()

        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(
            result.stderr,
            "link-pr-hook: auto-merge failed: GhFailed('gh api graphql failed: gh: Resource not accessible by integration (HTTP 403)')\n",
        )
        self.assertEqual(self.github.calls().count(MUTATION_ARGV), 1)

    def test_a_missing_linear_key_does_not_stop_arming(self) -> None:
        result = self.run_hook("--session-id", "live-session", "--auto-merge-repo", REPO)

        self.assertEqual(result.returncode, 0)
        self.assertIn("link-pr-hook: linking failed: KeyError('LINEAR_API_KEY')", result.stderr)
        self.assertIn(MUTATION_ARGV, self.github.calls())

    def test_a_failure_to_arm_does_not_stop_the_link(self) -> None:
        self.configure(graphql_error="gh: boom")

        result = self.run_hook("--session-id", "live-session", "--auto-merge-repo", REPO, token="run-token")

        self.assertEqual(result.returncode, 0)
        updates = [e["session_update"] for e in GitFixture.linear_requests(self.home) if "session_update" in e]
        self.assertEqual([u["input"]["addedExternalUrls"][0] for u in updates], [{"label": "Pull request #42", "url": URL}])
