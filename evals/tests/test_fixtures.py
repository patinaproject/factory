import re
import tempfile
import unittest
from pathlib import Path

from patina_evals import loader
from patina_evals.manifest import Manifest, ManifestError, parse
from tests.support import EVALS, synthetic_case, write_json

PUBLIC_SOURCES = (
    "https://github.com/patinaproject/factory/issues/371",
    "https://github.com/patinaproject/factory/issues/496",
    "https://github.com/patinaproject/factory/pull/518",
    "https://github.com/patinaproject/factory/issues/550",
    "https://github.com/patinaproject/factory/issues/579",
    "https://github.com/patinaproject/factory/blob/main/docs/agents/issue-tracker.md#priority-rubric",
    "https://github.com/patinaproject/skills/issues/692",
)
FORBIDDEN_TEXT = {
    "private Linear identifier": re.compile(r"\bPAT-\d+"),
    "Anthropic or OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "Linear key": re.compile(r"\blin_(api|oauth)_[A-Za-z0-9]{20,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "AWS key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "email address": re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net|invalid)\b)[A-Za-z0-9.-]+\.[a-z]{2,}"),
}
SCANNED = ("fixtures", "graders", "parity", "patina_evals", "tests", "qualifying_paths.json", "README.md")
SCAN_EXEMPT = {"tests/test_fixtures.py"}


def committed_manifests() -> list[Manifest]:
    return loader.load("manifest-dir", EVALS / "fixtures", EVALS / "graders")


class SmokeManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifests = {m.id: m for m in committed_manifests()}

    def test_two_paired_fixtures_in_each_area(self):
        areas = {}
        for m in self.manifests.values():
            areas.setdefault(m.area, set()).add(m.id)
        self.assertEqual(areas, {
            "triage-priority": {"triage-priority-allowed", "triage-priority-human-set"},
            "token-report-accounting": {"token-spend-supported-usage", "token-spend-unknown-cost"},
        })

    def test_siblings_share_a_group_so_a_holdout_split_keeps_them_together(self):
        groups = {m.area: set() for m in self.manifests.values()}
        for m in self.manifests.values():
            groups[m.area].add(m.group)
        self.assertTrue(all(len(g) == 1 for g in groups.values()), groups)

    def test_every_manifest_is_complete_and_cites_public_sources(self):
        for m in self.manifests.values():
            with self.subTest(case=m.id):
                self.assertRegex(m.version, r"^\d+\.\d+\.\d+$")
                self.assertRegex(m.task_version, r"^\d+\.\d+\.\d+$")
                self.assertTrue(m.task_text.strip())
                self.assertTrue(m.pre_fix_state["description"])
                self.assertTrue(m.dependencies["tools"])
                self.assertIn("human", m.responses)
                self.assertTrue(m.acceptance_checks and m.forbidden_outcomes)
                self.assertTrue(m.provenance["sources"])
                self.assertTrue(set(m.provenance["sources"]) <= set(PUBLIC_SOURCES), m.provenance["sources"])
                self.assertEqual((m.split, m.suite, m.required), ("development", "regression", True))
                self.assertTrue((m.grader_dir / "grader.py").is_file())
                for s in m.solutions:
                    folder = m.grader_dir / "solutions" / s.name
                    self.assertTrue((folder / "files").is_dir() or (folder / "stream.json").is_file(), folder)

    def test_each_case_has_a_reference_and_at_least_one_near_miss(self):
        for m in self.manifests.values():
            expects = sorted(s.expect for s in m.solutions)
            self.assertEqual(expects[-1], "pass")
            self.assertIn("fail", expects)

    def test_authority_boundary_case_forbids_overwriting_a_persons_priority(self):
        self.assertIn("overwrote_human_priority", self.manifests["triage-priority-human-set"].forbidden_outcomes)
        self.assertIn("unknown_cost_reported_as_zero", self.manifests["token-spend-unknown-cost"].forbidden_outcomes)

    def test_human_set_case_scripts_a_persons_reply(self):
        self.assertEqual(self.manifests["triage-priority-human-set"].responses["human"]["acknowledge"]["issue"], "ISSUE-2")


class ScanTest(unittest.TestCase):
    def test_no_credentials_customer_data_or_private_identifiers(self):
        files = []
        for name in SCANNED:
            path = EVALS / name
            files += [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        self.assertGreater(len(files), 40)
        for path in files:
            relative = str(path.relative_to(EVALS))
            if relative in SCAN_EXEMPT:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in FORBIDDEN_TEXT.items():
                with self.subTest(file=relative, pattern=label):
                    self.assertIsNone(pattern.search(text))


class LoaderTest(unittest.TestCase):
    def test_external_case_folder_maps_to_the_common_manifest(self):
        def reviewer_cases(cases: Path, graders: Path) -> list[Manifest]:
            out = []
            for path in sorted(cases.glob("case-*/case.json")):
                import json
                raw = json.loads(path.read_text())
                out.append(parse({
                    "manifest_version": 1, "id": raw["name"], "version": raw["rev"], "area": "external",
                    "task": {"file": "prompt.txt", "version": raw["rev"]}, "group": raw["family"],
                    "split": "holdout" if raw["held_out"] else "development",
                    "suite": "capability" if raw["stretch"] else "regression",
                    "provenance": {"sources": ["external"]}, "pre_fix_state": {"repo": "before"},
                    "dependencies": {"python": ">=3.14"}, "acceptance_checks": raw["expect"],
                    "solutions": {"reference": {"expect": "pass"}},
                }, path.parent, graders / raw["name"]))
            return out

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for i, held in ((1, False), (2, True)):
                case = root / f"case-{i}"
                (case / "before").mkdir(parents=True)
                (case / "prompt.txt").write_text("do it\n")
                write_json(case / "case.json", {"name": f"ext-{i}", "rev": "2.0.0", "family": "fam", "held_out": held,
                                                "stretch": i == 2, "expect": ["works"]})
            loader.register("synthetic-reviewer", reviewer_cases)
            try:
                manifests = loader.load("synthetic-reviewer", root, root / "graders")
            finally:
                del loader.LOADERS["synthetic-reviewer"]
        self.assertEqual([(m.id, m.split, m.suite, m.required) for m in manifests],
                         [("ext-1", "development", "regression", True), ("ext-2", "holdout", "capability", False)])
        self.assertEqual({m.group for m in manifests}, {"fam"})

    def test_unknown_loader_is_refused(self):
        with self.assertRaises(ValueError):
            loader.load("no-such-format", EVALS, EVALS)

    def test_near_miss_without_targets_is_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            synthetic_case(root, "x", extra_solutions={"oops": {"expect": "fail"}})
            with self.assertRaises(ManifestError):
                loader.load("manifest-dir", root / "cases", root / "graders")


if __name__ == "__main__":
    unittest.main()
