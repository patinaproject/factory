from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import REPO, SCRIPTS
from tests.test_card_transition import CardTransitionTest, park

PLUGIN_DIR = REPO / "plugins" / "linear-agent-session"
APP = "app-user-1"
PERSON = "user-person"
ISSUE_ID = "issue-uuid-1"
SESSION = "session-1"
HERMES_SESSION = "hermes-session-1"
NOW = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.timezone.utc)
HOSTILE_REPLY = "It's \"fine\"; echo $HOME `id`\nLINEAR_REPLY\nrm -rf / ; done"
RESOLVED_CONFIG = {"platforms": {"webhook": {"extra": {"routes": {"linear": {"secret": "resolved"}}}}}}


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "linear_agent_session", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.paused = lambda: False
    return module


plugin = load_plugin()


@contextlib.contextmanager
def hermes_config(config):
    hermes_cli = types.ModuleType("hermes_cli")
    module = types.ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: config
    hermes_cli.config = module
    with mock.patch.dict(sys.modules, {"hermes_cli": hermes_cli, "hermes_cli.config": module}):
        yield


class Context:
    def __init__(self):
        self.tools, self.hooks = {}, []

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def get_config(self, key, default=None):
        return {"app_user_id": APP}.get(key, default)


class Linear:
    def __init__(self):
        self.activities = []

    def create_activity(self, agent_session_id, content, *, ephemeral=False):
        self.activities.append((agent_session_id, content, ephemeral))
        return f"activity-{len(self.activities)}"


def prompted(body: str, activity_id: str = "activity-reply-1") -> dict:
    return {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "appUserId": APP,
        "agentSession": {"id": SESSION, "creatorId": PERSON, "issue": {"id": ISSUE_ID, "identifier": "PAT-1"}},
        "agentActivity": {"id": activity_id, "signal": None, "content": {"type": "prompt", "body": body}, "userId": PERSON},
    }


def gateway_event(payload: dict):
    source = SimpleNamespace(
        platform=SimpleNamespace(value="webhook"), user_id="webhook:linear", chat_type="webhook", chat_id="webhook:v2:chat-1"
    )
    return SimpleNamespace(source=source, raw_message=payload, text="rendered prompt")


class Recorder:
    """Every subprocess the plugin starts in this process, with the keyword arguments it passed."""

    def __init__(self):
        self.runs = []
        self.real = subprocess.run

    def __call__(self, *args, **kwargs):
        self.runs.append((list(args[0]), kwargs))
        return self.real(*args, **kwargs)


class ReplyForwardHarness(CardTransitionTest):
    def setUp(self) -> None:
        super().setUp()
        (self.home / "profiles" / "claude-worker").mkdir(parents=True)
        (self.home / "profiles" / "claude-worker" / "scripts").symlink_to(SCRIPTS)
        self.state["task"].update(
            id="t_1", title="Triage PAT-1", body=f"linear_issue_id: {ISSUE_ID}\nlinear_agent_session_id: {SESSION}\n"
        )
        env = {
            "HERMES_HOME": str(self.home),
            "HERMES_BIN": str(self.dir / "bin" / "hermes"),
            "FAKE_STATE": str(self.state_path),
            "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        redact = types.ModuleType("agent.redact")
        redact.redact_sensitive_text = lambda text, force=False: text
        modules = mock.patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.redact": redact})
        modules.start()
        self.addCleanup(modules.stop)
        self.linear = Linear()
        self.recorder = Recorder()
        run = mock.patch("subprocess.run", self.recorder)
        run.start()
        self.addCleanup(run.stop)
        client = mock.patch.object(plugin.linear, "client_from_env", lambda: self.linear)
        client.start()
        self.addCleanup(client.stop)
        self.ctx = Context()
        with hermes_config(RESOLVED_CONFIG):
            plugin.register(self.ctx)
        self.skip_home = Path(tempfile.mkdtemp(dir=self.dir))

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def load(self) -> None:
        self.state = json.loads(self.state_path.read_text())

    def intent_for(self, payload: dict):
        triage = plugin.triage_exit.Card("t_1", self.state["task"]["status"], "Triage PAT-1", self.state["task"]["body"])
        issue = {
            "id": ISSUE_ID, "identifier": "PAT-1", "gitBranchName": "pat-1", "stateType": "triage", "delegate": None,
            "delegateName": None, "labels": [], "agentSessions": [],
            "assignee": {"id": PERSON, "name": "Pat Person", "gitHubUserId": "1001", "app": False},
        }
        body = payload["agentActivity"]["content"]["body"]
        reply = plugin.triage_intent.Reply(body, "payload", PERSON, "person")
        approvers = plugin.linear.approvers(issue["assignee"], None, [])
        return plugin.triage_intent.decide(
            payload, issue, [triage], [], APP, NOW, reply, plugin.triage_intent.Thread([], "linear"), [], approvers, True, False
        )

    def deliver_through_gateway(self, payload: dict) -> dict:
        self.save()
        hook = plugin.make_dispatch_hook(
            lambda: self.linear, lambda: "linear", lambda: RESOLVED_CONFIG, lambda _: None, self.intent_for,
            lambda: self.skip_home,
        )
        return asyncio.run(hook(event=gateway_event(payload), gateway=object(), session_store=None))

    def forward(self, args: dict) -> dict:
        self.save()
        output = self.ctx.tools["linear_card_reply"]["handler"](args, session_id=HERMES_SESSION)
        self.load()
        return json.loads(output)

    def reply_comments(self) -> list:
        return [body for body in self.state["comments"] if body.startswith("Linear reply:")]

    def transitions(self) -> list:
        return [(argv, kwargs) for argv, kwargs in self.recorder.runs if any(part.endswith("card_transition.py") for part in argv)]


class TriageCardReplyTest(ReplyForwardHarness):
    def test_a_hostile_reply_to_a_scheduled_triage_card_reaches_it_once_on_standard_input(self):
        self.given("scheduled", comments=["Asked a question."], events=[park(1)])
        rewritten = self.deliver_through_gateway(prompted(HOSTILE_REPLY))
        self.assertEqual(rewritten["action"], "rewrite")

        out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual(out["action"], "unblocked")
        script = str(self.home / "profiles" / "claude-worker" / "scripts" / "card_transition.py")
        [(argv, kwargs)] = self.transitions()
        self.assertEqual(argv, [sys.executable, script, "--board", "triage", "triage-reply", "t_1"])
        self.assertEqual((kwargs["input"], kwargs.get("shell", False)), (HOSTILE_REPLY, False))
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("ready", None))
        self.assertFalse(any(HOSTILE_REPLY in part for argv, _ in self.recorder.runs for part in argv))

    def test_the_tool_runs_on_the_gateway_and_in_a_single_query_turn_without_the_terminal(self):
        guard = dict(self.ctx.hooks)["pre_tool_call"]
        with mock.patch.object(plugin, "session_platform", side_effect=["webhook", "cli"]):
            verdicts = [guard(tool_name="linear_card_reply", args={"reply_key": "k", "task_id": "t_1"}) for _ in range(2)]
        self.assertEqual(verdicts, [None, None])
        self.assertEqual(self.ctx.tools["linear_card_reply"]["toolset"], "linear_agent_session")
        self.assertNotIn("check_fn", self.ctx.tools["linear_card_reply"])

    def test_a_blocked_triage_card_is_unassigned_and_unblocked_once(self):
        self.given("blocked")
        self.deliver_through_gateway(prompted(HOSTILE_REPLY))

        out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual(out["action"], "unblocked")
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("ready", None))

    def test_a_triage_status_card_is_resumed_without_an_assignee(self):
        self.given("triage")
        self.deliver_through_gateway(prompted("Use the settings screen."))

        out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual((out["action"], out["assigned"]), ("resumed", False))
        self.assertEqual(self.reply_comments(), ["Linear reply: Use the settings screen."])
        self.assertEqual(self.state["task"]["assignee"], None)

    def test_a_repeated_delivery_and_a_replay_apply_the_reply_once(self):
        self.given("blocked")
        self.deliver_through_gateway(prompted(HOSTILE_REPLY))
        first = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})
        self.given("blocked")

        self.deliver_through_gateway(prompted(HOSTILE_REPLY))
        again = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual((first["repeat"], again["repeat"], again["action"]), (False, True, "unblocked"))
        self.assertEqual(len(self.transitions()), 1)
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])
        self.assertEqual(self.state["task"]["status"], "blocked")

    def test_a_failed_release_is_retried_and_releases_the_card_once(self):
        self.given("scheduled", comments=["Asked a question."], events=[park(1)])
        self.state["fail"]["unblock"] = 1
        self.deliver_through_gateway(prompted(HOSTILE_REPLY))

        with mock.patch.object(plugin.forwards.time, "sleep"):
            out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual(out["action"], "unblocked")
        self.assertEqual(len(self.transitions()), 2)
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])
        self.assertEqual([call[0] for call in self.state["calls"]].count("unblock"), 2)
        self.assertEqual(self.state["task"]["status"], "ready")

    def test_a_crash_after_the_reply_is_recorded_is_finished_by_the_next_delivery(self):
        self.given("scheduled", comments=["Asked a question."], events=[park(1)])
        self.deliver_through_gateway(prompted(HOSTILE_REPLY))
        store = plugin.forwards.store_dir(os.environ)

        def crash(forward):
            raise KeyboardInterrupt("the gateway died mid-forward")

        with self.assertRaises(KeyboardInterrupt):
            plugin.forwards.apply(store, "activity-reply-1", "t_1", crash)
        self.assertEqual(plugin.forwards.read(store, "activity-reply-1").attempts, 1)

        self.deliver_through_gateway(prompted(HOSTILE_REPLY))
        out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual(out["action"], "unblocked")
        self.assertEqual(plugin.forwards.read(store, "activity-reply-1").attempts, 2)
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])

    def test_the_sweep_releasing_the_card_first_leaves_the_forward_writing_nothing(self):
        self.given("scheduled", comments=["Asked a question."], events=[park(1)])
        self.deliver_through_gateway(prompted(HOSTILE_REPLY))
        self.state["comments"].append(f"Linear reply: {HOSTILE_REPLY}")
        self.given("ready")

        out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual((out["action"], out["status"]), ("none", "ready"))
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])

    def test_an_app_or_unknown_author_records_nothing_to_forward(self):
        for kind in ("app", "unknown"):
            with self.subTest(kind=kind):
                self.given("scheduled", comments=[], events=[park(0)])
                intent = dataclasses.replace(self.intent_for(prompted("ok", f"activity-{kind}")), reply_author_kind=kind)
                self.assertIsNone(plugin.forwards.receive(plugin.forwards.store_dir(os.environ), intent))
                out = self.forward({"reply_key": f"activity-{kind}", "task_id": "t_1"})
                self.assertEqual(out["stage"], "record")
        self.assertEqual(self.transitions(), [])
        self.assertEqual(self.reply_comments(), [])

    def test_a_failed_transition_posts_one_error_on_the_session_and_says_nothing_resumed(self):
        self.given("running")
        self.state["fail"]["show"] = plugin.forwards.FORWARD_ATTEMPTS
        self.deliver_through_gateway(prompted(HOSTILE_REPLY))
        acks = len(self.linear.activities)

        with mock.patch.object(plugin.forwards.time, "sleep"), self.assertLogs("linear_agent_session.forwards", "WARNING") as logs:
            out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})

        self.assertEqual((out["stage"], out["posted"]), ("transition", f"agent session {SESSION}"))
        [(session, content, ephemeral)] = self.linear.activities[acks:]
        self.assertEqual((session, content["type"], ephemeral), (SESSION, "error", False))
        self.assertIn("Stage `transition`", content["body"])
        self.assertIn("reply forward activity-reply-1 failed at transition", logs.output[0])
        self.assertEqual(len(self.transitions()), plugin.forwards.FORWARD_ATTEMPTS)

        spent = self.forward({"reply_key": "activity-reply-1", "task_id": "t_1"})
        self.assertEqual(spent["stage"], "attempts")
        self.assertEqual(len(self.transitions()), plugin.forwards.FORWARD_ATTEMPTS)

    def test_a_forward_to_another_card_is_refused(self):
        self.given("blocked")
        self.deliver_through_gateway(prompted(HOSTILE_REPLY))

        with self.assertLogs("linear_agent_session.forwards", "WARNING"):
            out = self.forward({"reply_key": "activity-reply-1", "task_id": "t_other"})

        self.assertEqual(out["stage"], "task")
        self.assertEqual((self.transitions(), self.state["task"]["status"]), ([], "blocked"))


def intent(**fields):
    return plugin.triage_intent.Intent(**{"issue": {"id": ISSUE_ID, "identifier": "PAT-1", "state_type": "todo"}, **fields})


class OtherForwardingIntentsTest(ReplyForwardHarness):
    def receive(self, value) -> None:
        self.assertIsNotNone(plugin.forwards.receive(plugin.forwards.store_dir(os.environ), value))

    def test_start_triage_with_reply_binds_the_new_card_on_its_first_forward(self):
        self.given("scheduled", comments=[], events=[park(0)])
        self.receive(intent(action="start_triage_with_reply", reply=HOSTILE_REPLY, reply_key="activity-start",
                            reply_author_kind="person", agent_session_id=SESSION))

        out = self.forward({"reply_key": "activity-start", "task_id": "t_1"})

        self.assertEqual(out["action"], "unblocked")
        [(argv, kwargs)] = self.transitions()
        self.assertEqual(argv[-3:], ["triage", "triage-reply", "t_1"])
        self.assertEqual(kwargs["input"], HOSTILE_REPLY)
        self.assertEqual(self.forward({"reply_key": "activity-start", "task_id": "t_1"})["repeat"], True)
        self.assertEqual(len(self.transitions()), 1)

    def test_a_top_level_comment_with_no_session_resumes_the_triage_card(self):
        self.given("blocked")
        self.receive(intent(action="comment_resume", reply=HOSTILE_REPLY, reply_key="comment-1",
                            triage_card={"id": "t_1", "status": "blocked"}, agent_session_id=None))

        out = self.forward({"reply_key": "comment-1", "task_id": "t_1"})

        self.assertEqual(out["action"], "unblocked")
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])

    def test_a_failed_comment_forward_with_no_session_comments_the_error_on_the_card(self):
        self.given("blocked")
        self.state["fail"]["show"] = plugin.forwards.FORWARD_ATTEMPTS
        self.receive(intent(action="comment_resume", reply="Go ahead.", reply_key="comment-2",
                            triage_card={"id": "t_1", "status": "blocked"}, agent_session_id=None))

        with mock.patch.object(plugin.forwards.time, "sleep"), self.assertLogs("linear_agent_session.forwards", "WARNING"):
            out = self.forward({"reply_key": "comment-2", "task_id": "t_1"})

        self.assertEqual((out["stage"], out["posted"]), ("transition", "card t_1"))
        [error] = [body for body in self.state["comments"] if body.startswith("The reply did not reach")]
        self.assertIn("Add the comment again on the issue.", error)
        self.assertEqual(self.reply_comments(), [])

    def implementation(self, status: str) -> None:
        self.state["task"]["body"] = "linear_issue_id: 1\nbranch: pat-1-work\nrepo: example-org/app"
        self.given(status, events=[{"kind": "scheduled", "payload": {"reason": "WAITING_ON=the design"}, "created_at": 500, "run_id": 1}],
                   branch_has_pr=False)
        self.receive(intent(action="card_reply", reply=HOSTILE_REPLY, reply_key="activity-impl", from_assignee=True,
                            reply_author_kind="person", card={"id": "t_1", "status": "scheduled"}, agent_session_id=SESSION))

    def test_a_scheduled_implementation_card_is_released_with_the_reply(self):
        self.implementation("scheduled")

        out = self.forward({"reply_key": "activity-impl", "task_id": "t_1"})

        self.assertEqual(out["action"], "unblocked")
        [(argv, kwargs)] = self.transitions()
        self.assertEqual((argv[-2:], "--board" in argv, kwargs["input"]), (["implementation-reply", "t_1"], False, HOSTILE_REPLY))
        self.assertEqual(self.reply_comments(), [f"Linear reply: {HOSTILE_REPLY}"])
        self.assertEqual((self.state["task"]["status"], self.state["task"]["assignee"]), ("ready", "claude-worker"))

    def test_a_blocked_or_triage_implementation_card_gets_the_reply_and_stays(self):
        for status in ("blocked", "triage"):
            with self.subTest(status=status):
                self.state["comments"] = []
                self.implementation(status)
                plugin.forwards._write(plugin.forwards.store_dir(os.environ), dataclasses.replace(
                    plugin.forwards.read(plugin.forwards.store_dir(os.environ), "activity-impl"), state="received", attempts=0))

                out = self.forward({"reply_key": "activity-impl", "task_id": "t_1"})

                self.assertEqual((out["action"], out["status"]), ("none", status))
                self.assertEqual(self.state["task"]["status"], status)

    def test_a_card_reply_to_a_card_that_does_not_wait_records_nothing(self):
        self.assertIsNone(plugin.forwards.receive(plugin.forwards.store_dir(os.environ), intent(
            action="card_reply", reply="ok", reply_key="activity-running", card={"id": "t_1", "status": "running"})))
