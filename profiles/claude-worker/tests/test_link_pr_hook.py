from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import SCRIPTS, GitFixture

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
    def __init__(self, error: Exception | None = None, remove_error: Exception | None = None) -> None:
        self.calls = []
        self.removals = []
        self.error = error
        self.remove_error = remove_error

    def add_external_urls(self, session_id, urls):
        if self.error:
            raise self.error
        self.calls.append((session_id, urls))
        return [u["url"] for u in urls]

    def remove_misplaced_pull_requests(self, session_id):
        if self.remove_error:
            raise self.remove_error
        self.removals.append(session_id)
        return {"older": [URL]}


class FakeLinear:
    CLIENT_ERRORS = (RuntimeError,)


class LinkCreatedPullTest(unittest.TestCase):
    def link(self, event, client=None):
        client = client or FakeClient()
        return client, HOOK.link_created_pull(FakeLinear, client, "live-session", event)

    def test_a_created_pull_request_is_added_with_its_number_in_the_label(self) -> None:
        client, report = self.link(CREATED)

        self.assertEqual(client.calls, [("live-session", [{"label": "Pull request #42", "url": URL}])])
        self.assertEqual(report, {"added": [URL], "action": "linked", "removed": {"older": [URL]}})
        self.assertEqual(client.removals, ["live-session"])

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

    def test_a_failed_removal_from_the_older_sessions_is_reported_not_raised(self) -> None:
        client, report = self.link(CREATED, FakeClient(remove_error=RuntimeError("Entity not found")))

        self.assertEqual(
            report,
            {"added": [URL], "action": "failed", "error": "unlinking the older agent sessions failed: Entity not found"},
        )


class HookHome(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.home.mkdir()
        GitFixture.install_linear_plugin(GitFixture, self.home, cached_token=None, accepted=("run-token",))
        self.board = Path(tmp.name).resolve() / "kanban.db"
        self.put_card("linear_agent_session_id: live-session\nrepo: example-org/app")

    def put_card(self, body: str) -> None:
        self.board.unlink(missing_ok=True)
        with closing(sqlite3.connect(self.board)) as board:
            board.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, branch_name TEXT, status TEXT, "
                          "block_kind TEXT, completion_contract TEXT)")
            board.execute("INSERT INTO tasks VALUES ('t_1', 'title', ?, NULL, 'running', NULL, NULL)", (body,))
            board.commit()

    def sessions(self) -> dict:
        return {k: v["urls"] for k, v in json.loads((self.home / "linear-fake.json").read_text())["sessions"].items()}

    def run_hook(self, stdin: str, token: str | None = "run-token", board: Path | None = None):
        env = {k: v for k, v in os.environ.items() if k not in ("LINEAR_API_KEY", "HERMES_HOME")}
        env["HERMES_HOME"] = str(self.home)
        if token:
            env["LINEAR_API_KEY"] = token
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "link-pr-hook"), "--session-id", "live-session", "--task-id", "t_1",
             "--board", str(board or self.board)],
            input=stdin, capture_output=True, text=True, env=env, timeout=60,
        )

    def updates(self) -> list[dict]:
        return [e["session_update"] for e in GitFixture.linear_requests(self.home) if "session_update" in e]

    def set_state(self, **fields) -> None:
        path = self.home / "linear-fake.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


class LinkPrHookProcessTest(HookHome):
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


class LinkPrHookCurrentSessionTest(HookHome):
    CARD = "https://kanban.test/t_1"

    def setUp(self) -> None:
        super().setUp()
        self.set_state(sessions={
            "live-session": {"createdAt": "2026-10-27T09:00:00.000Z", "urls": [URL, REVIEW_URL, self.CARD]},
            "newer-session": {"createdAt": "2026-10-27T11:00:00.000Z", "urls": []},
        })

    def test_a_pull_request_goes_to_the_session_the_card_holds_now_and_leaves_the_run_start_session(self) -> None:
        self.put_card("linear_agent_session_id: newer-session\nrepo: example-org/app")

        result = self.run_hook(json.dumps(CREATED))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.sessions(), {"live-session": [self.CARD], "newer-session": [URL, REVIEW_URL]})

    def test_an_unreadable_board_adds_the_pull_request_to_the_run_start_session(self) -> None:
        self.set_state(sessions={
            "live-session": {"createdAt": "2026-10-27T09:00:00.000Z", "urls": []},
            "newer-session": {"createdAt": "2026-10-27T11:00:00.000Z", "urls": []},
        })

        result = self.run_hook(json.dumps(CREATED), board=self.board.with_name("missing.db"))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.sessions(), {"live-session": [URL, REVIEW_URL], "newer-session": []})

    def test_a_card_without_a_session_line_adds_the_pull_request_to_the_run_start_session(self) -> None:
        self.put_card("repo: example-org/app")

        self.run_hook(json.dumps(CREATED))

        self.assertEqual(self.sessions()["newer-session"], [])
        self.assertEqual(self.sessions()["live-session"], [URL, REVIEW_URL, self.CARD])

    def test_a_failed_removal_still_exits_zero(self) -> None:
        self.put_card("linear_agent_session_id: newer-session")
        self.set_state(session_remove_error="Entity not found")

        result = self.run_hook(json.dumps(CREATED))

        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(self.sessions()["live-session"], [URL, REVIEW_URL, self.CARD])
