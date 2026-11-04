from __future__ import annotations

import sys
import unittest

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import stuck_reasons  # noqa: E402
from stuck_reasons import Facts, classify  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def test_the_earliest_reason_wins_a_tie(self) -> None:
        ties = [
            (Facts("review", draft=True, approval_stale=True, behind=True, branch_updatable=True), "draft"),
            (Facts("review", approval_stale=True, behind=True, branch_updatable=True), "approval_stale"),
            (Facts("review", approval_stale=True, dequeued=True, base="failed"), "approval_stale"),
            (Facts("review", dequeued=True, base="failed", flaky_failed=True), "dequeued_base_failure"),
            (Facts("review", dequeued=True, base="passed"), "dequeued_own_failure"),
            (Facts("review", dequeued=True, base="passed", base_escalated=True), "dequeued_base_failure"),
            (Facts("review", flaky_failed=True, behind=True, branch_updatable=True), "flaky_check"),
            (Facts("review", behind=True, branch_updatable=True, awaiting_queue=True), "behind_base"),
            (Facts("review", awaiting_queue=True), "awaiting_queue_entry"),
        ]
        for facts, name in ties:
            with self.subTest(name=name, facts=facts):
                self.assertEqual(classify(facts).name, name)

    def test_an_unsettled_input_gives_no_reason(self) -> None:
        self.assertIsNone(classify(Facts("review", unsettled=True, approval_stale=True, behind=True, branch_updatable=True)))

    def test_a_behind_branch_the_sweep_may_not_update_gives_no_reason(self) -> None:
        self.assertIsNone(classify(Facts("review", behind=True, branch_updatable=False)))

    def test_the_new_reasons_act_only_on_a_review_card(self) -> None:
        for status in ("blocked", "triage"):
            with self.subTest(status=status):
                self.assertIsNone(classify(Facts(status, approval_stale=True, behind=True, branch_updatable=True, awaiting_queue=True)))


class SettingsTest(unittest.TestCase):
    def test_a_missing_or_malformed_requeue_is_none(self) -> None:
        for entry in (None, {}, {"requeue": "label"}, {"requeue": {"label": ""}}, {"requeue": {"command": 5}}, {"requeue": ["queue"]}):
            with self.subTest(entry=entry):
                self.assertEqual(stuck_reasons.requeue_mechanism(entry), stuck_reasons.Requeue("none"))

    def test_a_declared_requeue_is_read(self) -> None:
        self.assertEqual(stuck_reasons.requeue_mechanism({"requeue": {"label": " queue "}}), stuck_reasons.Requeue("label", "queue"))
        self.assertEqual(stuck_reasons.requeue_mechanism({"requeue": {"command": "@queue"}}), stuck_reasons.Requeue("command", "@queue"))

    def test_updates_branch_is_true_only_for_true(self) -> None:
        self.assertEqual([stuck_reasons.updates_branch({"updates_branch": v}) for v in (True, "true", 1, None)], [True, False, False, False])


if __name__ == "__main__":
    unittest.main()
