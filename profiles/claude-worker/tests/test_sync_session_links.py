from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

from tests.support import SCRIPTS, GitFixture, FakeGithub, with_interpreter

REPO_NAME = "example-org/app"
REVIEW_PULL_9 = f"https://linear.review/{REPO_NAME}/pull/9"
FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
print(json.dumps([{"slug": "default", "db_path": os.environ["FAKE_DB"]}]))
"""
CARDS = [
    ("t_lacks", "linear_agent_session_id: s-lacks\nrepo: example-org/app", "abc-123-fix", "local-only"),
    ("t_no_session", "repo: example-org/app", "abc-123-fix", "local-only"),
    ("t_no_branch", "linear_agent_session_id: s-none\nrepo: example-org/app", None, "local-only"),
]


class SyncSessionLinksTest(GitFixture):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.install_linear_plugin(self.home)
        self.github = FakeGithub(self.tmp, REPO_NAME)
        self.github.add_pull(9, "abc-123-fix", state="closed", merged=True)
        self.db = self.tmp / "kanban.db"
        with closing(sqlite3.connect(self.db)) as board:
            board.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, branch_name TEXT, status TEXT, "
                          "block_kind TEXT, completion_contract TEXT)")
            board.executemany("INSERT INTO tasks VALUES (?, 'title', ?, ?, 'ready', NULL, ?)", CARDS)
            board.commit()
        hermes = self.tmp / "hermes"
        hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(hermes, 0o755)
        self.env = {
            **os.environ, "HERMES_HOME": str(self.home), "HERMES_BIN": str(hermes), "FAKE_DB": str(self.db),
            "LINEAR_CLIENT_ID": "id1", "LINEAR_CLIENT_SECRET": "secret", **self.github.env(),
        }

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "sync-session-links"), *args], capture_output=True, text=True, env=self.env,
        )

    def session_updates(self) -> list:
        log = self.home / "linear-fake.log"
        entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [e["session_update"] for e in entries if "session_update" in e]

    def hold(self) -> None:
        (self.home / "linear-fake.json").write_text(json.dumps({"accepted": ["cached-token"], "session_urls": [REVIEW_PULL_9]}))

    def test_check_prints_the_session_that_lacks_its_pull_request_and_changes_nothing(self) -> None:
        proc = self.run_script("--check")

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (1, f"t_lacks s-lacks lacks {REVIEW_PULL_9}\n", ""))
        self.assertEqual(self.session_updates(), [])

    def test_check_is_clean_when_the_session_holds_every_pull_request(self) -> None:
        self.hold()

        proc = self.run_script("--check")

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_sync_adds_the_missing_pull_request_to_each_session(self) -> None:
        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(proc.stdout, f"t_lacks s-lacks linked {REVIEW_PULL_9}\n")
        self.assertEqual(self.session_updates(), [
            {"id": "s-lacks", "input": {"addedExternalUrls": [{"label": "Pull request #9", "url": REVIEW_PULL_9}]}},
        ])
