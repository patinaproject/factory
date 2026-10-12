from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
REPO = SCRIPTS.parents[2]
BOT_LOGIN = "factory-app[bot]"
BOT_AUTHOR = (BOT_LOGIN, f"123+{BOT_LOGIN}@users.noreply.github.com")

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
# The real claude gets no test variables, so the fake reads its options from the file beside it.
options = json.load(open(__file__ + ".json"))
argv = sys.argv[1:]
settings = None
if "--settings" in argv:
    path = argv[argv.index("--settings") + 1]
    settings = {"content": json.load(open(path)), "mode": oct(os.stat(path).st_mode & 0o777)}
with open(options["log"], "w") as log:
    json.dump({"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ), "settings": settings}, log)
leftover = None
if options.get("leftover_pid_file"):
    import subprocess
    leftover = subprocess.Popen(["sleep", "600"], start_new_session=bool(options.get("leftover_detached")))
    with open(options["leftover_pid_file"], "w") as pid_file:
        pid_file.write(str(leftover.pid))
print(json.dumps({
    "type": "result",
    "subtype": options.get("subtype", "success"),
    "is_error": False,
    "result": "Opened the pull request.",
    "session_id": "ignored",
    "num_turns": 7,
}), flush=True)
if leftover and options.get("waits_for_leftover"):
    leftover.wait()
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class GitFixture(unittest.TestCase):
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

    def push_upstream_commit(
        self, name: str, ref: str = "refs/heads/main", author: tuple[str, str] | None = None, base: str | None = None
    ) -> str:
        """Push one commit to ref, on top of base when given, else on the default branch."""
        other = self.tmp / f"other-{name}"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        if base:
            git(other, "fetch", "-q", "origin", base)
            git(other, "reset", "-q", "--hard", "FETCH_HEAD")
        (other / f"{name}.txt").write_text(name)
        git(other, "add", f"{name}.txt")
        if author:
            git(other, "-c", "user.name=GitHub", "-c", "user.email=noreply@github.com",
                "commit", "-q", "-m", name, f"--author={author[0]} <{author[1]}>")
        else:
            git(other, "commit", "-q", "-m", name)
        git(other, "push", "-q", "origin", f"+HEAD:{ref}")
        return git(other, "rev-parse", "HEAD")

    def write_settings(
        self, repositories: list, claude_session: dict | None = None, login: str | None = BOT_LOGIN
    ) -> Path:
        path = self.tmp / "settings.json"
        settings = {"repositories": repositories, "claude_session": claude_session or {}}
        if login:
            settings["github"] = {"login": login}
        path.write_text(json.dumps(settings))
        return path

    def make_fake_claude(self, log: Path) -> Path:
        path = self.tmp / "fake-claude"
        path.write_text(FAKE_CLAUDE)
        os.chmod(path, 0o755)
        self.fake_claude_options = {"log": str(log)}
        self.configure_fake_claude()
        return path

    def configure_fake_claude(self, **options) -> None:
        self.fake_claude_options.update(options)
        (self.tmp / "fake-claude.json").write_text(json.dumps(self.fake_claude_options))

    def install_linear_plugin(
        self, home: Path, cached_token: str | None = "cached-token", *, accepted: tuple = ("cached-token",)
    ) -> None:
        """The real plugin client under a worker home, against a fake Linear that accepts the accepted tokens."""
        plugin = home / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "linear.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "tests" / "staged_linear.py")
        (home / "linear-fake.json").write_text(json.dumps({"accepted": list(accepted)}))
        if cached_token:
            cache = home / "plugin-data" / "linear-agent-session" / "token.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"client_id": "id1", "access_token": cached_token, "expires_at": 4102444800}))

    @staticmethod
    def linear_requests(home: Path) -> list:
        log = home / "linear-fake.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
