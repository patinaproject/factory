from __future__ import annotations

import datetime as dt
import enum
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import chat_key, load_plugin


class Platform(enum.Enum):
    WEBHOOK = "webhook"
    SLACK = "slack"


RECEIVED = dt.datetime(2026, 10, 15, 8, 30, 0, tzinfo=dt.timezone.utc)
CONFIG = {"platforms": {"webhook": {"extra": {"routes": {"linear": {"secret": "resolved"}}}}}}
PAYLOAD = {"type": "Issue", "action": "update", "data": {"identifier": "PAT-5290"}}


def event(delivery="d1", route="linear", platform=Platform.WEBHOOK, payload=PAYLOAD, text="triage this"):
    source = SimpleNamespace(platform=platform, user_id=f"webhook:{route}", chat_id=chat_key(delivery, route))
    return SimpleNamespace(source=source, raw_message=payload, text=text, timestamp=RECEIVED)


class FakeStore:
    def __init__(self, sessions):
        self.sessions = sessions

    def lookup_by_session_id(self, session_id):
        key = self.sessions.get(session_id)
        return SimpleNamespace(origin=SimpleNamespace(chat_id=key)) if key else None


class PluginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = CONFIG
        self.start()

    def start(self):
        self.plugin = load_plugin()
        for name, value in (
            ("hermes_home", lambda: self.home),
            ("load_config", lambda: self.config),
            ("reset_delay", self.plugin.queue_store.reset_delay_seconds),
        ):
            patcher = mock.patch.object(self.plugin, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def entries(self):
        path = self.home / "webhook-retry" / "queue.json"
        return json.loads(path.read_text())["entries"] if path.is_file() else {}

    def dispatch(self, ev, sessions=None):
        store = FakeStore(sessions if sessions is not None else {"s1": ev.source.chat_id})
        return self.plugin.record_delivery(event=ev, gateway=None, session_store=store)

    def test_usage_limit_failure_queues_the_delivery(self):
        ev = event()
        self.assertIsNone(self.dispatch(ev))

        self.plugin.note_request_error(
            session_id="s1", reason="rate_limit", error={"type": "RateLimitError", "message": "usage limit, resets in ~15h"}
        )

        entry = self.entries()[ev.source.chat_id]
        self.assertEqual(entry["state"], "queued")
        self.assertEqual(entry["prompt"], "triage this")
        self.assertEqual(entry["summary"], {"event": "Issue", "action": "update", "subject": "PAT-5290"})
        self.assertEqual((entry["profile"], entry["route"], entry["delivery"]), ("default", "linear", "d1"))
        self.assertEqual(entry["delivered_at"], "2026-10-15T08:30:00Z")
        self.assertEqual(entry["last_error"], "usage limit, resets in ~15h")
        self.assertEqual(entry["attempts"], 0)
        self.assertFalse(entry["reported"])
        wait = dt.datetime.strptime(entry["next_try_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        self.assertAlmostEqual((wait - dt.datetime.now(dt.timezone.utc)).total_seconds(), 15 * 3600, delta=10)

    def test_a_short_or_missing_reset_waits_thirty_minutes(self):
        ev = event()
        self.dispatch(ev)

        self.plugin.note_request_error(
            session_id="s1", reason="upstream_rate_limit", error={"message": "retry after 20 seconds"}
        )

        wait = dt.datetime.strptime(self.entries()[ev.source.chat_id]["next_try_at"], "%Y-%m-%dT%H:%M:%SZ")
        seconds = (wait.replace(tzinfo=dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)).total_seconds()
        self.assertAlmostEqual(seconds, 1800, delta=10)

    def test_a_linear_issue_delivery_keeps_its_payload_for_the_replay_check(self):
        ev = event()
        self.dispatch(ev)
        self.assertEqual(self.entries()[ev.source.chat_id]["payload"], PAYLOAD)

    def test_a_linear_agent_session_delivery_keeps_its_payload_for_the_replay_intent(self):
        session = {"type": "AgentSessionEvent", "action": "prompted"}
        ev = event(payload=session)
        self.dispatch(ev)
        self.assertEqual(self.entries()[ev.source.chat_id]["payload"], session)

    def test_other_deliveries_keep_no_payload(self):
        github = {"action": "closed", "pull_request": {"number": 1}, "repository": {"full_name": "o/r"}}
        other = {"type": "Comment", "action": "create"}
        for name, payload in (("github", github), ("other linear type", other), ("not a dict", "raw")):
            with self.subTest(name):
                ev = event(delivery=name, payload=payload)
                self.dispatch(ev)
                self.assertIsNone(self.entries()[ev.source.chat_id]["payload"])

    def test_a_turn_that_finishes_leaves_nothing_behind(self):
        ev = event()
        self.dispatch(ev)

        self.plugin.clear_finished_turn(session_id="s1", completed=True)

        self.assertEqual(self.entries(), {})

    def test_other_errors_do_not_queue(self):
        ev = event()
        self.dispatch(ev)

        self.plugin.note_request_error(session_id="s1", reason="auth", error={"message": "bad key"})

        self.assertEqual(self.entries()[ev.source.chat_id]["state"], "pending")

    def test_a_fallback_rescue_removes_a_queued_delivery(self):
        ev = event()
        self.dispatch(ev)
        self.plugin.note_request_error(session_id="s1", reason="rate_limit", error={"message": "limit"})

        self.plugin.clear_finished_turn(session_id="s1", completed=True)

        self.assertEqual(self.entries(), {})

    def test_an_unresolved_secret_is_not_recorded(self):
        self.config = {"platforms": {"webhook": {"extra": {"secret": "${LINEAR_WEBHOOK_SECRET}"}}}}

        self.dispatch(event())

        self.assertEqual(self.entries(), {})

    def test_a_route_secret_overrides_an_unresolved_global_secret(self):
        self.config = {"platforms": {"webhook": {"extra": {"secret": "${X}", "routes": {"linear": {"secret": "ok"}}}}}}

        ev = event()
        self.dispatch(ev)

        self.assertEqual(list(self.entries()), [ev.source.chat_id])

    def test_a_slack_event_is_not_recorded(self):
        slack = event(platform=Platform.SLACK)
        slack.source.chat_id = "C0123"

        self.dispatch(slack)

        self.assertEqual(self.entries(), {})

    def test_the_queue_survives_reloading_the_plugin(self):
        ev = event()
        self.dispatch(ev)
        self.plugin.note_request_error(session_id="s1", reason="rate_limit", error={"message": "limit"})

        self.start()
        self.plugin.clear_finished_turn(session_id="unknown")

        self.assertEqual(self.entries()[ev.source.chat_id]["state"], "queued")
        self.assertEqual(oct((self.home / "webhook-retry" / "queue.json").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.home / "webhook-retry").stat().st_mode & 0o777), "0o700")

    def test_a_redelivery_does_not_resurrect_or_duplicate_a_queued_entry(self):
        ev = event()
        self.dispatch(ev)
        self.plugin.note_request_error(session_id="s1", reason="rate_limit", error={"message": "limit"})

        self.dispatch(event(text="changed"))

        self.assertEqual([(e["state"], e["prompt"]) for e in self.entries().values()], [("queued", "triage this")])

    def test_github_summary_comes_from_fixed_fields(self):
        payload = {"repository": {"full_name": "patinaproject/factory"}, "pull_request": {"number": 7, "title": "secret"}}
        text = "intro\n- Event: pull_request, action opened\n- Repository: x"

        self.dispatch(event(route="github", payload=payload, text=text))

        entry = next(iter(self.entries().values()))
        self.assertEqual(entry["summary"], {"event": "pull_request", "action": "opened", "subject": "patinaproject/factory#7"})

    def test_linear_timestamp_sets_delivered_at(self):
        millis = int(dt.datetime(2026, 10, 14, 1, 2, 3, tzinfo=dt.timezone.utc).timestamp() * 1000)

        self.dispatch(event(payload={**PAYLOAD, "webhookTimestamp": millis}))

        self.assertEqual(next(iter(self.entries().values()))["delivered_at"], "2026-10-14T01:02:03Z")

    def test_a_retry_process_writes_its_report_and_touches_no_queue(self):
        report = self.home / "report.json"
        with mock.patch.dict(os.environ, {"WEBHOOK_RETRY_REPORT": str(report)}):
            self.plugin.note_request_error(
                session_id="", reason="rate_limit", error={"message": "usage limit, resets in ~45 min"}
            )
            self.plugin.note_request_error(session_id="", reason="auth", error={"message": "no"})

        self.assertEqual(
            json.loads(report.read_text()),
            {"reason": "rate_limit", "message": "usage limit, resets in ~45 min", "reset_seconds": 2700.0},
        )
        self.assertEqual(self.entries(), {})

    def test_reset_delays(self):
        parse = self.plugin.queue_store.reset_delay_seconds
        self.assertEqual(parse("resets in ~14h 46m"), 14 * 3600 + 46 * 60)
        self.assertEqual(parse("resets in ~15h"), 15 * 3600)
        self.assertEqual(parse("resets in ~45 min"), 2700)
        self.assertEqual(parse("Retry after 90 seconds"), 90)
        self.assertIsNone(parse("slow down"))

    def test_broken_input_never_raises(self):
        with self.assertLogs(level="ERROR"):
            broken = SimpleNamespace(source=None)
            self.assertIsNone(self.plugin.record_delivery(event=broken, session_store=object()))
            self.assertIsNone(self.plugin.record_delivery(event=None))
            self.assertIsNone(self.plugin.note_request_error(session_id="s1", reason="rate_limit", error="not a dict"))
            self.assertIsNone(self.plugin.clear_finished_turn(session_id=None))
            self.plugin._session_store = object()
            self.assertIsNone(self.plugin.clear_finished_turn(session_id="s1"))
            self.assertIsNone(self.plugin.note_request_error(session_id="s1", reason="rate_limit", error={}))

    def test_a_session_without_an_origin_is_ignored_quietly(self):
        ev = event()
        self.dispatch(ev)
        self.plugin._session_store = SimpleNamespace(lookup_by_session_id=lambda _: SimpleNamespace(origin=None))

        with self.assertNoLogs(level="ERROR"):
            self.plugin.clear_finished_turn(session_id="slack-session")

        self.assertEqual(self.entries()[ev.source.chat_id]["state"], "pending")

    def test_registers_the_three_hooks(self):
        registered = {}
        ctx = SimpleNamespace(register_hook=lambda name, fn: registered.update({name: fn}))

        self.plugin.register(ctx)

        self.assertEqual(set(registered), {"pre_gateway_dispatch", "api_request_error", "on_session_end"})


if __name__ == "__main__":
    unittest.main()
