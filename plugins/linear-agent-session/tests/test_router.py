from __future__ import annotations

import asyncio
import base64
import datetime as dt
import importlib.util
import json
import os
import stat
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import PLUGIN_DIR, load_plugin
from tests.test_missed_prompts import PROMPT_ID, prompted_event
from tests.test_plugin import RESOLVED_CONFIG, FakeContext, Platform, RecordingClient, event, hermes_config, session_event

plugin = load_plugin()
router = plugin.router
Intent = plugin.triage_intent.Intent
REPO = Path(PLUGIN_DIR).parents[1]


def chat_key(delivery, route="linear", profile="default"):
    body = base64.urlsafe_b64encode(json.dumps([profile, route, delivery]).encode()).decode().rstrip("=")
    return f"webhook:v2:{body}"


def load_queue_store():
    spec = importlib.util.spec_from_file_location("queue_store_for_router", REPO / "plugins" / "webhook-retry" / "queue_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMMENT_ID = "6c1f0a52-3a8e-4f43-9d0b-1f5c2a7d9e01"
ISSUE = {"id": "issue-1", "identifier": "ABC-1", "state_type": "backlog"}
TRIAGE_EXIT_ISSUE = {"id": "issue-1", "identifier": "ABC-1", "state": {"type": "backlog"}}


def not_handled(_payload):
    return types.SimpleNamespace(handled=False, action="none", reason="")


def github_event(payload, event_type, action="created", chat_id=None):
    evt = event(payload, route="github", chat_id=chat_id or chat_key("gh-1", "github"))
    evt.text = f"## This event\n\n- Event: {event_type}, action {action}\n- Repository: example/app"
    return evt


class RouterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.computed = []
        self.client = RecordingClient()

    def hook(self, intent=None, route="linear", triage_exit=not_handled, shadow=True):
        def compute(payload):
            self.computed.append(payload)
            return intent or Intent(action="create_card")

        return plugin.make_dispatch_hook(
            lambda: self.client, lambda: route, lambda: RESOLVED_CONFIG, triage_exit, compute, lambda: self.home,
            router_shadow=lambda: shadow,
        )

    def deliver(self, evt, **options):
        result = asyncio.run(self.hook(**options)(event=evt))
        router.drain()
        return result

    def records(self):
        directory = self.home / "webhook-router" / "decisions"
        if not directory.is_dir():
            return []
        return [json.loads(line) for path in sorted(directory.glob("*.jsonl")) for line in path.read_text().splitlines()]

    def decisions(self):
        return [(r["action"], r["then"], r["rule"], r["outcome"]) for r in self.records()]


class IdentityTest(RouterCase):
    def test_a_record_names_the_chat_id_and_its_decoded_parts(self):
        key = chat_key("delivery-7")
        self.deliver(event(session_event("prompted"), chat_id=key))

        (record,) = self.records()
        self.assertEqual(
            {k: record[k] for k in ("chat_id", "profile", "route", "delivery", "attempt", "retry", "session_id")},
            {"chat_id": key, "profile": "default", "route": "linear", "delivery": "delivery-7", "attempt": 1,
             "retry": False, "session_id": None},
        )

    def test_a_key_that_does_not_decode_keeps_its_raw_chat_id_and_no_delivery(self):
        self.deliver(event(session_event("prompted"), chat_id="webhook:v2:chat-1"))

        (record,) = self.records()
        self.assertEqual(
            (record["chat_id"], record["profile"], record["route"], record["delivery"]),
            ("webhook:v2:chat-1", None, None, None),
        )

    def test_decode_key_matches_the_webhook_retry_queue_store(self):
        queue_store = load_queue_store()
        for key in (chat_key("d"), chat_key("x", "github", "worker"), "webhook:v2:chat-1", "webhook:v2:" + "e30", 7, ""):
            with self.subTest(key):
                self.assertEqual(router.decode_key(key), queue_store.decode_key(key))


class OneRecordPerDecisionTest(RouterCase):
    def test_each_linear_hook_exit_appends_exactly_one_record(self):
        prompted = session_event("prompted")
        cases = {
            "rewrite": (event(prompted), {}, ("create_card", None, "intent_turn", "rewrite")),
            "needs no turn": (
                event(prompted),
                {"intent": Intent(action="ignore_factory_session")},
                ("ignore_factory_session", None, "intent_needs_no_turn", "skip"),
            ),
            "teammate reply": (
                event({**prompted, "agentActivity": {"content": {"body": '<user id="u-2" notify>Luke</user> ok?'}}}),
                {},
                ("teammate_reply", None, "teammate_reply", "skip"),
            ),
            "triage exit": (
                event({"type": "Issue", "action": "update", "updatedFrom": {"stateId": "s"}, "data": TRIAGE_EXIT_ISSUE}),
                {"triage_exit": lambda _: types.SimpleNamespace(handled=True, action="archive", reason="archived")},
                ("triage_exit_handled", None, "triage_exit_handled", "skip"),
            ),
            "use session": (
                event(prompted),
                {"intent": Intent(action="use_session", then="create_card")},
                ("use_session", "create_card", "intent_turn", "rewrite"),
            ),
        }
        for name, (evt, options, expected) in cases.items():
            with self.subTest(name):
                self.setUp()
                self.deliver(evt, **options)
                self.assertEqual(self.decisions(), [expected])

    def test_a_paused_delivery_and_an_unresolved_secret_each_append_one_record(self):
        with mock.patch.object(plugin, "paused", return_value=True):
            self.deliver(event(session_event("created")))
        unresolved = {"platforms": {"webhook": {"extra": {"routes": {"linear": {"secret": "${LINEAR_SECRET}"}}}}}}
        hook = plugin.make_dispatch_hook(
            lambda: self.client, lambda: "linear", lambda: unresolved, not_handled, None, lambda: self.home,
            router_shadow=lambda: True,
        )
        asyncio.run(hook(event=event(session_event("created"))))
        router.drain()

        self.assertEqual(
            self.decisions(),
            [("paused", None, "paused", "pass"), ("unresolved_secret", None, "unresolved_secret", "skip")],
        )

    def test_a_delivery_that_is_not_a_webhook_appends_nothing(self):
        self.deliver(event(session_event("created"), platform=Platform.SLACK))

        self.assertEqual(self.records(), [])

    def test_a_redelivery_counts_one_higher_even_after_a_restart(self):
        key = chat_key("again")
        self.deliver(event(session_event("prompted"), chat_id=key))
        self.deliver(event(session_event("prompted"), chat_id=key))
        restarted = load_plugin()
        asyncio.run(restarted.make_dispatch_hook(
            lambda: self.client, lambda: "linear", lambda: RESOLVED_CONFIG, not_handled,
            lambda _: Intent(action="card_reply"), lambda: self.home, router_shadow=lambda: True,
        )(event=event(session_event("prompted"), chat_id=key)))
        router.drain()
        self.deliver(event(session_event("prompted"), chat_id=chat_key("other")))

        self.assertEqual([(r["delivery"], r["attempt"]) for r in self.records()], [
            ("again", 1), ("again", 2), ("again", 3), ("other", 1),
        ])

    def test_a_coalesced_issue_burst_appends_one_record(self):
        payload = {
            "type": "Issue", "action": "update", "updatedFrom": {"delegateId": None, "labelIds": []},
            "data": {"id": "issue-1"}, "factoryRoute": {"issue": "issue-1", "seq": 3},
        }
        self.deliver(event(payload), intent=Intent(action="use_session", then="create_card"))

        self.assertEqual(self.decisions(), [("use_session", "create_card", "intent_turn", "rewrite")])

    def test_a_comment_records_its_first_delivery_and_then_its_duplicate(self):
        comment = {"type": "Comment", "action": "create", "data": {"id": COMMENT_ID}}
        intent = Intent(action="comment_resume", comment_id=COMMENT_ID, reply_key="k-1")
        store = self.home / "store"
        hook = plugin.make_dispatch_hook(
            lambda: self.client, lambda: "linear", lambda: RESOLVED_CONFIG, not_handled, lambda _: intent,
            lambda: self.home, comment_store=lambda: str(store), forward_store=lambda: str(self.home / "forwards"),
            router_shadow=lambda: True,
        )
        with mock.patch.object(plugin.forwards, "pending", return_value=False):
            for _ in range(2):
                asyncio.run(hook(event=event(comment)))
        router.drain()

        self.assertEqual(self.decisions(), [
            ("comment_resume", None, "comment_first_delivery", "rewrite"),
            ("duplicate_comment", None, "duplicate_comment", "skip"),
        ])

    def test_the_linear_route_records_the_computed_intent_without_computing_again(self):
        self.deliver(event(session_event("prompted")), intent=Intent(action="card_reply"))

        self.assertEqual((len(self.computed), self.client.calls[1:]), (1, []))
        self.assertEqual(self.decisions(), [("card_reply", None, "intent_turn", "rewrite")])


class GithubDecisionTest(RouterCase):
    PERSON = {"login": "ted", "id": 1, "type": "User"}
    BOT = {"login": "ci[bot]", "id": 2, "type": "Bot"}

    def test_each_triage_github_branch_has_its_action(self):
        pr = {"number": 4, "merged": True, "head": {"ref": "b"}}
        on_pr = {"number": 4, "pull_request": {"html_url": "https://github.com/example/app/pull/4"}}
        cases = [
            ({"ref": "refs/heads/main", "before": "a", "after": "b", "pusher": {}}, "push", "", ("auto_deploy", None, "push")),
            ({"action": "closed", "pull_request": pr, "sender": self.PERSON}, "pull_request", "closed",
             ("complete_merged_card", None, "github_merged")),
            ({"action": "closed", "pull_request": {**pr, "merged": False}, "sender": self.PERSON}, "pull_request", "closed",
             ("card_comment_only", None, "github_comment_only")),
            ({"action": "submitted", "review": {"state": "changes_requested"}, "sender": self.BOT}, "pull_request_review",
             "submitted", ("act_on_change_request", None, "github_change_request_bot")),
            ({"action": "submitted", "review": {"state": "changes_requested"}, "sender": self.PERSON}, "pull_request_review",
             "submitted", ("needs_sender_authority", "act_on_change_request", "github_change_request_person")),
            ({"action": "submitted", "review": {"state": "approved"}, "sender": self.PERSON}, "pull_request_review",
             "submitted", ("needs_sender_authority", "resume_approved_card", "github_approval_person")),
            ({"action": "submitted", "review": {"state": "approved"}, "sender": self.BOT}, "pull_request_review",
             "submitted", ("card_comment_only", None, "github_comment_only")),
            ({"action": "submitted", "review": {"state": "commented"}, "sender": self.PERSON}, "pull_request_review",
             "submitted", ("needs_sender_authority", "needs_change_request_judgment", "github_review_comment_person")),
            ({"action": "created", "comment": {}, "sender": self.PERSON}, "pull_request_review_comment", "created",
             ("needs_sender_authority", "needs_change_request_judgment", "github_comment_person")),
            ({"action": "created", "issue": on_pr, "comment": {}, "sender": self.PERSON}, "issue_comment", "created",
             ("needs_sender_authority", "needs_change_request_judgment", "github_comment_person")),
            ({"action": "created", "issue": on_pr, "comment": {}, "sender": self.BOT}, "issue_comment", "created",
             ("card_comment_only", None, "github_comment_only")),
            ({"action": "created", "issue": {"number": 5}, "comment": {}, "sender": self.PERSON}, "issue_comment", "created",
             ("card_comment_only", None, "github_comment_only")),
            ({"action": "completed", "workflow_run": {}, "sender": self.BOT}, "workflow_run", "completed",
             ("card_comment_only", None, "github_comment_only")),
        ]
        for payload, event_type, action, expected in cases:
            with self.subTest(expected):
                self.setUp()
                self.deliver(github_event(payload, event_type, action), route="linear")
                self.assertEqual(self.decisions(), [(*expected, "pass")])
                self.assertEqual(self.computed, [])

    def test_a_question_agrees_with_its_own_effects_and_with_its_yes_branch(self):
        comment, transition = router.CARD_COMMENT, router.CARD_TRANSITION
        self.assertEqual(
            router.acceptable("needs_sender_authority", "needs_change_request_judgment"),
            {frozenset({comment}), frozenset({comment, transition})},
        )
        self.assertEqual(router.acceptable("card_comment_only", None), {frozenset({comment})})
        self.assertEqual(
            router.acceptable("replay_succeeded", "create_card"), {frozenset({router.CARD_CREATE, router.LINEAR_ACTIVITY})}
        )
        self.assertIsNone(router.acceptable("replay_succeeded", None))


class ActionTableTest(RouterCase):
    def test_the_table_holds_every_triage_intent_action_and_names_known_effects(self):
        self.assertEqual(set(router.INTENT_EXPECTS), set(plugin.triage_intent.ACTIONS))
        self.assertLessEqual(set(plugin.triage_intent.ACTIONS), set(router.ACTIONS))
        for name, action in router.ACTIONS.items():
            with self.subTest(name):
                self.assertLessEqual(action.expects, router.EFFECTS)
                self.assertIn(action.kind, ("decision", "question", "replay"))
                self.assertTrue(action.yes is None or action.yes in router.ACTIONS)

    def test_every_recorded_action_and_then_is_a_key_of_the_table(self):
        for intent in plugin.triage_intent.ACTIONS:
            self.deliver(event(session_event("prompted")), intent=Intent(action=intent, issue=ISSUE))
        for event_type in ("pull_request", "pull_request_review", "issue_comment", "check_run"):
            self.deliver(github_event({"action": "submitted", "review": {"state": "commented"}}, event_type))

        records = self.records()
        self.assertEqual(len(records), len(plugin.triage_intent.ACTIONS) + 4)
        for record in records:
            self.assertIn(record["action"], router.ACTIONS)
            self.assertTrue(record["then"] is None or record["then"] in router.ACTIONS, record)


class SettingTest(RouterCase):
    def test_the_registered_hook_reads_router_shadow_and_defaults_it_on(self):
        for settings, expected in (({}, True), ({"router_shadow": True}, True), ({"router_shadow": False}, False)):
            with self.subTest(settings):
                ctx = FakeContext({"webhook_route": "linear", **settings})
                seen = []
                with hermes_config(RESOLVED_CONFIG), mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
                    plugin.register(ctx)
                    hook = dict(ctx.hooks)["pre_gateway_dispatch"]
                    with mock.patch.object(router, "submit", lambda enabled, *_: seen.append(enabled())):
                        asyncio.run(hook(event=event(session_event("created"), route="github")))
                self.assertEqual(seen, [expected])


class StorageTest(RouterCase):
    def test_records_are_versioned_private_json_lines_named_for_the_utc_date(self):
        self.deliver(event(session_event("prompted")))

        root = self.home / "webhook-router"
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        path = root / "decisions" / f"{today}.jsonl"
        self.assertEqual([p.name for p in (root / "decisions").iterdir()], [path.name])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        for directory in (root, root / "decisions", root / "attempts"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        (record,) = self.records()
        self.assertEqual(set(record), {
            "version", "recorded_at", "chat_id", "profile", "route", "delivery", "attempt", "retry", "session_id",
            "action", "then", "rule", "outcome",
        })
        self.assertEqual(record["version"], 1)

    def test_files_older_than_fourteen_days_are_pruned(self):
        decisions = self.home / "webhook-router" / "decisions"
        decisions.mkdir(parents=True)
        today = dt.datetime.now(dt.timezone.utc).date()
        old = decisions / f"{today - dt.timedelta(days=15)}.jsonl"
        kept = decisions / f"{today - dt.timedelta(days=14)}.jsonl"
        old.write_text("{}\n")
        kept.write_text("{}\n")

        self.deliver(event(session_event("prompted")))

        self.assertEqual(sorted(p.name for p in decisions.iterdir()), [kept.name, f"{today}.jsonl"])

    def test_a_record_holds_no_payload_text(self):
        payload = session_event("prompted")
        payload["agentActivity"]["content"]["body"] = "PRIVATE-REPLY-BODY"
        payload["agentSession"]["issue"]["title"] = "PRIVATE-TITLE"
        evt = event(payload)
        evt.text = "PRIVATE-PROMPT"
        self.deliver(evt, intent=Intent(action="card_reply", reply="PRIVATE-REPLY-BODY"))

        text = "".join(p.read_text() for p in (self.home / "webhook-router" / "decisions").glob("*"))
        self.assertNotIn("PRIVATE", text)

    def test_the_setting_off_writes_nothing(self):
        result = self.deliver(event(session_event("prompted")), shadow=False)

        self.assertEqual(result["action"], "rewrite")
        self.assertFalse((self.home / "webhook-router").exists())


class FailSafeTest(RouterCase):
    def setUp(self):
        super().setUp()
        router.drain()
        patcher = mock.patch.object(router, "_last_warning", -router.WARNING_INTERVAL_SECONDS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def timed(self, evt, **options):
        hook = self.hook(**options)
        started = time.monotonic()
        result = asyncio.run(hook(event=evt))
        return result, time.monotonic() - started

    def test_a_router_that_raises_leaves_the_return_alone_and_warns_once_a_minute(self):
        expected = self.deliver(event(session_event("prompted")), shadow=False)
        with mock.patch.object(router, "record", side_effect=RuntimeError("boom")), \
                self.assertLogs("linear_agent_session", "WARNING") as logs:
            first, elapsed = self.timed(event(session_event("prompted")))
            router.drain()
            second, _ = self.timed(event(session_event("prompted")))
            router.drain()

        self.assertEqual((first, second), (expected, expected))
        self.assertLess(elapsed, 1.0)
        self.assertEqual(logs.output, [
            "WARNING:linear_agent_session.router:linear-agent-session: the shadow router recorded no decision: RuntimeError: boom",
        ])

    def test_a_router_that_sleeps_ten_seconds_leaves_the_return_alone_and_returns_at_once(self):
        expected = self.deliver(event(session_event("prompted")), shadow=False)
        release = threading.Event()
        self.addCleanup(router.drain)
        self.addCleanup(release.set)
        with mock.patch.object(router, "record", side_effect=lambda *_: release.wait(10)), \
                mock.patch.object(router, "MAX_PENDING", 1), \
                self.assertLogs("linear_agent_session", "WARNING") as logs:
            first, first_elapsed = self.timed(event(session_event("prompted")))
            second, second_elapsed = self.timed(event(session_event("prompted")))
            third, _ = self.timed(event(session_event("prompted")))
            release.set()
            router.drain()

        self.assertEqual((first, second, third), (expected, expected, expected))
        self.assertLess(max(first_elapsed, second_elapsed), 1.0)
        self.assertEqual(logs.output, [
            "WARNING:linear_agent_session.router:linear-agent-session: the shadow router recorded no decision: "
            "1 decisions are already waiting for the worker",
        ])

    def test_submit_adds_under_fifty_milliseconds_to_the_hook(self):
        release = threading.Event()
        self.addCleanup(router.drain)
        self.addCleanup(release.set)
        evt = event(session_event("prompted"))
        trace = router.Trace(home=self.home, route="linear", rule="intent_turn", intent=Intent(action="x"))
        with mock.patch.object(router, "record", side_effect=lambda *_: release.wait(10)):
            started = time.monotonic()
            for _ in range(20):
                router.submit(lambda: True, trace, evt, {"action": "rewrite", "text": "t"})
            per_call = (time.monotonic() - started) / 20
            release.set()
            router.drain()

        self.assertLess(per_call, 0.05)


class ClaimedPromptTest(RouterCase):
    def test_the_shadow_router_records_a_claimed_prompt_as_its_own_exit(self):
        self.assertTrue(plugin.missed_prompts.claim(str(self.home / "idempotency"), PROMPT_ID))
        hook = plugin.make_dispatch_hook(
            lambda: None, lambda: "linear", lambda: {}, lambda payload: None, None, lambda: self.home,
            lambda: str(self.home / "idempotency"), router_shadow=lambda: True,
        )
        key = "webhook:v2:late"
        asyncio.run(hook(event=prompted_event(chat_id=key)))
        router.drain()
        (path,) = (self.home / "webhook-router" / "decisions").glob("*.jsonl")
        (record,) = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(
            (record["chat_id"], record["action"], record["rule"], record["outcome"]),
            (key, "prompt_already_claimed", "prompt_already_claimed", "skip"),
        )


if __name__ == "__main__":
    unittest.main()
