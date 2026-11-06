import json
import tempfile
import unittest
from pathlib import Path

from patina_evals import schema
from tests.support import run_synthetic, synthetic_case

PRICED_MODEL = "claude-opus-5-5"
USAGE = [{"scope": "worker", "agent": "main", "model": PRICED_MODEL, "message_id": "m1",
          "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}]


class ExitCodeTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)

    def tearDown(self):
        self.scratch.cleanup()

    def run_cases(self, *args):
        code, result, output = run_synthetic(self.root, "--split", "development", *args)
        self.assertIsNotNone(result, output)
        schema.validate(result)
        return code, result

    def test_passing_required_case_exits_zero(self):
        synthetic_case(self.root, "good")
        code, result = self.run_cases("--suite", "regression")
        self.assertEqual((code, result["summary"]["exit_reason"]), (0, "ok"))

    def test_deterministic_required_failure_exits_one(self):
        synthetic_case(self.root, "bad", reference="no")
        code, result = self.run_cases("--suite", "regression")
        self.assertEqual((code, result["summary"]["required_failed"]), (1, ["bad"]))

    def test_incomplete_required_run_exits_three(self):
        synthetic_case(self.root, "broken", reference_stream=[{"tool": "no-such-tool", "args": []}])
        code, result = self.run_cases("--suite", "regression")
        self.assertEqual((code, result["summary"]["incomplete"]), (3, ["broken"]))
        self.assertEqual(result["cases"][0]["trials"][0]["outcome"], "error")

    def test_parity_failure_exits_four(self):
        synthetic_case(self.root, "good")
        empty = self.root / "differences.json"
        empty.write_text(json.dumps({"schema_version": 1, "differences": []}), encoding="utf-8")
        code, result = self.run_cases("--suite", "regression", "--differences", str(empty))
        self.assertEqual((code, result["summary"]["exit_reason"]), (4, "parity_invalid"))
        self.assertFalse(result["parity"]["valid"])

    def test_capability_only_failure_passes_with_a_diagnostic(self):
        synthetic_case(self.root, "good")
        synthetic_case(self.root, "stretch", suite="capability", reference="no")
        code, result = self.run_cases("--suite", "all")
        self.assertEqual(code, 0)
        self.assertEqual(result["summary"]["capability_failed"], ["stretch"])
        self.assertEqual(len(result["summary"]["diagnostics"]), 1)
        self.assertFalse([c for c in result["cases"] if c["id"] == "stretch"][0]["required"])

    def test_missing_accounting_fails_a_cost_assertion_and_is_never_zero(self):
        synthetic_case(self.root, "good")
        code, result = self.run_cases("--suite", "regression", "--require-cost", "experiment_total.cost_usd")
        self.assertEqual(code, 5)
        assertion = result["summary"]["cost_assertions"][0]
        self.assertEqual((assertion["status"], assertion["value"]), ("unknown", None))
        self.assertIsNone(result["cost"]["experiment_total"]["cost_usd"]["value"])

    def test_recorded_usage_satisfies_a_cost_assertion(self):
        synthetic_case(self.root, "good", usage=USAGE)
        code, result = self.run_cases(
            "--suite", "regression", "--require-cost", "experiment_total.cost_usd",
            "--require-cost", "worker_cost_per_successful_card",
        )
        self.assertEqual(code, 0, result["summary"])
        known = [a["value"] for a in result["summary"]["cost_assertions"]]
        self.assertEqual(known[0], known[1])
        self.assertGreater(known[0], 0)


if __name__ == "__main__":
    unittest.main()
