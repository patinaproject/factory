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

from tests.support import SCRIPTS, GitFixture

sys.path.insert(0, str(SCRIPTS))
import session_links  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("link_pr_hook", str(SCRIPTS / "link-pr-hook"))
_spec = importlib.util.spec_from_loader("link_pr_hook", _loader)
HOOK = importlib.util.module_from_spec(_spec)
_loader.exec_module(HOOK)

URL = "https://github.com/example-org/example-repo/pull/42"
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
            [{"id": "live-session", "input": {"addedExternalUrls": [{"label": "Pull request #42", "url": URL}]}}],
        )

    def test_a_session_that_holds_the_pull_request_gets_no_update(self) -> None:
        self.set_state(session_urls=[URL])

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
        self.assertEqual(self.updates()[0]["input"]["addedExternalUrls"][0]["url"], URL)
