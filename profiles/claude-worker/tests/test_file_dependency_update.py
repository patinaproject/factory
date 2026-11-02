from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from tests.support import SCRIPTS, GitFixture, with_interpreter

sys.path.insert(0, str(SCRIPTS))
import dependency_update  # noqa: E402

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_HERMES_STATE"]))
if sys.argv[1:3] == ["config", "get"]:
    print(json.dumps(state["settings"]))
else:
    sys.exit("hermes: refused " + repr(sys.argv[1:]))
"""

SETTINGS = {
    "dependency_updates": {
        "repository": "example-org/example-app",
        "team_key": "ABC",
        "theme_label": "Security",
        "type_label": "Tools",
    }
}


class FileDependencyUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        GitFixture.install_linear_plugin(GitFixture, self.home)
        self.hermes = self.tmp / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)
        self.state_path = self.tmp / "hermes-state.json"

    def linear(self, **fields) -> None:
        path = self.home / "linear-fake.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))

    def run_script(self, settings: object = SETTINGS) -> subprocess.CompletedProcess:
        self.state_path.write_text(json.dumps({"settings": settings}))
        env = {
            **os.environ, "HERMES_HOME": str(self.home), "HERMES_BIN": str(self.hermes),
            "FAKE_HERMES_STATE": str(self.state_path), "LINEAR_CLIENT_ID": "id1", "LINEAR_CLIENT_SECRET": "secret",
        }
        return subprocess.run([sys.executable, str(SCRIPTS / "file-dependency-update")], env=env, capture_output=True, text=True)

    def requests(self, key: str) -> list:
        return [entry[key] for entry in GitFixture.linear_requests(self.home) if key in entry]

    def test_files_one_triage_issue_with_the_theme_label_delegated_to_the_app(self) -> None:
        self.linear(team_labels=["Security", "Tools"])
        before = datetime.now(timezone.utc).date()

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, "filed ABC-900 https://linear.app/example/issue/ABC-900\n"))
        self.assertEqual(self.requests("open_titled"), [{"filter": {
            "team": {"key": {"eq": "ABC"}},
            "creator": {"isMe": {"eq": True}},
            "title": {"startsWith": "Update npm dependencies within declared ranges (week of "},
            "state": {"type": {"nin": ["completed", "canceled", "duplicate"]}},
        }}])
        created = self.requests("issue_create")
        self.assertEqual(len(created), 1)
        issue = created[0]["input"]
        self.assertEqual(
            {key: value for key, value in issue.items() if key not in ("title", "description")},
            {"teamId": "team-ABC", "stateId": "state-triage", "labelIds": ["label-Security"], "delegateId": "app-user"},
        )
        week = date.fromisoformat(issue["title"].removeprefix(dependency_update.TITLE_PREFIX).removesuffix(")"))
        self.assertEqual(week.weekday(), 0)
        self.assertIn((before - week).days, range(0, 8))
        self.assertIn("Run `pnpm update` within the declared ranges across the workspace. Do not use `--latest`.", issue["description"])
        self.assertIn("`example-org/example-app`", issue["description"])
        self.assertIn("Use the `Tools` type and the `chore(deps)` commit type. Close this issue with `Closes ABC-N`", issue["description"])

    def test_an_open_update_issue_files_nothing(self) -> None:
        self.linear(team_labels=["Security"], open_issues=[
            {"identifier": "ABC-41", "title": "Update npm dependencies within declared ranges (week of 2026-10-26)",
             "url": "https://linear.app/example/issue/ABC-41"},
        ])

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, "open ABC-41 https://linear.app/example/issue/ABC-41\n"))
        self.assertEqual(self.requests("issue_create"), [])

    def test_a_failed_find_reports_the_failure_and_files_nothing(self) -> None:
        self.linear(team_labels=["Security"], open_titled_http_status=500)

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "file-dependency-update: Linear failed: Internal Server Error\n")
        self.assertEqual(self.requests("issue_create"), [])

    def test_a_missing_label_reports_the_failure_and_files_nothing(self) -> None:
        self.linear(team_labels=["Tools"])

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertIn("no label named 'Security'", proc.stderr)
        self.assertEqual(self.requests("issue_create"), [])

    def test_invalid_settings_exit_2_before_any_linear_call(self) -> None:
        for settings, message in (
            ({}, "dependency_updates is missing from the factory settings; it must be an object"),
            ({"dependency_updates": {**SETTINGS["dependency_updates"], "team_key": ""}},
             "dependency_updates.team_key '' must be a non-empty string"),
            ({"dependency_updates": {**SETTINGS["dependency_updates"], "repository": "example-app"}},
             "dependency_updates.repository 'example-app' must be <owner>/<repo>"),
        ):
            with self.subTest(message=message):
                proc = self.run_script(settings)

                self.assertEqual((proc.returncode, proc.stdout), (2, ""))
                self.assertEqual(proc.stderr, f"file-dependency-update: {message}\n")
                self.assertEqual(GitFixture.linear_requests(self.home), [])


class WeekMondayTest(unittest.TestCase):
    def test_the_title_names_the_monday_of_the_week(self) -> None:
        for day, title in (
            (date(2026, 11, 2), "Update npm dependencies within declared ranges (week of 2026-11-02)"),
            (date(2026, 11, 8), "Update npm dependencies within declared ranges (week of 2026-11-02)"),
            (date(2026, 11, 9), "Update npm dependencies within declared ranges (week of 2026-11-09)"),
        ):
            with self.subTest(day=day):
                self.assertEqual(dependency_update.title(day), title)


if __name__ == "__main__":
    unittest.main()
