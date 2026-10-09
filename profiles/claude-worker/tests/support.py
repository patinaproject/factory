from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
settings = None
if "--settings" in argv:
    path = argv[argv.index("--settings") + 1]
    settings = {"content": json.load(open(path)), "mode": oct(os.stat(path).st_mode & 0o777)}
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as log:
    json.dump({"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ), "settings": settings}, log)
print(json.dumps({
    "type": "result",
    "subtype": os.environ.get("FAKE_CLAUDE_SUBTYPE", "success"),
    "is_error": False,
    "result": "Opened the pull request.",
    "session_id": "ignored",
    "num_turns": 7,
}))
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class GitFixture(unittest.TestCase):
    """A bare origin, a main checkout cloned from it, and helpers to add worktrees."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        seed = self.tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("seed\n")
        git(seed, "add", "README.md")
        git(seed, "commit", "-q", "-m", "seed")
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "clone", "-q", "--bare", str(seed), str(self.origin))
        self.main = self.tmp / "main"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.main))

    def add_worktree(self, branch: str) -> Path:
        path = self.main / ".worktrees" / branch
        git(self.main, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def push_upstream_commit(self, name: str) -> str:
        other = self.tmp / f"other-{name}"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        (other / f"{name}.txt").write_text(name)
        git(other, "add", f"{name}.txt")
        git(other, "commit", "-q", "-m", name)
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def write_settings(self, repositories: list, claude_session: dict | None = None) -> Path:
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"repositories": repositories, "claude_session": claude_session or {}}))
        return path

    def make_fake_claude(self) -> Path:
        path = self.tmp / "fake-claude"
        path.write_text(FAKE_CLAUDE)
        os.chmod(path, 0o755)
        return path
