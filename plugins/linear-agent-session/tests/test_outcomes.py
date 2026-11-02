from __future__ import annotations

import importlib.util
import os
import unittest

from tests import PLUGIN_DIR

spec = importlib.util.spec_from_file_location("outcomes", os.path.join(PLUGIN_DIR, "outcomes.py"))
outcomes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(outcomes)

ACTIVITY_TYPES = {
    "parked_in_triage": "elicitation",
    "needs_input": "elicitation",
    "capability": "error",
    "review_handoff": "response",
    "re_review": "response",
    "merge_queue": "response",
    "merged": "response",
}


class OutcomesTest(unittest.TestCase):
    def test_each_outcome_posts_its_activity_type(self) -> None:
        for outcome, expected in ACTIVITY_TYPES.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(outcomes.activity_type(outcome), expected)

    def test_an_unknown_outcome_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "no_such_outcome"):
            outcomes.activity_type("no_such_outcome")


if __name__ == "__main__":
    unittest.main()
