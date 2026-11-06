import shutil
import tempfile
import unittest
from pathlib import Path

from patina_evals import adapter, parity

REPO = adapter.REPO_ROOT
COPIED = ("profiles/claude-worker", "plugins/blocker-order", ".claude/agents", "AGENTS.md", "CLAUDE.md", "skills-lock.json")


class RealConstructionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = parity.check()

    def test_adapter_matches_the_worker_up_to_recorded_differences(self):
        self.assertEqual(self.report["problems"], [])
        self.assertTrue(self.report["valid"])

    def test_covers_every_required_dimension(self):
        self.assertEqual(
            [d["dimension"] for d in self.report["dimensions"]],
            ["worker_prompt", "project_instructions", "tool_search", "agents_and_hooks", "skill_and_dependency_pins",
             "role_models", "effort", "resume", "environment"],
        )

    def test_each_dimension_names_its_defining_worker_profile_file(self):
        for row in self.report["dimensions"]:
            with self.subTest(dimension=row["dimension"]):
                self.assertTrue([s for s in row["source"] if s.startswith("profiles/claude-worker/")], row)
                for source in row["source"]:
                    self.assertTrue((REPO / source).exists(), source)

    def test_every_recorded_difference_is_used(self):
        self.assertEqual(len(self.report["intentional_differences"]), len(parity.load_differences()))


class ResumeContractTest(unittest.TestCase):
    def test_worker_session_planner_decisions(self):
        resume = adapter.construction("worker")["resume"]["value"]
        self.assertEqual(resume["resume_prompt"], "Continue.")
        self.assertEqual(resume["no_transcript"], {"resumed": False, "reason": "new"})
        self.assertEqual(resume["warm_transcript"], {"resumed": True, "reason": "warm", "same_session": True})
        self.assertEqual(resume["idle_unreadable_transcript"], {"resumed": False, "reason": "fresh-unreadable"})

    def test_role_models_compare_the_committed_placeholder_structure(self):
        roles = adapter.construction("worker")["role_models"]["value"]
        self.assertEqual(roles["committed"], {"model": "<placeholder>", "tier_models": []})


class InvalidComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = adapter.construction("worker")
        cls.trial = adapter.construction("trial")

    def test_unlisted_difference_is_incompatible(self):
        report = parity.compare(self.worker, self.trial, [])
        self.assertFalse(report["valid"])
        self.assertEqual({p["kind"] for p in report["problems"]}, {"incompatible"})

    def test_difference_with_another_value_is_incompatible(self):
        differences = [d | {"trial": "http://elsewhere.invalid"} if d["path"] == "environment.ANTHROPIC_BASE_URL" else d
                       for d in parity.load_differences()]
        report = parity.compare(self.worker, self.trial, differences)
        self.assertFalse(report["valid"])
        self.assertIn("environment.ANTHROPIC_BASE_URL", [p.get("path") for p in report["problems"]])

    def test_stale_recorded_difference_invalidates(self):
        differences = [*parity.load_differences(), {"path": "effort.worker_flag", "worker": False, "trial": True, "reason": "x"}]
        report = parity.compare(self.worker, self.trial, differences)
        self.assertEqual([p["kind"] for p in report["problems"]], ["stale_difference"])

    def test_unknown_dimension_invalidates(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for part in COPIED:
                source = REPO / part
                (shutil.copytree if source.is_dir() else shutil.copy2)(source, root / part)
            (root / "profiles/claude-worker/config.yaml").unlink()
            report = parity.check(root)
        self.assertFalse(report["valid"])
        unknown = {p["dimension"] for p in report["problems"] if p["kind"] == "unknown"}
        self.assertIn("role_models", unknown)
        self.assertIn("tool_search", unknown)

    def test_missing_dimension_is_unknown(self):
        trial = {k: v for k, v in self.trial.items() if k != "effort"}
        report = parity.compare(self.worker, trial, parity.load_differences())
        self.assertEqual([(p["dimension"], p["kind"]) for p in report["problems"]], [("effort", "unknown")])


if __name__ == "__main__":
    unittest.main()
