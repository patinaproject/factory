from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS

TASKS = [
    {"id": "t1", "title": "ABC-1: fix", "body": "```text\nbranch: goodyear/abc-1-fix\n```\n", "branch_name": "goodyear/abc-1-fix", "status": "running"},
    {
        "id": "t2",
        "title": "site#7: docs",
        "body": "```text\nrepo: example-org/site\n```\ngithub_issue_url: https://github.com/example-org/site/issues/7\n",
        "branch_name": "7-docs",
        "status": "ready",
    },
    {"id": "t3", "title": "ABC-3: triage", "body": "", "branch_name": None, "status": "triage"},
    {"id": "t4", "title": "ABC-1 again", "body": "", "branch_name": "goodyear/abc-1-fix", "status": "done"},
]
EXPECTED = ["7-docs", "goodyear/abc-1-fix", "https://github.com/example-org/site/issues/7"]

FAKE_HERMES_OK = f"""#!/usr/bin/env python3
import json
print(json.dumps({TASKS!r}))
"""
FAKE_HERMES_FAILS = """#!/usr/bin/env python3
import sys
sys.exit("kanban: board is locked")
"""


class RefreshOwnedRefsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.tasks = self.tmp / "tasks.json"
        self.tasks.write_text(json.dumps(TASKS))
        self.output = self.tmp / "out" / "owned-refs.json"
        self.output.parent.mkdir()
        self.worker = self.tmp / "hermes" / "profiles" / "claude-worker"
        self.worker.mkdir(parents=True)

    def fake_hermes(self, text: str) -> Path:
        path = self.tmp / "bin" / "hermes"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        os.chmod(path, 0o755)
        return path

    def run_script(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        if env is None:
            env = {**os.environ, "HERMES_HOME": str(self.worker)}
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "refresh-owned-refs"), *args], env=env, capture_output=True, text=True
        )

    def test_writes_every_branch_and_github_issue_url_sorted(self) -> None:
        proc = self.run_script("--tasks-json", str(self.tasks), "--output", str(self.output))

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(self.output.read_text(), json.dumps(EXPECTED, indent=2) + "\n")
        self.assertEqual(json.loads(self.output.read_text()), EXPECTED)

    def test_keeps_the_branch_of_a_card_hermes_bound_to_a_pull_request(self) -> None:
        bound = {
            "id": "t5",
            "title": "ABC-5: bound",
            "body": "",
            "branch_name": "goodyear/abc-5-bound",
            "status": "blocked",
            "completion_contract": "https://github.com/example-org/app/pull/12",
        }
        self.tasks.write_text(json.dumps([bound]))

        proc = self.run_script("--tasks-json", str(self.tasks), "--output", str(self.output))

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(self.output.read_text()), ["goodyear/abc-5-bound"])

    def test_rewrites_unchanged_content_with_a_newer_mtime_and_no_leftover(self) -> None:
        self.run_script("--tasks-json", str(self.tasks), "--output", str(self.output))
        first = self.output.read_bytes()
        os.utime(self.output, (1_000_000, 1_000_000))

        proc = self.run_script("--tasks-json", str(self.tasks), "--output", str(self.output))

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.output.read_bytes(), first)
        self.assertGreater(self.output.stat().st_mtime, 1_000_000)
        self.assertEqual(os.listdir(self.output.parent), ["owned-refs.json"])

    def test_default_target_is_the_root_home(self) -> None:
        hermes = self.fake_hermes(FAKE_HERMES_OK)
        env = {**os.environ, "HERMES_HOME": str(self.worker), "HERMES_BIN": str(hermes)}

        proc = self.run_script(env=env)

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(json.loads((self.tmp / "hermes" / "owned-refs.json").read_text()), EXPECTED)

    def test_a_failing_kanban_list_leaves_the_file_untouched(self) -> None:
        self.output.write_text('["stale"]\n')
        hermes = self.fake_hermes(FAKE_HERMES_FAILS)
        env = {**os.environ, "HERMES_HOME": str(self.worker), "HERMES_BIN": str(hermes)}

        proc = self.run_script("--output", str(self.output), env=env)

        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertEqual(proc.stderr, "refresh-owned-refs: hermes kanban list failed: kanban: board is locked\n")
        self.assertEqual(self.output.read_text(), '["stale"]\n')
        self.assertEqual(os.listdir(self.output.parent), ["owned-refs.json"])

    def test_unset_hermes_home_is_refused(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != "HERMES_HOME"}

        proc = self.run_script("--tasks-json", str(self.tasks), env=env)

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertEqual(proc.stderr, "refresh-owned-refs: HERMES_HOME is not set; point it at the claude-worker profile home\n")

    def test_hermes_home_outside_profiles_is_refused(self) -> None:
        proc = self.run_script("--tasks-json", str(self.tasks), env={**os.environ, "HERMES_HOME": str(self.tmp)})

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertEqual(proc.stderr, f"refresh-owned-refs: HERMES_HOME {self.tmp} is not a profile home under <root>/profiles/\n")


if __name__ == "__main__":
    unittest.main()
