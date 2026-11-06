import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from patina_evals.path_filter import glob_regex, qualifying

EVALS = Path(__file__).resolve().parents[1]
REPO = EVALS.parent
SCRIPT = EVALS / "patina_evals" / "path_filter.py"

QUALIFY = [
    "AGENTS.md",
    "CLAUDE.md",
    "docs/nested/CLAUDE.md",
    "SOUL.md",
    "profiles/claude-worker/SOUL.md",
    "profiles/claude-worker/config.yaml",
    "templates/triage-linear.md",
    "templates/hermes-memory.txt",
    ".agents/skills/how/SKILL.md",
    ".claude/skills/patina-mode/playbooks/feature.md",
    "skills/setup-factory/SKILL.md",
    ".claude/agents/patina-agent.md",
    ".github/workflows/evals.yml",
    ".github/workflows/skills-sync.yml",
    "skills-lock.json",
    "evals/patina_evals/ledger.py",
    "evals/requirements.txt",
    "evals/requirements.in",
    "evals/qualifying_paths.json",
]
DO_NOT_QUALIFY = [
    "plugins/pr-ready-gate/gate.py",
    "plugins/pr-ready-gate/tests/test_gate.py",
    "templates/render.py",
    "templates/README.md",
    "profiles/claude-worker/scripts/token-spend",
    "README.md",
    ".github/workflows/test.yml",
    "evals/fixtures/triage-priority/manifest.json",
    "evals/fixtures/triage-priority/repo/AGENTS.md",
    "evals/graders/triage-priority/reference.patch",
    "evals/tests/test_ledger.py",
    "evals/README.md",
]


def run_cli(paths, env_extra=None):
    with tempfile.TemporaryDirectory() as tmp:
        paths_file = Path(tmp) / "paths.txt"
        paths_file.write_text("\n".join(paths) + "\n", encoding="utf-8")
        output = Path(tmp) / "github_output"
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        env["GITHUB_OUTPUT"] = str(output)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--paths-file", str(paths_file)],
            capture_output=True, text=True, cwd=REPO, env=env,
        )
        return result, output.read_text(encoding="utf-8") if output.exists() else None


class GlobTest(unittest.TestCase):
    def test_double_star_slash_matches_zero_or_more_directories(self):
        pattern = glob_regex("**/CLAUDE.md")
        self.assertTrue(pattern.match("CLAUDE.md"))
        self.assertTrue(pattern.match("a/b/CLAUDE.md"))
        self.assertFalse(pattern.match("a/NOTCLAUDE.md"))

    def test_single_star_stays_within_a_segment(self):
        pattern = glob_regex("templates/triage-*.md")
        self.assertTrue(pattern.match("templates/triage-inbox.md"))
        self.assertFalse(pattern.match("templates/triage-x/y.md"))

    def test_trailing_double_star_matches_everything_below(self):
        pattern = glob_regex("skills/**")
        self.assertTrue(pattern.match("skills/a/b/c.md"))
        self.assertFalse(pattern.match("other/skills/a.md"))

    def test_dots_are_literal(self):
        self.assertFalse(glob_regex("skills-lock.json").match("skills-lockxjson"))


class QualifyingPathsTest(unittest.TestCase):
    def test_each_qualifying_path_qualifies(self):
        for path in QUALIFY:
            with self.subTest(path=path):
                self.assertEqual(qualifying([path]), [path])

    def test_each_unrelated_path_does_not_qualify(self):
        for path in DO_NOT_QUALIFY:
            with self.subTest(path=path):
                self.assertEqual(qualifying([path]), [])

    def test_mixed_change_returns_only_the_matches_in_order(self):
        paths = ["plugins/x/a.py", "AGENTS.md", "evals/fixtures/a/b.md", "skills-lock.json"]
        self.assertEqual(qualifying(paths), ["AGENTS.md", "skills-lock.json"])

    def test_table_is_valid_json_with_both_lists(self):
        table = json.loads((EVALS / "qualifying_paths.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(table), ["exclude", "include"])


class CliTest(unittest.TestCase):
    def test_qualifying_change_json_and_github_output_agree(self):
        result, output = run_cli(["plugins/x/a.py", "AGENTS.md"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"qualifies": True, "matched": ["AGENTS.md"]})
        self.assertEqual(output, "qualifies=true\n")

    def test_unrelated_change_exits_zero_and_reports_false(self):
        result, output = run_cli(["plugins/x/a.py", "evals/fixtures/a/manifest.json"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"qualifies": False, "matched": []})
        self.assertEqual(output, "qualifies=false\n")

    def test_empty_change_does_not_qualify(self):
        result, output = run_cli([])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["qualifies"], False)
        self.assertEqual(output, "qualifies=false\n")

    def test_no_github_output_variable_still_prints_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths_file = Path(tmp) / "paths.txt"
            paths_file.write_text("CLAUDE.md\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--paths-file", str(paths_file)],
                capture_output=True, text=True, cwd=REPO, env=env,
            )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["qualifies"])

    def test_usage_error_exits_nonzero(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(result.returncode, 2)

    def test_git_range_lists_changed_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                    cwd=tmp, check=True, capture_output=True,
                )

            git("init", "-q", "-b", "main")
            Path(tmp, "a.py").write_text("x\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "-m", "base")
            Path(tmp, "AGENTS.md").write_text("rules\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "-m", "head")
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--base", "HEAD~1", "--head", "HEAD"],
                capture_output=True, text=True, cwd=tmp, env=env,
            )
        self.assertEqual(json.loads(result.stdout), {"qualifies": True, "matched": ["AGENTS.md"]})


if __name__ == "__main__":
    unittest.main()
