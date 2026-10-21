from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, with_interpreter

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_HERMES_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]
if argv[1:3] == ["--board", state.get("board")]:
    argv = argv[:1] + argv[3:]
elif state.get("board"):
    sys.exit("not on board " + state["board"] + ": " + repr(argv))
if argv[:2] == ["kanban", "show"]:
    print(json.dumps({"task": state["task"]}))
elif argv[:2] == ["kanban", "archive"]:
    if state.get("archive_fails"):
        sys.exit("hermes: cannot archive")
    if state["task"]["status"] == "archived":
        sys.exit("hermes: task is already archived")
    state["task"]["status"] = "archived"
    json.dump(state, open(path, "w"))
else:
    sys.exit("unexpected " + repr(argv))
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class ArchiveCardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "-b", "main")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "init")
        git(self.repo, "branch", "feature")
        self.old = self.repo / ".worktrees" / "t_old"
        git(self.repo, "worktree", "add", "--quiet", str(self.old), "feature")
        self.hermes = self.dir / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)
        self.state_path = self.dir / "state.json"
        self.state = {"task": {"id": "t_old", "status": "blocked", "workspace_kind": "worktree",
                               "workspace_path": str(self.old)}}
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def archive(self, *extra: str) -> tuple[int, dict]:
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path)}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "archive_card.py"), "t_old", *extra],
                              capture_output=True, text=True, env=env)
        return proc.returncode, json.loads(proc.stdout)

    def start_new_card(self) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "worktree", "add", str(self.repo / ".worktrees" / "t_new"), "feature"],
                              cwd=self.repo, capture_output=True, text=True)

    def test_a_new_card_can_check_out_the_branch_a_preserved_worktree_held(self) -> None:
        (self.old / "unsaved.txt").write_text("work in progress")
        git(self.old, "add", "unsaved.txt")
        git(self.old, "commit", "--quiet", "-m", "unpushed work")
        (self.old / "dirty.txt").write_text("uncommitted")
        before = self.start_new_card()
        self.assertNotEqual(before.returncode, 0)
        self.assertIn("already used by worktree", before.stderr)

        code, out = self.archive()

        self.assertEqual((code, out), (0, {"archived": "t_old", "freed_worktree": str(self.old)}))
        after = self.start_new_card()
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertTrue((self.repo / ".worktrees" / "t_new" / "unsaved.txt").exists())
        self.assertEqual((self.old / "dirty.txt").read_text(), "uncommitted")
        self.assertEqual(git(self.old, "branch", "--show-current"), "")

    def test_running_it_again_changes_nothing(self) -> None:
        self.archive()
        code, out = self.archive()
        self.assertEqual((code, out), (0, {"archived": "t_old", "freed_worktree": None}))

    def test_a_card_whose_worktree_hermes_already_removed_archives_cleanly(self) -> None:
        git(self.repo, "worktree", "remove", str(self.old))
        code, out = self.archive()
        self.assertEqual((code, out), (0, {"archived": "t_old", "freed_worktree": None}))
        self.assertEqual(self.start_new_card().returncode, 0)

    def test_a_card_without_a_worktree_is_only_archived(self) -> None:
        self.state["task"].update(workspace_kind="scratch", workspace_path=None)
        self.save()
        code, out = self.archive()
        self.assertEqual((code, out), (0, {"archived": "t_old", "freed_worktree": None}))
        self.assertEqual(git(self.old, "branch", "--show-current"), "feature")

    def test_a_card_on_another_board_is_read_and_archived_there(self) -> None:
        self.state["board"] = "triage"
        self.save()
        code, out = self.archive("--board", "triage")
        self.assertEqual((code, out), (0, {"archived": "t_old", "freed_worktree": str(self.old)}))
        self.assertEqual(json.loads(self.state_path.read_text())["task"]["status"], "archived")

    def test_a_failed_archive_reports_the_error_and_keeps_the_worktree_on_its_branch(self) -> None:
        self.state["archive_fails"] = True
        self.save()
        code, out = self.archive()
        self.assertEqual(code, 1)
        self.assertIn("cannot archive", out["error"])
        self.assertEqual(git(self.old, "branch", "--show-current"), "feature")


if __name__ == "__main__":
    unittest.main()
