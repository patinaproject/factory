from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

from tests.support import SCRIPTS, GitFixture, FakeGithub, with_interpreter

REPO_NAME = "example-org/app"
GITHUB_PULL_9 = f"https://github.com/{REPO_NAME}/pull/9"
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

    def hold(self, *urls: str) -> None:
        (self.home / "linear-fake.json").write_text(json.dumps({"accepted": ["cached-token"], "session_urls": list(urls)}))

    def test_check_prints_the_session_that_lacks_its_pull_request_and_changes_nothing(self) -> None:
        proc = self.run_script("--check")

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (1, f"t_lacks s-lacks lacks {GITHUB_PULL_9} {REVIEW_PULL_9}\n", ""))
        self.assertEqual(self.session_updates(), [])

    def test_check_reports_a_session_that_holds_only_the_review_url(self) -> None:
        self.hold(REVIEW_PULL_9)

        proc = self.run_script("--check")

        self.assertEqual((proc.returncode, proc.stdout), (1, f"t_lacks s-lacks lacks {GITHUB_PULL_9}\n"))

    def test_check_reports_a_session_that_holds_only_the_github_url(self) -> None:
        self.hold(GITHUB_PULL_9)

        proc = self.run_script("--check")

        self.assertEqual((proc.returncode, proc.stdout), (1, f"t_lacks s-lacks lacks {REVIEW_PULL_9}\n"))

    def test_check_is_clean_when_the_session_holds_every_pull_request(self) -> None:
        self.hold(GITHUB_PULL_9, REVIEW_PULL_9)

        proc = self.run_script("--check")

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_sync_adds_the_missing_pull_request_to_each_session(self) -> None:
        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(proc.stdout, f"t_lacks s-lacks linked {GITHUB_PULL_9} {REVIEW_PULL_9}\n")
        self.assertEqual(self.session_updates(), [
            {"id": "s-lacks", "input": {"addedExternalUrls": [
                {"label": "Pull request #9", "url": GITHUB_PULL_9},
                {"label": "Pull request #9", "url": REVIEW_PULL_9},
            ]}},
        ])

    CARD_URL = "https://kanban.test/t_lacks"

    def hold_sessions(self, older: list, current: list, **state) -> None:
        sessions = {
            "s-old": {"createdAt": "2026-10-27T09:00:00.000Z", "urls": older},
            "s-lacks": {"createdAt": "2026-10-27T11:00:00.000Z", "urls": current},
        }
        (self.home / "linear-fake.json").write_text(json.dumps({"accepted": ["cached-token"], "sessions": sessions, **state}))

    def held(self) -> dict:
        return {k: v["urls"] for k, v in json.loads((self.home / "linear-fake.json").read_text())["sessions"].items()}

    def test_check_prints_the_older_session_that_still_holds_the_current_sessions_pull_request(self) -> None:
        self.hold_sessions([GITHUB_PULL_9, REVIEW_PULL_9, self.CARD_URL], [GITHUB_PULL_9, REVIEW_PULL_9])

        proc = self.run_script("--check")

        self.assertEqual(
            (proc.returncode, proc.stdout, proc.stderr),
            (1, f"t_lacks s-lacks misplaced {GITHUB_PULL_9} {REVIEW_PULL_9} in s-old\n", ""),
        )
        self.assertEqual(self.session_updates(), [])

    def test_check_prints_both_lines_for_a_card_that_lacks_one_link_and_has_another_misplaced(self) -> None:
        self.hold_sessions([REVIEW_PULL_9], [REVIEW_PULL_9])

        proc = self.run_script("--check")

        self.assertEqual(
            (proc.returncode, proc.stdout),
            (1, f"t_lacks s-lacks lacks {GITHUB_PULL_9}\nt_lacks s-lacks misplaced {REVIEW_PULL_9} in s-old\n"),
        )

    def test_sync_removes_the_pull_request_from_the_older_session_and_a_second_run_prints_nothing(self) -> None:
        self.hold_sessions([GITHUB_PULL_9, REVIEW_PULL_9, self.CARD_URL], [GITHUB_PULL_9, REVIEW_PULL_9])

        first = self.run_script()
        second = self.run_script()

        self.assertEqual(
            (first.returncode, first.stdout, first.stderr),
            (0, f"t_lacks s-lacks unlinked {GITHUB_PULL_9} {REVIEW_PULL_9} from s-old\n", ""),
        )
        self.assertEqual(self.held(), {"s-old": [self.CARD_URL], "s-lacks": [GITHUB_PULL_9, REVIEW_PULL_9]})
        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, "", ""))
        self.assertEqual(len(self.session_updates()), 1)

    def test_sync_adds_what_the_current_session_lacks_then_removes_it_from_the_older_one(self) -> None:
        self.hold_sessions([REVIEW_PULL_9], [REVIEW_PULL_9])

        proc = self.run_script()

        self.assertEqual(
            (proc.returncode, proc.stdout),
            (0, f"t_lacks s-lacks linked {GITHUB_PULL_9}\nt_lacks s-lacks unlinked {REVIEW_PULL_9} from s-old\n"),
        )
        self.assertEqual(self.held(), {"s-old": [], "s-lacks": [REVIEW_PULL_9, GITHUB_PULL_9]})

    def test_a_failed_removal_is_reported_for_the_card_and_fails_the_run(self) -> None:
        self.hold_sessions([GITHUB_PULL_9, REVIEW_PULL_9], [GITHUB_PULL_9, REVIEW_PULL_9], session_remove_error="boom")

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertIn("t_lacks: unlinking the older agent sessions failed:", proc.stderr)
