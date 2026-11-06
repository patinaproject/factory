from __future__ import annotations

import importlib.util
import json
import stat
import sys
import unittest
from pathlib import Path

from tests import PLUGIN_DIR, chat_key
from tests.test_retry import ROUTER_OFF_CONFIG, TRIAGE_EXIT_PAYLOAD, RetryCase, ago, entry

LINEAR_AGENT_SESSION = Path(PLUGIN_DIR).parent / "linear-agent-session"
FIELDS = {
    "version", "recorded_at", "chat_id", "profile", "route", "delivery", "attempt", "retry", "session_id", "action",
    "then", "rule", "outcome",
}


def load_router():
    spec = importlib.util.spec_from_file_location(
        "linear_agent_session_for_retry", LINEAR_AGENT_SESSION / "__init__.py",
        submodule_search_locations=[str(LINEAR_AGENT_SESSION)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.router


router = load_router()


class RetryRecordTest(RetryCase):
    def records(self):
        directory = self.home / "webhook-router" / "decisions"
        if not directory.is_dir():
            return []
        return [json.loads(line) for path in sorted(directory.glob("*.jsonl")) for line in path.read_text().splitlines()]

    def decisions(self):
        return [(r["delivery"], r["action"], r["then"], r["rule"], r["outcome"], r["session_id"]) for r in self.records()]

    def assert_router_actions(self):
        for record in self.records():
            self.assertEqual(set(record), FIELDS)
            self.assertEqual((record["version"], record["retry"]), (1, True))
            self.assertIn(record["action"], router.ACTIONS)
            self.assertTrue(record["then"] is None or record["then"] in router.ACTIONS, record)

    def test_a_succeeded_replay_records_its_intent_and_the_session_it_started(self):
        self.install_triage_exit(handled=False)
        self.seed(entry("reply", attempts=2, payload={"type": "AgentSessionEvent", "action": "prompted"}))
        self.behave(**{"prompt for reply": {"exit": 0, "session": "20261106_101500_abc123"}})

        self.run_retry()

        (record,) = self.records()
        self.assertEqual(
            {k: record[k] for k in ("chat_id", "profile", "route", "delivery", "attempt", "session_id")},
            {"chat_id": chat_key("reply"), "profile": "default", "route": "linear", "delivery": "reply", "attempt": 3,
             "session_id": "20261106_101500_abc123"},
        )
        self.assertEqual(self.decisions(), [
            ("reply", "replay_succeeded", "card_reply", "replay", "succeeded", "20261106_101500_abc123"),
        ])
        self.assert_router_actions()

    def test_each_retry_outcome_records_one_action_of_the_router_table(self):
        self.install_triage_exit(handled=False)
        self.seed(
            entry("plain", delivered=ago(hours=9)),
            entry("broken", delivered=ago(hours=8)),
            entry("limited", delivered=ago(hours=7)),
            entry("old", delivered=ago(hours=49)),
            entry("cut", state="running", delivered=ago(hours=6)),
        )
        self.behave(**{
            "prompt for broken": {"exit": 3, "session": "s-broken"},
            "prompt for limited": {"exit": 1, "usage": 600},
        })

        self.run_retry()

        self.assertEqual(sorted(self.decisions()), sorted([
            ("cut", "retry_interrupted", None, "running_at_start", "interrupted", None),
            ("old", "retry_gave_up", None, "too_old", "gave_up", None),
            ("plain", "replay_succeeded", None, "replay", "succeeded", None),
            ("broken", "replay_failed", None, "replay_failed", "failed", "s-broken"),
            ("limited", "replay_rate_limited", None, "usage_limit", "rate_limited", None),
        ]))
        self.assert_router_actions()

    def test_the_no_turn_exits_a_pause_and_a_drop_each_record_one_action(self):
        self.install_triage_exit(handled=True, addressed=True, skip=True)
        self.seed(
            entry("exit", delivered=ago(hours=3), payload=TRIAGE_EXIT_PAYLOAD),
            entry("teammate", delivered=ago(hours=2), payload={"type": "AgentSessionEvent", "action": "prompted"}),
            entry("comment", delivered=ago(hours=1), payload={"type": "Comment", "action": "create"}),
        )
        self.run_retry()
        self.seed(entry("held"), entry("unwanted", delivered=ago(hours=2)))
        self.engage_pause()
        self.run_retry()
        self.run_retry("--drop", "unwanted")

        self.assertEqual(self.decisions(), [
            ("exit", "retry_triage_exit_handled", None, "triage_exit", "handled", None),
            ("teammate", "retry_teammate_reply", None, "teammate_reply", "handled", None),
            ("comment", "retry_comment_skipped", None, "comment_skip", "handled", None),
            ("unwanted", "retry_paused", None, "paused", "waiting", None),
            ("unwanted", "retry_dropped", None, "operator_drop", "dropped", None),
        ])
        self.assert_router_actions()

    def test_records_are_private(self):
        self.seed(entry("plain"))

        self.run_retry()

        root = self.home / "webhook-router"
        (path,) = (root / "decisions").iterdir()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((root / "decisions").stat().st_mode), 0o700)

    def test_the_setting_off_records_nothing(self):
        (self.home / "config.yaml").write_text(ROUTER_OFF_CONFIG)
        self.seed(entry("plain"))

        result = self.run_retry()

        self.assertEqual(result.stdout, "retried linear plain: succeeded\nwebhook retry queue drained\n")
        self.assertFalse((self.home / "webhook-router").exists())


if __name__ == "__main__":
    unittest.main()
