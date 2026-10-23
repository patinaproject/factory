from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import PLUGIN_DIR, FakeLinear, load_plugin

plugin = load_plugin()

APP = "app-user-1"
ISSUE = "issue-uuid-1"
SECRET = "lin_api_a1b2c3d4e5f6"
CONFIG = {
    "platforms": {"webhook": {"extra": {"routes": {"linear": {"secret": "resolved"}}}}},
    "plugins": {"entries": {"linear-agent-session": {"settings": {"app_user_id": APP}}}},
}
FAKE_ARCHIVE = textwrap.dedent(
    """\
    import json, os, sys
    with open(os.environ["ARCHIVE_LOG"], "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    error = os.environ.get("ARCHIVE_ERROR")
    if error:
        print(json.dumps({"error": error}))
        sys.exit(1)
    print(json.dumps({"archived": sys.argv[-1], "freed_worktree": None}))
    """
)


def issue_update(state_type="backlog", previous=None, **data):
    return {
        "type": "Issue",
        "action": "update",
        "updatedFrom": previous if previous is not None else {"stateId": "triage-state", "updatedAt": "2026-10-23T09:00:00.000Z"},
        "data": {"id": ISSUE, "identifier": "PAT-1", "state": {"type": state_type, "name": "Backlog"}, **data},
    }


BRANCH = "pat-1-fix-it"


def card(task_id, status, issue=ISSUE, title="PAT-1: Fix it", branch=None):
    body = f"```text\ncard: triage\nlinear_issue_id: {issue}\nlinear_issue_identifier: PAT-1\n```\n"
    return SimpleNamespace(id=task_id, status=status, title=title, body=body, branch_name=branch)


def rewritten_card(task_id, status, branch):
    """What Hermes leaves when `kanban specify` succeeded but the restoring `edit` did not."""
    return SimpleNamespace(id=task_id, status=status, title="Specified task", body="a spec", branch_name=branch)


def live_issue(state_type="backlog", delegate=None, branch=BRANCH):
    issue = {"state": {"type": state_type}, "delegate": {"id": delegate} if delegate else None, "branchName": branch}
    return 200, {"data": {"issue": issue}}


class FakeContext:
    def __init__(self, settings):
        self.settings = settings
        self.hooks = []

    def register_tool(self, *_, **__):
        pass

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name))
        scripts = self.home / "profiles" / "claude-worker" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "archive_card.py").write_text(FAKE_ARCHIVE)
        self.archive_log = self.home / "archive.log"
        self.boards = {"triage": [], None: []}
        self.board_error = None
        self.linear = FakeLinear([live_issue()])
        env = {
            "LINEAR_CLIENT_ID": "client-id",
            "LINEAR_CLIENT_SECRET": "client-secret",
            "HERMES_HOME": str(self.home),
            "ARCHIVE_LOG": str(self.archive_log),
        }
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.dict(sys.modules, self.hermes_modules()),
            mock.patch("urllib.request.urlopen", self.linear),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def hermes_modules(self):
        boards = self

        @contextlib.contextmanager
        def connect_closing(board=None):
            if boards.board_error:
                raise boards.board_error
            yield board

        hermes_cli = types.ModuleType("hermes_cli")
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: CONFIG
        kanban_db = types.ModuleType("hermes_cli.kanban_db")
        kanban_db.list_tasks = lambda board: list(boards.boards[board])
        kanban_db_connect = types.ModuleType("hermes_cli.kanban_db_connect")
        kanban_db_connect.connect_closing = connect_closing
        hermes_cli.config, hermes_cli.kanban_db, hermes_cli.kanban_db_connect = config, kanban_db, kanban_db_connect
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.home
        redact = types.ModuleType("agent.redact")
        redact.redact_sensitive_text = lambda text, force=False: text
        return {
            "hermes_cli": hermes_cli,
            "hermes_cli.config": config,
            "hermes_cli.kanban_db": kanban_db,
            "hermes_cli.kanban_db_connect": kanban_db_connect,
            "hermes_constants": constants,
            "agent": types.ModuleType("agent"),
            "agent.redact": redact,
        }

    def dispatch(self, payload, route="linear"):
        ctx = FakeContext({"app_user_id": APP})
        plugin.register(ctx)
        hook = dict(ctx.hooks)["pre_gateway_dispatch"]
        source = SimpleNamespace(platform=SimpleNamespace(value="webhook"), user_id=f"webhook:{route}", chat_type="webhook")
        evt = SimpleNamespace(source=source, raw_message=payload, text="rendered prompt")
        return asyncio.run(hook(event=evt, gateway=object(), session_store=None))

    def assert_only_the_plain_exit_is_archived(self):
        """Control: the same board with a plain Triage exit is archived, so the guard is what passed the other through."""
        self.boards[None] = []
        self.linear.graphql_responses = [live_issue()]
        self.assertEqual(self.dispatch(issue_update("backlog"))["action"], "skip")
        self.assertEqual(self.archived(), [["--board", "triage", "t_triage"]])

    def archived(self):
        return [json.loads(line) for line in self.archive_log.read_text().splitlines()] if self.archive_log.exists() else []



class TriageExitTest(Harness):
    """A Linear delivery reaches the registered pre_gateway_dispatch hook as Hermes' gateway sends it."""

    def test_a_plain_triage_exit_archives_the_card_and_starts_no_turn(self):
        self.boards["triage"] = [card("t_other", "todo", issue="issue-uuid-2"), card("t_triage", "blocked")]

        result = self.dispatch(issue_update("backlog"))

        self.assertEqual(result, {"action": "skip", "reason": "archived triage card t_triage: PAT-1 left Triage"})
        self.assertEqual(self.archived(), [["--board", "triage", "t_triage"]])

    def test_a_running_triage_card_is_left_to_its_worker_and_no_turn_starts(self):
        self.boards["triage"] = [card("t_triage", "running")]

        result = self.dispatch(issue_update("backlog"))

        self.assertEqual(
            result, {"action": "skip", "reason": "left running triage card t_triage to its worker: PAT-1 left Triage"}
        )
        self.assertEqual(self.archived(), [])

    def test_an_issue_delegated_to_the_agent_app_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]

        self.assertIsNone(self.dispatch(issue_update("backlog", delegateId=APP)))
        self.assert_only_the_plain_exit_is_archived()

    def test_an_issue_delegated_since_the_delivery_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        self.linear.graphql_responses = [live_issue("backlog", delegate=APP)]

        self.assertIsNone(self.dispatch(issue_update("backlog")))
        self.assert_only_the_plain_exit_is_archived()

    def test_an_issue_back_in_triage_since_the_delivery_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        self.linear.graphql_responses = [live_issue("triage")]

        self.assertIsNone(self.dispatch(issue_update("backlog")))
        self.assert_only_the_plain_exit_is_archived()

    def test_a_status_change_that_also_changes_the_delegate_or_labels_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        for changed in ("delegateId", "labelIds"):
            with self.subTest(changed):
                previous = {"stateId": "triage-state", changed: None}
                self.assertIsNone(self.dispatch(issue_update("backlog", previous=previous)))
        self.assert_only_the_plain_exit_is_archived()

    def test_a_completed_or_canceled_issue_with_a_factory_card_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        self.boards[None] = [card("t_work", "review", title="PAT-1: Fix it")]
        for state_type in ("completed", "canceled"):
            with self.subTest(state_type):
                self.assertIsNone(self.dispatch(issue_update(state_type)))
        self.assert_only_the_plain_exit_is_archived()

    def test_a_default_board_card_on_the_issue_branch_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        for branch in (BRANCH, f"{BRANCH}-takeover"):
            with self.subTest(branch):
                self.boards[None] = [rewritten_card("t_work", "review", branch)]
                self.linear.graphql_responses = [live_issue("canceled")]
                self.assertIsNone(self.dispatch(issue_update("canceled")))
                self.assertEqual(self.archived(), [])
        self.boards[None] = [rewritten_card("t_other", "review", "pat-2-other-issue")]
        self.linear.graphql_responses = [live_issue("canceled")]
        self.assertEqual(self.dispatch(issue_update("canceled"))["action"], "skip")
        self.assertEqual(self.archived(), [["--board", "triage", "t_triage"]])

    def test_a_card_found_by_title_on_the_default_board_also_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        self.boards[None] = [SimpleNamespace(id="t_work", status="ready", title="PAT-1: Fix it", body="no id line", branch_name=None)]

        self.assertIsNone(self.dispatch(issue_update("completed")))
        self.assert_only_the_plain_exit_is_archived()

    def test_a_failed_archive_reaches_triage_and_logs_no_secret(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        os.environ["ARCHIVE_ERROR"] = f"hermes kanban archive failed: token {SECRET} rejected"

        with self.assertLogs("linear_agent_session", "WARNING") as logs:
            result = self.dispatch(issue_update("backlog"))

        self.assertIsNone(result)
        self.assertEqual(self.archived(), [["--board", "triage", "t_triage"]])
        output = "\n".join(logs.output)
        self.assertIn("t_triage", output)
        self.assertIn("token [redacted] rejected", output)
        self.assertNotIn(SECRET, output)

    def test_a_board_that_cannot_be_read_reaches_triage_and_logs_no_secret(self):
        self.board_error = RuntimeError(f"database is locked ({SECRET})")

        with self.assertLogs("linear_agent_session", "WARNING") as logs:
            self.assertIsNone(self.dispatch(issue_update("backlog")))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(self.archived(), [])

    def test_an_unreadable_live_issue_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        self.linear.graphql_responses = [(500, {"errors": [{"message": "down"}]})] * 3

        with self.assertLogs("linear_agent_session", "WARNING"):
            self.assertIsNone(self.dispatch(issue_update("backlog")))
        self.assertEqual(self.archived(), [])

    def test_every_other_delivery_reaches_triage_untouched(self):
        self.boards["triage"] = [card("t_triage", "todo"), card("t_twin", "todo")]
        cases = {
            "two triage cards": issue_update("backlog"),
            "still in triage": issue_update("triage"),
            "no status change": issue_update("backlog", previous={"title": "old"}),
            "created": {**issue_update("triage"), "action": "create"},
            "agent session": {"type": "AgentSessionEvent", "action": "removed"},
            "not a dict": "not json",
            "no state": {**issue_update(), "data": {"id": ISSUE, "identifier": "PAT-1"}},
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.dispatch(payload))
        self.boards["triage"] = []
        self.assertIsNone(self.dispatch(issue_update("backlog")), "no triage card")
        self.boards["triage"] = [card("t_triage", "todo")]
        self.assertIsNone(self.dispatch(issue_update("backlog"), route="github"), "another route")
        self.assert_only_the_plain_exit_is_archived()

    def test_a_delegate_or_label_change_passes_without_the_pool(self):
        submitted = []
        original = plugin.concurrent.futures.ThreadPoolExecutor.submit

        def spy(pool, fn, *args, **kwargs):
            submitted.append(args)
            return original(pool, fn, *args, **kwargs)

        self.boards["triage"] = [card("t_triage", "todo")]
        with mock.patch.object(plugin.concurrent.futures.ThreadPoolExecutor, "submit", spy):
            for changed in ("delegateId", "labelIds"):
                previous = {"stateId": "triage-state", changed: None}
                self.assertIsNone(self.dispatch(issue_update("backlog", previous=previous)))
            self.assertEqual(submitted, [])
            self.dispatch(issue_update("backlog"))
        self.assertEqual(len(submitted), 1, "control: a plain exit does enter the pool")

    def test_no_app_user_id_reaches_triage(self):
        self.boards["triage"] = [card("t_triage", "todo")]
        ctx = FakeContext({})
        plugin.register(ctx)
        hook = dict(ctx.hooks)["pre_gateway_dispatch"]
        source = SimpleNamespace(platform=SimpleNamespace(value="webhook"), user_id="webhook:linear", chat_type="webhook")
        evt = SimpleNamespace(source=source, raw_message=issue_update("backlog"), text="")
        self.assertIsNone(asyncio.run(hook(event=evt)))
        self.assert_only_the_plain_exit_is_archived()


FAKE_HERMES = textwrap.dedent(
    """\
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ["HERMES_LOG"], "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:2] == ["config", "get"]:
        assert args[2:] == ["plugins.entries.linear-agent-session.settings.app_user_id", "--json"], args
        print(json.dumps(os.environ["FAKE_APP_USER_ID"]))
        sys.exit(0)
    assert args[0] == "kanban" and args[-2:] == ["list", "--json"], args
    board = args[2] if args[1] == "--board" else "default"
    print(json.dumps(json.load(open(os.environ["FAKE_BOARDS"]))[board]))
    """
)


class TriageExitCliTest(unittest.TestCase):
    """webhook-retry runs this command from a cron script, where Hermes is reachable only through its CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name))
        scripts = self.home / "profiles" / "claude-worker" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "archive_card.py").write_text(FAKE_ARCHIVE)
        hermes = self.home / "hermes"
        hermes.write_text(f"#!{sys.executable}\n{FAKE_HERMES}")
        hermes.chmod(0o755)
        self.boards = self.home / "boards.json"
        self.archive_log = self.home / "archive.log"
        self.linear = FakeLinear([live_issue()])
        env = {
            "LINEAR_CLIENT_ID": "client-id",
            "LINEAR_CLIENT_SECRET": "client-secret",
            "HERMES_HOME": str(self.home),
            "HERMES_BIN": str(hermes),
            "FAKE_APP_USER_ID": APP,
            "FAKE_BOARDS": str(self.boards),
            "ARCHIVE_LOG": str(self.archive_log),
            "HERMES_LOG": str(self.home / "hermes.log"),
        }
        outside_hermes = {"hermes_cli": None, "hermes_constants": None, "agent": None, "agent.redact": None}
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.dict(sys.modules, outside_hermes),
            mock.patch("urllib.request.urlopen", self.linear),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def seed(self, triage, default=()):
        as_task = lambda c: {"id": c.id, "status": c.status, "title": c.title, "body": c.body, "branch_name": c.branch_name}
        self.boards.write_text(json.dumps({"triage": [as_task(c) for c in triage], "default": [as_task(c) for c in default]}))

    def archived(self):
        return [json.loads(line) for line in self.archive_log.read_text().splitlines()] if self.archive_log.exists() else []

    def hermes_calls(self):
        log = self.home / "hermes.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def run_cli(self, payload):
        path = self.home / "payload.json"
        path.write_text(json.dumps(payload))
        spec = importlib.util.spec_from_file_location("linear_agent_session_cli", os.path.join(PLUGIN_DIR, "cli.py"))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["triage-exit", "--payload-file", str(path)])
        return code, json.loads(out.getvalue())

    def test_a_replayed_plain_triage_exit_archives_the_card(self):
        self.seed([card("t_triage", "ready")])

        code, output = self.run_cli(issue_update("backlog"))

        self.assertEqual((code, output), (0, {"handled": True, "action": "archive", "task_id": "t_triage"}))
        self.assertEqual(self.archived(), [["--board", "triage", "t_triage"]])
        self.assertEqual(self.hermes_calls()[0], ["config", "get", "plugins.entries.linear-agent-session.settings.app_user_id", "--json"])

    def test_a_replayed_delivery_that_needs_a_decision_is_not_handled(self):
        self.seed([card("t_triage", "ready")], default=[card("t_work", "review")])

        code, output = self.run_cli(issue_update("canceled"))

        self.assertEqual((code, output["handled"], output["action"]), (0, False, "pass"))
        self.assertEqual(self.archived(), [])

    def test_a_replayed_exit_whose_archive_fails_is_not_handled_and_shows_no_secret(self):
        self.seed([card("t_triage", "ready")])
        os.environ["ARCHIVE_ERROR"] = f"bad token {SECRET}"

        code, output = self.run_cli(issue_update("backlog"))

        self.assertEqual((code, output), (0, {"handled": False, "action": "failed", "task_id": "t_triage"}))

    def test_a_replayed_delivery_that_cannot_be_a_plain_exit_reads_nothing(self):
        self.seed([card("t_triage", "ready")])
        not_issue = {**issue_update("backlog"), "type": "AgentSessionEvent"}
        delegate_changed = issue_update("backlog", previous={"stateId": "triage-state", "delegateId": None})
        label_changed = issue_update("backlog", previous={"stateId": "triage-state", "labelIds": []})
        still_in_triage = issue_update("triage")
        for name, payload in {"type": not_issue, "delegate": delegate_changed, "labels": label_changed, "state": still_in_triage}.items():
            with self.subTest(name):
                code, output = self.run_cli(payload)
                self.assertEqual((code, output["handled"], output["action"]), (0, False, "pass"))
        self.assertEqual(self.hermes_calls(), [])
        self.assertEqual(self.archived(), [])

    def test_an_unreadable_board_is_not_handled(self):
        self.boards.write_text("{}")

        code, output = self.run_cli(issue_update("backlog"))

        self.assertEqual((code, output), (0, {"handled": False, "action": "failed", "task_id": None}))


if __name__ == "__main__":
    unittest.main()
