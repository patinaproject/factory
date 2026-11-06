import json
import os
import tempfile
import unittest
from pathlib import Path

from patina_evals import schema
from tests.support import Listener, run_cli, run_synthetic, synthetic_case

PROVIDER_VARS = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "GOOGLE_GEMINI_BASE_URL", "MISTRAL_BASE_URL", "GROQ_BASE_URL")


class SmokeRunTest(unittest.TestCase):
    """The documented command on the committed fixtures, with every provider endpoint pointed at a counting socket."""

    @classmethod
    def setUpClass(cls):
        cls.listener = Listener()
        env = dict.fromkeys(PROVIDER_VARS, cls.listener.url) | {"EVALS_BLOCKED_URL": cls.listener.url}
        cls.code, cls.result, cls.output = run_cli("run", "--split", "development", "--suite", "regression", env=env)
        cls.listener.close()

    def test_exits_zero(self):
        self.assertEqual(self.code, 0, self.output)

    def test_no_model_or_provider_endpoint_was_reached(self):
        self.assertEqual(self.listener.connections, 0)
        self.assertEqual(self.result["run"]["model_calls"], 0)
        self.assertEqual(self.result["run"]["model"], "mockllm/model")

    def test_result_validates_against_the_schema(self):
        schema.validate(self.result)
        self.assertEqual(self.result["schema_version"], 1)

    def test_runs_the_four_smoke_fixtures(self):
        self.assertEqual(
            sorted(c["id"] for c in self.result["cases"]),
            ["token-spend-supported-usage", "token-spend-unknown-cost", "triage-priority-allowed", "triage-priority-human-set"],
        )

    def test_every_reference_passes_and_every_near_miss_fails_its_target(self):
        for case in self.result["cases"]:
            for trial in case["trials"]:
                with self.subTest(case=case["id"], solution=trial["solution"]):
                    self.assertEqual(trial["outcome"], "ok")
                    if trial["solution"] == "reference":
                        self.assertEqual(trial["verdict"], "pass")
                    else:
                        self.assertEqual(trial["verdict"], "fail")
                        hit = [t for t in trial["targets"] if trial["checks"].get(t) is False or trial["forbidden"].get(t) is True]
                        self.assertTrue(hit, trial)

    def test_marks_results_as_plumbing_validation(self):
        self.assertEqual(self.result["evidence"]["type"], "plumbing_validation")
        self.assertIs(self.result["evidence"]["agent_reliability_evidence"], False)
        self.assertIn("PLUMBING VALIDATION ONLY", self.output)

    def test_records_resources_concurrency_and_timeouts(self):
        resources = self.result["resources"]
        self.assertEqual(resources["concurrency"], 2)
        self.assertEqual(resources["timeouts"], {"command_seconds": 120, "trial_seconds": 600})
        self.assertEqual(resources["cpu"]["ceiling_cpu_seconds_per_command"], 60)
        self.assertEqual(resources["memory"]["ceiling_bytes_per_command"], 2 * 1024**3)
        self.assertIsNone(resources["cpu"]["guarantee_cores"])

    def test_speed_report_covers_every_trial(self):
        speed = self.result["speed"]
        self.assertEqual(speed["wall_source"], "run")
        self.assertEqual(speed["trials"]["count"], 13)
        self.assertEqual(speed["parts"]["setup"]["spans"], 13)
        self.assertEqual(speed["parts"]["grading"]["spans"], 13)
        self.assertGreater(speed["calls"], 0)

    def test_scripted_runs_carry_no_usage_so_cost_stays_unknown(self):
        total = self.result["cost"]["experiment_total"]["cost_usd"]
        self.assertIsNone(total["value"])
        self.assertIn("no_events", total["flags"])

    def test_parity_is_valid(self):
        self.assertTrue(self.result["parity"]["valid"])


class HoldoutTest(unittest.TestCase):
    def test_holdout_with_no_cases_exits_zero_with_an_empty_valid_result(self):
        code, result, output = run_cli("run", "--split", "holdout", "--suite", "all")
        self.assertEqual(code, 0, output)
        self.assertEqual(result["cases"], [])
        schema.validate(result)


class LocalCiParityTest(unittest.TestCase):
    def test_ci_environment_gives_the_same_schema_and_exit_code(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic_case(root, "good")
            synthetic_case(root, "bad", reference="no")
            local = run_synthetic(root, "--split", "development", "--suite", "regression")
            ci = run_synthetic(root, "--split", "development", "--suite", "regression",
                               env={"CI": "true", "GITHUB_ACTIONS": "true"})
        self.assertEqual(local[0], 1, local[2])
        self.assertEqual(ci[0], local[0], ci[2])
        for result in (local[1], ci[1]):
            schema.validate(result)
        self.assertEqual(json.dumps(sorted(local[1])), json.dumps(sorted(ci[1])))
        self.assertEqual([c["status"] for c in local[1]["cases"]], [c["status"] for c in ci[1]["cases"]])


if __name__ == "__main__":
    unittest.main()
