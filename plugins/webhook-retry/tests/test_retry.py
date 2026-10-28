from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import PLUGIN_DIR, chat_key

RETRY = Path(PLUGIN_DIR) / "retry.py"
FAKE_HERMES = textwrap.dedent(
    """\
    import json, os, sys
    args = sys.argv[1:]
    if args[:2] == ["config", "get"]:
        if "routes.unknown." in args[2]:
            sys.stderr.write("Config key not set")
            sys.exit(1)
        print(json.dumps(["terminal", "linear_agent_session"]))
        sys.exit(0)
    query = open(args[args.index("--query-file") + 1]).read()
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"argv": args, "query": query, "home": os.environ["HERMES_HOME"]}) + "\\n")
    behaviors = json.load(open(os.environ["FAKE_BEHAVIOR"]))
    for marker, behavior in behaviors.items():
        if marker in query:
            break
    else:
        behavior = {"exit": 0}
    if behavior.get("garble"):
        open(os.environ["WEBHOOK_RETRY_REPORT"], "w").write("{not json")
    if behavior.get("usage"):
        with open(os.environ["WEBHOOK_RETRY_REPORT"], "w") as report:
            json.dump({"reason": "rate_limit", "message": "limit", "reset_seconds": behavior["usage"]}, report)
    sys.stderr.write(behavior.get("stderr", ""))
    sys.exit(behavior["exit"])
    """
)


FAKE_TRIAGE_EXIT = textwrap.dedent(
    """\
    import json, os, sys
    args = sys.argv[1:]
    payload = json.load(open(args[args.index("--payload-file") + 1]))
    now = args[args.index("--now") + 1] if "--now" in args else None
    with open(os.environ["FAKE_TRIAGE_LOG"], "a") as log:
        log.write(json.dumps({"argv": args[:2], "payload": payload, "now": now, "home": os.environ["HERMES_HOME"]}) + "\\n")
    behavior = json.load(open(os.environ["FAKE_TRIAGE_BEHAVIOR"]))
    if args[0] == "teammate-reply":
        print(json.dumps({"addressed": behavior.get("addressed", False)}))
        sys.exit(behavior.get("exit", 0))
    if args[0] == "triage-intent":
        if behavior.get("intent_exit"):
            sys.exit(behavior["intent_exit"])
        intent = json.dumps({"action": "card_reply", "now": now})
        print("## Computed triage intent\\n\\n<triage_intent>\\n" + intent + "\\n</triage_intent>\\n")
        sys.exit(0)
    if behavior.get("garble"):
        print("not json")
    else:
        print(json.dumps({"handled": behavior["handled"], "action": "archive", "task_id": "t_triage", "reason": "r"}))
    sys.exit(behavior.get("exit", 0))
    """
)
TRIAGE_EXIT_PAYLOAD = {"type": "Issue", "action": "update", "updatedFrom": {"stateId": "s"}, "data": {"id": "issue-1"}}


def ago(**delta) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(delivery, state="queued", delivered=None, next_try=None, attempts=0, route="linear", reported=True, **fields):
    key = chat_key(delivery, route)
    return key, {
        "key": key, "profile": "default", "route": route, "delivery": delivery,
        "delivered_at": delivered or ago(hours=1),
        "summary": {"event": "Issue", "action": "update", "subject": "PAT-1"},
        "prompt": f"prompt for {delivery}", "state": state, "attempts": attempts, "last_error": "",
        "next_try_at": next_try or ago(minutes=5), "updated_at": ago(hours=1), "reported": reported, **fields,
    }


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.queue = self.home / "webhook-retry"
        self.queue.mkdir(parents=True)
        self.log = root / "chat.log"
        self.behavior = root / "behavior.json"
        self.behavior.write_text("{}")
        self.hermes = root / "hermes"
        self.hermes.write_text(f"#!{sys.executable}\n{FAKE_HERMES}")
        os.chmod(self.hermes, 0o755)

    def seed(self, *pairs):
        (self.queue / "queue.json").write_text(json.dumps({"version": 1, "entries": dict(pairs)}))

    def entries(self):
        return json.loads((self.queue / "queue.json").read_text())["entries"]

    def behave(self, **behaviors):
        self.behavior.write_text(json.dumps(behaviors))

    def install_triage_exit(self, **behavior):
        cli = self.home / "plugins" / "linear-agent-session" / "cli.py"
        cli.parent.mkdir(parents=True)
        cli.write_text(FAKE_TRIAGE_EXIT)
        (self.home.parent / "triage.json").write_text(json.dumps(behavior))

    def triage_checks(self):
        log = self.home.parent / "triage.log"
        return [c for c in self.cli_calls(log) if c["argv"][0] == "triage-exit"]

    def teammate_checks(self):
        return [c for c in self.cli_calls(self.home.parent / "triage.log") if c["argv"][0] == "teammate-reply"]

    def engage_pause(self):
        (self.home / "ESTOP").write_text("")

    def intent_checks(self):
        return [c for c in self.cli_calls(self.home.parent / "triage.log") if c["argv"][0] == "triage-intent"]

    @staticmethod
    def cli_calls(log):
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def run_retry(self, *args):
        env = {
            "FAKE_TRIAGE_LOG": str(self.home.parent / "triage.log"),
            "FAKE_TRIAGE_BEHAVIOR": str(self.home.parent / "triage.json"),
            **os.environ, "HERMES_HOME": str(self.home), "HERMES_BIN": str(self.hermes),
            # Without these, find_hermes could reach a real hermes on PATH or in ~/.local/bin.
            "PATH": "/usr/bin:/bin", "HOME": str(self.home.parent),
            "FAKE_LOG": str(self.log), "FAKE_BEHAVIOR": str(self.behavior),
        }
        return subprocess.run([sys.executable, str(RETRY), *args], env=env, capture_output=True, text=True)

    def chats(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_due_entries_run_oldest_first_one_at_a_time(self):
        older = ago(hours=3)
        self.seed(entry("newer", delivered=ago(hours=2)), entry("older", delivered=older))

        result = self.run_retry()

        chats = self.chats()
        self.assertEqual([c["query"].rsplit("prompt for ", 1)[1] for c in chats], ["older", "newer"])
        self.assertEqual(chats[0]["argv"][:5], ["chat", "-Q", "--oneshot", "--query-file", chats[0]["argv"][4]])
        self.assertEqual(chats[0]["argv"][5:], ["-t", "terminal,linear_agent_session", "--source", "webhook-retry"])
        self.assertEqual(chats[0]["home"], str(self.home))
        self.assertIn(older, chats[0]["query"].split("prompt for")[0])
        self.assertEqual(
            result.stdout,
            "retried linear older: succeeded\nretried linear newer: succeeded\nwebhook retry queue drained\n",
        )
        self.assertEqual((result.returncode, self.entries()), (0, {}))
        self.assertIn("retried linear older: succeeded", (self.queue / "retry.log").read_text())

    def test_a_replayed_plain_triage_exit_archives_its_card_without_a_turn(self):
        self.install_triage_exit(handled=True)
        self.seed(entry("exit", payload=TRIAGE_EXIT_PAYLOAD))

        result = self.run_retry()

        self.assertEqual(self.chats(), [])
        self.assertEqual(
            self.triage_checks(),
            [{"argv": ["triage-exit", "--payload-file"], "payload": TRIAGE_EXIT_PAYLOAD, "now": None, "home": str(self.home)}],
        )
        self.assertEqual(
            result.stdout, "retried linear exit: handled the Triage exit without a turn\nwebhook retry queue drained\n"
        )
        self.assertEqual(self.entries(), {})

    def test_a_replayed_delivery_the_triage_exit_check_does_not_handle_runs_a_turn(self):
        for behavior in ({"handled": False}, {"handled": True, "exit": 1}, {"garble": True}):
            with self.subTest(behavior):
                self.setUp()
                self.install_triage_exit(**behavior)
                self.seed(entry("decide", payload=TRIAGE_EXIT_PAYLOAD))

                result = self.run_retry()

                self.assertEqual(len(self.triage_checks()), 1)
                self.assertEqual(len(self.chats()), 1)
                self.assertEqual(result.stdout, "retried linear decide: succeeded\nwebhook retry queue drained\n")

    def test_an_entry_without_a_payload_runs_a_turn_without_the_check(self):
        self.install_triage_exit(handled=True)
        self.seed(entry("old"))

        self.run_retry()

        self.assertEqual((len(self.triage_checks()), len(self.chats())), (0, 1))

    def test_a_replayed_linear_turn_starts_with_a_freshly_computed_intent_block(self):
        self.install_triage_exit(handled=False)
        delivered = ago(hours=2)
        session_payload = {"type": "AgentSessionEvent", "action": "prompted"}
        self.seed(entry("reply", delivered=delivered, payload=session_payload))

        self.run_retry()

        self.assertEqual(
            [(c["payload"], c["now"]) for c in self.intent_checks()], [(session_payload, delivered)]
        )
        self.assertEqual(self.triage_checks(), [], "an agent session event has no Triage exit")
        query = self.chats()[0]["query"]
        self.assertTrue(query.startswith("This is a replay of a webhook delivery"))
        self.assertLess(query.index("## Computed triage intent"), query.index("</triage_intent>"))
        self.assertLess(query.index("</triage_intent>"), query.index("prompt for reply"))
        self.assertIn(json.dumps({"action": "card_reply", "now": delivered}), query)

    def test_a_replay_whose_intent_cannot_be_computed_starts_with_the_error_intent(self):
        self.install_triage_exit(handled=False, intent_exit=1)
        self.seed(entry("broken", payload=TRIAGE_EXIT_PAYLOAD))

        self.run_retry()

        query = self.chats()[0]["query"]
        block = query[query.index("<triage_intent>\n") + len("<triage_intent>\n") : query.index("\n</triage_intent>")]
        self.assertEqual(json.loads(block)["action"], "error")
        self.assertLess(query.index("</triage_intent>"), query.index("prompt for broken"))

    def test_a_replay_without_the_cli_still_starts_with_the_error_intent(self):
        self.seed(entry("nocli", payload=TRIAGE_EXIT_PAYLOAD))

        self.run_retry()

        self.assertIn('"action": "error"', self.chats()[0]["query"])

    def test_a_delivery_without_a_linear_payload_replays_its_prompt_alone(self):
        self.install_triage_exit(handled=False)
        self.seed(entry("github"))

        self.run_retry()

        self.assertEqual(self.intent_checks(), [])
        self.assertNotIn("triage_intent", self.chats()[0]["query"])

    def test_entries_that_are_not_due_wait_and_now_forces_them(self):
        self.seed(entry("later", next_try=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")))

        quiet = self.run_retry()
        forced = self.run_retry("--now")

        self.assertEqual((quiet.stdout, len(self.chats())), ("", 1))
        self.assertEqual(forced.stdout, "retried linear later: succeeded\nwebhook retry queue drained\n")

    def test_a_usage_limit_keeps_the_entry_and_stops_the_run(self):
        self.seed(entry("first", delivered=ago(hours=3)), entry("second", delivered=ago(hours=2)))
        self.behave(**{"prompt for first": {"exit": 1, "usage": 7200}})

        result = self.run_retry()

        stored = self.entries()
        self.assertEqual(len(self.chats()), 1)
        self.assertEqual((stored[chat_key("first")]["state"], stored[chat_key("first")]["attempts"]), ("queued", 1))
        self.assertEqual(stored[chat_key("second")]["state"], "queued")
        next_try = dt.datetime.strptime(stored[chat_key("first")]["next_try_at"], "%Y-%m-%dT%H:%M:%SZ")
        seconds = (next_try.replace(tzinfo=dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)).total_seconds()
        self.assertAlmostEqual(seconds, 7200, delta=10)
        self.assertEqual(result.stdout, f"retried linear first: rate-limited again, next try {stored[chat_key('first')]['next_try_at']}\n")

    def test_a_later_delivery_waits_behind_an_earlier_one_that_is_not_due(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.seed(entry("reply", delivered=ago(hours=3), next_try=later), entry("done", delivered=ago(hours=2)))

        result = self.run_retry()

        self.assertEqual((result.stdout, self.chats()), ("", []))

    def test_an_unreadable_usage_report_counts_as_another_failure(self):
        self.seed(entry("garbled"))
        self.behave(**{"prompt for garbled": {"exit": 1, "garble": True}})

        result = self.run_retry()

        self.assertEqual(result.stdout, "retried linear garbled: failed (1)\nwebhook retry queue drained\n")
        self.assertEqual(self.entries()[chat_key("garbled")]["state"], "failed")

    def test_a_missing_hermes_leaves_the_entry_queued(self):
        self.seed(entry("waiting"))
        self.hermes.unlink()

        result = self.run_retry()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.entries()[chat_key("waiting")]["state"], "queued")

    def test_a_usage_limit_without_a_reset_backs_off(self):
        self.seed(entry("first", attempts=2))
        self.behave(**{"prompt for first": {"exit": 1, "usage": 1}})
        self.run_retry()
        stored = self.entries()[chat_key("first")]
        self.assertEqual(stored["attempts"], 3)

    def test_another_failure_marks_the_entry_failed_and_redacts_the_error(self):
        self.seed(entry("broken"))
        self.behave(**{"prompt for broken": {"exit": 3, "stderr": "boom with lin_api_abc123, lin_oauth_def and Bearer xyz"}})

        first = self.run_retry()
        second = self.run_retry("--now")

        stored = self.entries()[chat_key("broken")]
        self.assertEqual(first.stdout, "retried linear broken: failed (3)\nwebhook retry queue drained\n")
        self.assertEqual((stored["state"], stored["last_error"]), ("failed", "boom with [redacted], [redacted] and [redacted]"))
        self.assertEqual((second.stdout, len(self.chats())), ("", 1))

    def test_caps_give_up_once_and_expire_old_pending_entries(self):
        self.seed(
            entry("old", delivered=ago(hours=49)),
            entry("tired", attempts=8),
            entry("stale", state="pending", delivered=ago(hours=25), admitted=True),
            entry("fresh", state="pending", delivered=ago(hours=1), admitted=True),
        )

        first = self.run_retry()
        second = self.run_retry()

        self.assertEqual(
            first.stdout, "gave up linear old: older than 48 hours\ngave up linear tired: 8 attempts\n"
        )
        self.assertEqual(second.stdout, "")
        self.assertEqual(
            {e["delivery"]: e["state"] for e in self.entries().values()},
            {"old": "given_up", "tired": "given_up", "fresh": "pending"},
        )
        self.assertEqual(self.chats(), [])

    def test_a_paused_delivery_replays_once_after_resume_in_order_within_its_subject(self):
        self.seed(
            entry("second", delivered=ago(hours=1), reason="paused"),
            entry("first", delivered=ago(hours=2), reason="paused"),
        )
        self.engage_pause()
        held = self.run_retry()
        (self.home / "ESTOP").unlink()

        result = self.run_retry()

        self.assertEqual((held.stdout, held.returncode), ("Hermes is paused: the queued deliveries wait for hermes resume\n", 0))
        self.assertEqual([c["query"].rsplit("prompt for ", 1)[1] for c in self.chats()], ["first", "second"])
        self.assertEqual(
            result.stdout,
            "retried linear first: succeeded\nretried linear second: succeeded\nwebhook retry queue drained\n",
        )
        self.assertEqual(self.entries(), {})

    def test_a_pause_engaged_during_a_run_starts_no_further_entry(self):
        self.install_triage_exit(handled=False)
        self.seed(
            entry("first", delivered=ago(hours=3), reason="paused", payload=TRIAGE_EXIT_PAYLOAD),
            entry("second", delivered=ago(hours=2), reason="paused", payload=TRIAGE_EXIT_PAYLOAD),
        )
        pause = self.home / "ESTOP"
        fake = self.hermes.read_text().replace(
            "sys.exit(behavior[\"exit\"])", f"open({str(pause)!r}, 'w').close()\nsys.exit(behavior[\"exit\"])"
        )
        self.hermes.write_text(fake)

        result = self.run_retry("--now")

        stored = self.entries()
        self.assertEqual(
            [c["query"].rsplit("prompt for ", 1)[1] for c in self.chats()], ["first"],
        )
        self.assertEqual(len(self.triage_checks()), 1)
        self.assertEqual((stored[chat_key("second")]["state"], stored[chat_key("second")]["attempts"]), ("queued", 0))
        self.assertEqual(
            result.stdout,
            "retried linear first: succeeded\nHermes is paused: the queued deliveries wait for hermes resume\n",
        )

    def test_now_does_not_replay_while_paused(self):
        self.install_triage_exit(handled=True)
        self.seed(entry("held", reason="paused", payload=TRIAGE_EXIT_PAYLOAD))
        self.engage_pause()

        result = self.run_retry("--now")

        self.assertEqual((self.chats(), self.triage_checks()), ([], []))
        self.assertEqual(self.entries()[chat_key("held")]["state"], "queued")
        self.assertEqual(result.stdout, "Hermes is paused: the queued deliveries wait for hermes resume\n")

    def test_the_fleet_root_pause_holds_a_profile_home(self):
        fleet = Path(self.tmp.name) / "fleet"
        self.home = fleet / "profiles" / "worker"
        self.queue = self.home / "webhook-retry"
        self.queue.mkdir(parents=True)
        self.seed(entry("held", reason="paused"))
        (fleet / "ESTOP").write_text("")

        result = self.run_retry()

        self.assertEqual((self.chats(), result.stdout.splitlines()), ([], ["Hermes is paused: the queued deliveries wait for hermes resume"]))

    def test_a_pending_entry_that_never_reached_a_turn_replays_as_unadmitted(self):
        self.seed(
            entry("dropped", state="pending", delivered=ago(minutes=11), next_try_at=None),
            entry("recent", state="pending", delivered=ago(minutes=5), next_try_at=None),
        )

        result = self.run_retry()

        self.assertEqual([c["query"].rsplit("prompt for ", 1)[1] for c in self.chats()], ["dropped"])
        self.assertIn("because a restart, drain, or concurrency cap dropped it", self.chats()[0]["query"])
        self.assertEqual(result.stdout, "retried linear dropped: succeeded\nwebhook retry queue drained\n")
        self.assertEqual([e["delivery"] for e in self.entries().values()], ["recent"])

    def test_an_admitted_pending_entry_stays_pending(self):
        self.seed(entry("running", state="pending", delivered=ago(minutes=30), admitted=True))

        result = self.run_retry()

        self.assertEqual((result.stdout, self.chats()), ("", []))
        self.assertEqual(self.entries()[chat_key("running")]["state"], "pending")

    def test_an_old_unadmitted_entry_is_promoted_before_it_expires(self):
        self.seed(entry("stale", state="pending", delivered=ago(hours=25)))

        self.run_retry()

        self.assertEqual([c["query"].rsplit("prompt for ", 1)[1] for c in self.chats()], ["stale"])

    def test_a_reply_addressed_to_a_teammate_is_not_replayed(self):
        reply = {"type": "AgentSessionEvent", "action": "prompted"}
        for reason in ("paused", "unadmitted"):
            with self.subTest(reason):
                self.setUp()
                self.install_triage_exit(handled=False, addressed=True)
                state = "pending" if reason == "unadmitted" else "queued"
                self.seed(entry("talk", state=state, delivered=ago(minutes=30), reason=reason, payload=reply))

                result = self.run_retry()

                self.assertEqual(self.chats(), [])
                self.assertEqual([c["payload"] for c in self.teammate_checks()], [reply])
                self.assertEqual(
                    result.stdout,
                    "retried linear talk: skipped a reply addressed to a teammate\nwebhook retry queue drained\n",
                )
                self.assertEqual(self.entries(), {})

    def test_a_reply_for_the_agent_is_replayed(self):
        reply = {"type": "AgentSessionEvent", "action": "prompted"}
        self.install_triage_exit(handled=False, addressed=False)
        self.seed(entry("talk", payload=reply))

        self.run_retry()

        self.assertEqual((len(self.teammate_checks()), len(self.chats())), (1, 1))

    def test_a_failed_teammate_check_replays_the_turn(self):
        reply = {"type": "AgentSessionEvent", "action": "prompted"}
        self.install_triage_exit(handled=False, addressed=True, exit=1)
        self.seed(entry("talk", payload=reply))

        self.run_retry()

        self.assertEqual(len(self.chats()), 1)

    def test_two_deliveries_for_one_subject_replay_oldest_first(self):
        self.seed(entry("newer", delivered=ago(hours=1)), entry("older", delivered=ago(hours=2)))

        self.run_retry()

        self.assertEqual([c["query"].rsplit("prompt for ", 1)[1] for c in self.chats()], ["older", "newer"])

    def test_a_delivery_that_is_not_due_holds_back_only_its_own_subject(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def subject(name, delivered, next_try=None):
            key, value = entry(name, delivered=delivered, next_try=next_try)
            value["summary"] = {"event": "Issue", "action": "update", "subject": name[0].upper()}
            return key, value

        self.seed(
            subject("a-waiting", ago(hours=3), later),
            subject("a-behind", ago(hours=2)),
            subject("b-due", ago(hours=1)),
        )

        result = self.run_retry()

        self.assertEqual([c["query"].rsplit("prompt for ", 1)[1] for c in self.chats()], ["b-due"])
        self.assertEqual(result.stdout, "retried linear b-due: succeeded\n")

    def test_each_reason_is_named_in_the_replay_the_announcement_and_the_list(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        causes = {
            "usage_limit": "could not run then because the model's usage limit was reached.",
            "paused": "could not run then because Hermes was paused.",
            "unadmitted": "could not run then because it never reached an agent turn, because a restart, drain, or concurrency cap dropped it.",
        }
        for reason, cause in causes.items():
            with self.subTest(reason):
                self.setUp()
                self.seed(entry("d1", reason=reason, reported=False, next_try=later))
                announced = self.run_retry()
                listed = self.run_retry("--list")
                self.run_retry("--now")

                self.assertEqual(announced.stdout, f"queued linear d1 Issue update PAT-1: {reason} (next try {later})\n")
                self.assertTrue(listed.stdout.rstrip().endswith(f" {later} {reason}"))
                self.assertIn(cause, self.chats()[0]["query"])

    def test_an_entry_stored_before_reasons_reads_as_a_usage_limit(self):
        key, old = entry("old", reported=False)
        self.seed((key, old))
        self.assertNotIn("reason", old)

        result = self.run_retry("--list")

        self.assertTrue(result.stdout.rstrip().endswith(" usage_limit"))

    def test_a_route_without_toolsets_fails_without_running(self):
        self.seed(entry("lost", route="unknown"))

        result = self.run_retry()

        stored = self.entries()[chat_key("lost", "unknown")]
        self.assertEqual(result.stdout, "retried unknown lost: failed (1)\nwebhook retry queue drained\n")
        self.assertEqual((stored["state"], stored["last_error"], self.chats()), ("failed", "route unknown has no toolsets in the config", []))

    def test_a_retry_cut_short_is_never_run_again(self):
        self.seed(entry("cut", state="running"))

        first = self.run_retry("--now")
        second = self.run_retry("--now")

        stored = self.entries()[chat_key("cut")]
        self.assertEqual(first.stdout, "interrupted linear cut: not retried again, it may have run\n")
        self.assertEqual((second.stdout, stored["state"], self.chats()), ("", "failed", []))

    def test_each_waiting_entry_is_announced_once(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.seed(entry("d1", reported=False, next_try=later))

        first = self.run_retry()
        second = self.run_retry()

        self.assertEqual(
            first.stdout, f"queued linear d1 Issue update PAT-1: usage_limit (next try {later})\n"
        )
        self.assertEqual(second.stdout, "")

    def test_a_held_run_lock_runs_nothing(self):
        self.seed(entry("d1"))
        lock = os.open(self.queue / "run.lock", os.O_CREAT | os.O_RDWR)
        import fcntl

        fcntl.flock(lock, fcntl.LOCK_EX)
        self.addCleanup(os.close, lock)

        result = self.run_retry()

        self.assertEqual((result.returncode, result.stdout, self.chats()), (0, "", []))

    def test_list_never_shows_the_prompt_or_secrets(self):
        self.seed(entry("d1", prompt="steal lin_api_xxx", last_error="Bearer abc", attempts=2))

        result = self.run_retry("--list")

        stored = self.entries()[chat_key("d1")]
        self.assertEqual(
            result.stdout,
            f"queued linear d1 Issue update PAT-1 {stored['delivered_at']} 2 {stored['next_try_at']} usage_limit\n",
        )

    def test_list_when_empty(self):
        self.assertEqual(self.run_retry("--list").stdout, "no webhook deliveries are waiting\n")

    def test_drop_removes_by_delivery_or_reports_none(self):
        self.seed(entry("d1"), entry("d2"))

        dropped = self.run_retry("--drop", "d1")
        missing = self.run_retry("--drop", "nope")

        self.assertEqual((dropped.returncode, dropped.stdout), (0, "dropped linear d1\n"))
        self.assertEqual(missing.returncode, 1)
        self.assertEqual([e["delivery"] for e in self.entries().values()], ["d2"])

    def test_nothing_to_do_prints_nothing(self):
        self.assertEqual((self.run_retry().returncode, self.run_retry().stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
