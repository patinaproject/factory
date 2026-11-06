from __future__ import annotations

import asyncio
import datetime as dt
import enum
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import load_plugin

plugin = load_plugin()
missed_prompts = plugin.missed_prompts
triage_intent = plugin.triage_intent

APP = "app-user-1"
NOW = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.timezone.utc)
PROMPT_ID = "0a1b2c3d-0000-4000-8000-000000000001"
OTHER_ID = "0a1b2c3d-0000-4000-8000-000000000002"
WINDOW = dt.timedelta(hours=24)
GRACE = dt.timedelta(minutes=10)


def iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def prompt(activity_id=PROMPT_ID, age=dt.timedelta(minutes=15), author_app=False, signal=None, owner=APP, session="session-1"):
    return {
        "id": activity_id,
        "createdAt": iso(NOW - age),
        "ephemeral": False,
        "signal": signal,
        "promptAuthor": {"id": "user-ted", "app": author_app},
        "agentSession": {"id": session, "appUser": {"id": owner}, "issue": {"id": "issue-1", "identifier": "ABC-1"}},
        "content": {"__typename": "AgentActivityPromptContent", "body": "Any update?"},
    }


class FakeClient:
    def __init__(self, prompts, newest=None, error=None):
        self.prompts = prompts
        self.newest = newest or {}
        self.error = error
        self.since = []

    def recent_prompts(self, since):
        if self.error:
            raise self.error
        self.since.append(since)
        return self.prompts

    def newest_activity_id(self, session_id):
        return self.newest.get(session_id, PROMPT_ID)


def authorized(payload, moment):
    return triage_intent.Intent(action="resume_card", authorized_as="assignee")


class SweepTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = home.name
        self.store = os.path.join(self.home, "idempotency")
        self.marker = os.path.join(self.home, "missed-prompts.json")
        self.write_marker(NOW - dt.timedelta(days=2))
        self.intents = []

    def write_marker(self, moment):
        with open(self.marker, "w") as handle:
            json.dump({"since": iso(moment)}, handle)

    def sweep(self, client, compute=authorized):
        def record(payload, moment):
            self.intents.append((payload["agentActivity"]["id"], moment))
            return compute(payload, moment)

        return missed_prompts.sweep(
            client=client, app_user_id=APP, compute_intent=record, store=self.store, marker=self.marker,
            now=NOW, window=WINDOW, grace=GRACE,
        )

    def test_an_unanswered_authorized_prompt_is_output_once_as_its_webhook_payload(self):
        found = self.sweep(FakeClient([prompt()]))
        self.assertEqual(found, [{
            "type": "AgentSessionEvent",
            "action": "prompted",
            "appUserId": APP,
            "createdAt": "2026-11-06T11:45:00.000Z",
            "webhookTimestamp": 1793965500000,
            "agentSession": {"id": "session-1", "issue": {"id": "issue-1", "identifier": "ABC-1"}},
            "agentActivity": {"id": PROMPT_ID, "signal": None, "userId": "user-ted", "content": {"body": "Any update?"}},
        }])
        self.assertEqual(self.intents, [(PROMPT_ID, NOW - dt.timedelta(minutes=15))])

    def test_two_sweeps_in_a_row_output_the_prompt_once(self):
        client = FakeClient([prompt()])
        self.assertEqual(len(self.sweep(client)), 1)
        self.assertEqual(self.sweep(client), [])

    def test_a_prompt_the_live_hook_claimed_is_not_output(self):
        self.assertTrue(missed_prompts.claim(self.store, PROMPT_ID))
        self.assertEqual(self.sweep(FakeClient([prompt()])), [])
        self.assertEqual(self.intents, [])

    def test_a_prompt_whose_session_has_a_newer_activity_is_not_output(self):
        self.assertEqual(self.sweep(FakeClient([prompt()], newest={"session-1": OTHER_ID})), [])

    def test_prompts_from_an_app_with_a_stop_signal_or_in_another_apps_session_are_not_output(self):
        cases = {
            "app author": prompt(author_app=True),
            "stop": prompt(signal="stop"),
            "other app's session": prompt(owner="app-user-2"),
            "no author": {**prompt(), "promptAuthor": None},
            "ephemeral": {**prompt(), "ephemeral": True},
        }
        for name, node in cases.items():
            with self.subTest(name):
                self.assertEqual(self.sweep(FakeClient([node])), [])

    def test_a_prompt_from_a_person_outside_the_issues_authority_is_not_output(self):
        stranger = lambda payload, moment: triage_intent.Intent(action="resume_card", authorized_as=None)
        self.assertEqual(self.sweep(FakeClient([prompt()]), stranger), [])
        self.assertFalse(plugin.idempotency.recorded(self.store, PROMPT_ID))

    def test_a_prompt_that_needs_no_turn_is_not_output(self):
        no_turn = lambda payload, moment: triage_intent.Intent(action="ignore_factory_session", authorized_as="assignee")
        self.assertEqual(self.sweep(FakeClient([prompt()]), no_turn), [])

    def test_prompts_inside_the_grace_period_or_before_the_window_or_the_marker_are_not_output(self):
        for name, node, marker in [
            ("younger than the grace period", prompt(age=dt.timedelta(minutes=5)), NOW - dt.timedelta(days=2)),
            ("older than the window", prompt(age=dt.timedelta(hours=25)), NOW - dt.timedelta(days=2)),
            ("older than the marker", prompt(age=dt.timedelta(hours=2)), NOW - dt.timedelta(hours=1)),
        ]:
            with self.subTest(name):
                self.write_marker(marker)
                self.assertEqual(self.sweep(FakeClient([node])), [])

    def test_the_read_starts_at_the_later_of_the_window_and_the_marker(self):
        client = FakeClient([])
        self.sweep(client)
        self.write_marker(NOW - dt.timedelta(hours=3))
        self.sweep(client)
        self.assertEqual(client.since, ["2026-11-05T12:00:00Z", "2026-11-06T09:00:00Z"])

    def test_the_first_sweep_writes_the_marker_and_outputs_nothing(self):
        os.unlink(self.marker)
        self.assertEqual(self.sweep(FakeClient([prompt()])), [])
        with open(self.marker) as handle:
            self.assertEqual(json.load(handle), {"since": "2026-11-06T12:00:00Z"})
        self.assertEqual(self.intents, [])

    def test_a_linear_error_or_an_error_intent_claims_nothing_and_raises(self):
        with self.assertRaises(plugin.linear.LinearError):
            self.sweep(FakeClient([], error=plugin.linear.LinearError("HTTP 500")))
        def second_fails(payload, moment):
            if payload["agentActivity"]["id"] == PROMPT_ID:
                return authorized(payload, moment)
            return triage_intent.error_intent("HTTPError")

        two = FakeClient([prompt(), prompt(OTHER_ID, session="session-2")], newest={"session-2": OTHER_ID})
        with self.assertRaises(missed_prompts.SweepError):
            self.sweep(two, second_fails)
        self.assertFalse(plugin.idempotency.recorded(self.store, PROMPT_ID))
        self.assertEqual([p["agentActivity"]["id"] for p in self.sweep(two)], [PROMPT_ID, OTHER_ID])


class Platform(enum.Enum):
    WEBHOOK = "webhook"


def prompted_event(activity_id=PROMPT_ID, chat_id="webhook:v2:chat-1"):
    payload = missed_prompts.payload_of(prompt(activity_id), APP)
    source = SimpleNamespace(platform=Platform.WEBHOOK, user_id="webhook:linear", chat_type="webhook", chat_id=chat_id)
    return SimpleNamespace(source=source, raw_message=payload, text="rendered prompt")


class HookClaimTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        self.store = str(self.home / "idempotency")

    def run_hook(self, evt, paused=False):
        hook = plugin.make_dispatch_hook(
            lambda: (_ for _ in ()).throw(AssertionError("no Linear call for a claimed prompt")),
            lambda: "linear",
            lambda: {"platforms": {"webhook": {"extra": {"routes": {"linear": {"secret": "resolved"}}}}}},
            lambda payload: None,
            None,
            lambda: self.home,
            lambda: self.store,
        )
        original = plugin.paused
        plugin.paused = lambda: paused
        try:
            return asyncio.run(hook(event=evt, gateway=object(), session_store=None))
        finally:
            plugin.paused = original

    def skip_record(self, chat_id):
        return self.home / "webhook-retry" / "skipped" / hashlib.sha256(chat_id.encode()).hexdigest()

    def test_a_live_delivery_of_a_prompt_the_sweep_claimed_is_skipped_with_a_skip_record(self):
        self.assertTrue(missed_prompts.claim(self.store, PROMPT_ID))
        answer = self.run_hook(prompted_event(chat_id="webhook:v2:late"))
        self.assertEqual(answer, {"action": "skip", "reason": "the session prompt was already answered or queued"})
        self.assertTrue(self.skip_record("webhook:v2:late").exists())

    def test_a_delivery_while_paused_claims_its_prompt_so_the_sweep_leaves_it(self):
        self.assertIsNone(self.run_hook(prompted_event(), paused=True))
        self.assertTrue(plugin.idempotency.recorded(self.store, PROMPT_ID))
        self.assertFalse(missed_prompts.claim(self.store, PROMPT_ID))

    def test_a_redelivery_of_a_prompt_the_hook_already_claimed_is_skipped(self):
        self.assertIsNone(self.run_hook(prompted_event(), paused=True))
        self.assertEqual(self.run_hook(prompted_event(chat_id="webhook:v2:again"), paused=True)["action"], "skip")


class ClaimTest(unittest.TestCase):
    def test_a_claim_succeeds_once_and_a_reserved_record_counts_as_claimed(self):
        with tempfile.TemporaryDirectory() as store:
            self.assertTrue(missed_prompts.claim(store, PROMPT_ID))
            self.assertFalse(missed_prompts.claim(store, PROMPT_ID))
            reserved = os.path.join(store, hashlib.sha256(OTHER_ID.encode()).hexdigest() + ".json")
            with open(reserved, "w") as handle:
                json.dump({"state": "reserved", "args_digest": "x"}, handle)
            self.assertFalse(missed_prompts.claim(store, OTHER_ID))


if __name__ == "__main__":
    unittest.main()
