from __future__ import annotations

import asyncio
import contextlib
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
            "LINEAR_CLIENT_ID": "client-id",
            "LINEAR_CLIENT_SECRET": "client-secret",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.linear = Linear()
        self.recorder = Recorder()
        run = mock.patch("subprocess.run", self.recorder)
        run.start()
        self.addCleanup(run.stop)
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
