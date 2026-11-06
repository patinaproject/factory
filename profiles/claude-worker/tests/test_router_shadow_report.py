from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

from tests.support import REPO, SCRIPTS

REPORT = SCRIPTS / "router-shadow-report"
PLUGIN = REPO / "plugins" / "linear-agent-session"
MODEL = "claude-test-1"
PRICES = {MODEL: {"input": 1e6, "output": 2e6, "cache_read": 0.5e6, "cache_write_5m": 1e6, "cache_write_1h": 1e6}}
BASE = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc).timestamp()
SCHEMA = (
    "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, model TEXT, started_at REAL, ended_at REAL, "
    "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER);"
    "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, "
    "tool_name TEXT, timestamp REAL)"
)


def chat_key(delivery, route="linear"):
    body = base64.urlsafe_b64encode(json.dumps(["default", route, delivery]).encode()).decode().rstrip("=")
    return f"webhook:v2:{body}"


def stamp(seconds):
    return datetime.fromtimestamp(BASE + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(delivery, at, action, outcome="rewrite", then=None, rule="r", route="linear", retry=False, session_id=None):
    return {
        "version": 1, "recorded_at": stamp(at), "chat_id": chat_key(delivery, route), "profile": "default",
        "route": route, "delivery": delivery, "attempt": 1, "retry": retry, "session_id": session_id,
        "action": action, "then": then, "rule": rule, "outcome": outcome,
    }


def call(name, **arguments):
    return {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def terminal(command):
    return call("terminal", command=command)


class RouterShadowReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gateway = Path(self.tmp.name) / "gateway"
        (self.gateway / "webhook-router" / "decisions").mkdir(parents=True)
        self.prices = Path(self.tmp.name) / "prices.json"
        self.prices.write_text(json.dumps(PRICES))
        with closing(sqlite3.connect(self.gateway / "state.db")) as db:
            db.executescript(SCHEMA)
            db.commit()

    def records(self, *records):
        path = self.gateway / "webhook-router" / "decisions" / "2026-11-06.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def session(self, session_id, chat_id, started, calls=(), source="webhook", ended=30.0, input_tokens=10, output_tokens=5):
        with closing(sqlite3.connect(self.gateway / "state.db")) as db:
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (session_id, source, chat_id, MODEL, BASE + started, BASE + started + ended, input_tokens, output_tokens),
            )
            db.execute(
                "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, 'assistant', ?, ?)",
                (session_id, json.dumps(list(calls)), BASE + started),
            )
            db.commit()

    def report(self, *extra, since="2026-11-06T09:00:00Z", until="2026-11-06T11:00:00Z"):
        return subprocess.run(
            [sys.executable, str(REPORT), "--since", since, "--until", until, "--gateway-home", str(self.gateway),
             "--plugin-dir", str(PLUGIN), "--prices", str(self.prices), *extra],
            capture_output=True, text=True,
        )

    def seed(self):
        self.records(
            record("created", 10, "create_card", rule="intent_turn"),
            record("ignored", 20, "ignore_factory_session", outcome="skip", rule="intent_needs_no_turn"),
            record("paused", 30, "paused", outcome="pass", rule="paused"),
            record("pr-comment", 40, "card_comment_only", outcome="pass", rule="github_comment_only", route="github"),
            record("review", 50, "needs_sender_authority", outcome="pass", then="act_on_change_request",
                   rule="github_change_request_person", route="github"),
            record("lost", 60, "create_card", rule="intent_turn"),
            record("replayed", 70, "replay_succeeded", outcome="succeeded", then="card_reply", rule="replay",
                   retry=True, session_id="s-replay"),
            record("github-replay", 80, "replay_succeeded", outcome="succeeded", rule="replay", retry=True,
                   route="github", session_id="s-github-replay"),
            record("too-late", 4000, "create_card"),
        )
        self.session("s-created", chat_key("created"), 12, [
            call("linear_agent_activity", type="action"),
            terminal("hermes kanban create 'Fix it' --assignee claude-worker --json"),
        ])
        self.session("s-paused", chat_key("paused"), 31)
        self.session("s-pr-comment", chat_key("pr-comment", "github"), 41, [
            call("mcp__hermes__kanban_comment", task_id="t"),
            terminal("python3 /home/profiles/claude-worker/scripts/card_transition.py reopen t --reason x"),
        ])
        self.session("s-review", chat_key("review", "github"), 51, [
            call("kanban_comment", task_id="t"),
            terminal("python3 '/home/profiles/claude-worker/scripts/card_transition.py' --board triage resume t"),
        ])
        self.session("s-replay", None, 71, [call("linear_card_reply"), call("linear_agent_activity")],
                     source="webhook-retry")
        self.session("s-github-replay", None, 81, [call("kanban_comment")], source="webhook-retry")

    def test_each_decision_is_joined_to_its_turn_and_judged_against_the_effects_its_action_expects(self):
        self.seed()

        result = self.report("--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(
            [(r["record"]["delivery"], r["session_id"], r["effects"], r["agreement"]) for r in rows],
            [
                ("created", "s-created", ["card_create", "linear_activity"], "agree"),
                ("ignored", None, None, "agree"),
                ("paused", "s-paused", [], "disagree"),
                ("pr-comment", "s-pr-comment", ["card_comment", "card_transition"], "disagree"),
                ("review", "s-review", ["card_comment", "card_transition"], "agree"),
                ("lost", None, None, "unjoined"),
                ("replayed", "s-replay", ["card_reply", "linear_activity"], "agree"),
                ("github-replay", "s-github-replay", ["card_comment"], "unjudged"),
            ],
        )
        created = rows[0]
        self.assertEqual((created["tokens"], created["cost_usd"], created["latency_seconds"]), (15, 20.0, 30.0))
        self.assertEqual(rows[4]["expected"], [["card_comment"], ["card_comment", "card_transition"]])

    def test_the_text_report_prints_the_agreement_rate_and_every_disagreement(self):
        self.seed()

        result = self.report()

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("agreement: 4/6 (66.7%); unjoined 1, unjudged 1", lines)
        disagreements = lines[lines.index("disagreements:") + 1:]
        self.assertEqual(disagreements, [
            f"- {stamp(30)} {chat_key('paused')} paused (paused): expected no turn, got session s-paused",
            f"- {stamp(40)} {chat_key('pr-comment', 'github')} card_comment_only (github_comment_only): "
            "expected card_comment, got card_comment,card_transition",
        ])
        created = next(line for line in lines if "linear/created" in line)
        self.assertEqual(created.split("  ")[3:], ["create_card", "card_create,linear_activity", "agree", "15", "20.0000", "30.0"])

    def test_a_redelivery_joins_the_session_that_started_after_it(self):
        self.records(record("again", 10, "create_card"), record("again", 100, "create_card"))
        self.session("s-second", chat_key("again"), 101, [call("kanban_create"), call("linear_agent_activity")])

        rows = json.loads(self.report("--json").stdout)

        self.assertEqual([(r["session_id"], r["agreement"]) for r in rows], [(None, "unjoined"), ("s-second", "agree")])

    def test_since_and_until_are_required(self):
        result = subprocess.run([sys.executable, str(REPORT), "--gateway-home", str(self.gateway)], capture_output=True, text=True)

        self.assertEqual(result.returncode, 2)
        self.assertIn("--since", result.stderr)

    def test_every_effect_the_tables_name_is_one_the_router_knows(self):
        sys.path.insert(0, str(SCRIPTS))
        loader = SourceFileLoader("router_shadow_report", str(REPORT))
        report = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        sys.modules[loader.name] = report
        loader.exec_module(report)
        router = report.load_router(PLUGIN)

        self.assertLessEqual(set(report.TOOL_EFFECTS.values()) | {e for _, e in report.COMMAND_EFFECTS}, set(router.EFFECTS))
        self.assertEqual(report.call_effects(call("mcp__linear__linear_issue_edit")), {"issue_edit"})
        self.assertEqual(report.call_effects(terminal("hermes kanban --board triage comment t 'x'")), {"card_comment"})
        self.assertEqual(report.call_effects(terminal("hermes kanban list --json")), set())


if __name__ == "__main__":
    unittest.main()
