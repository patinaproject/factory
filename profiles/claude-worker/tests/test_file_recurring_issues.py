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
import recurring_issues  # noqa: E402

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_HERMES_STATE"]))
if sys.argv[1:3] == ["config", "get"]:
    print(json.dumps(state["settings"]))
else:
    sys.exit("hermes: refused " + repr(sys.argv[1:]))
"""

ENTRY = {
    "repository": "example-org/example-app",
    "team_key": "ABC",
    "title_prefix": "Audit the example app, week of ",
    "cadence": "weekly",
    "labels": ["Security"],
    "description": "Audit `example-org/example-app`.\n\nClose with `Closes ABC-N`.",
}
WEDNESDAY = "2026-11-04T10:30:00Z"
REPOSITORY_LINK = {"issueId": "issue-uuid", "url": "https://github.com/example-org/example-app", "title": "GitHub repository example-org/example-app"}
FILED = "filed 0 ABC-900 https://linear.app/example/issue/ABC-900\n"
NEXT_MONDAY = "next 0 2026-11-09T00:00:00Z\n"
TITLE_FILTER = {
    "team": {"key": {"eq": "ABC"}},
    "creator": {"isMe": {"eq": True}},
    "title": {"eq": "Audit the example app, week of 2026-11-02"},
}
PREFIX_FILTER = {
    "team": {"key": {"eq": "ABC"}},
    "creator": {"isMe": {"eq": True}},
    "title": {"startsWith": "Audit the example app, week of "},
    "state": {"type": {"nin": ["completed", "canceled", "duplicate"]}},
}


def issue(identifier: str, week: str, state_type: str, team: str = "ABC") -> dict:
    return {
        "identifier": identifier, "title": f"Audit the example app, week of {week}", "team": team,
        "url": f"https://linear.app/example/issue/{identifier}", "state_type": state_type,
    }


class FileRecurringIssuesTest(unittest.TestCase):
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

    def run_script(self, entries: object = (ENTRY,), now: str = WEDNESDAY, **env) -> subprocess.CompletedProcess:
        settings = {} if entries is None else {"recurring_issues": entries if isinstance(entries, str) else list(entries)}
        self.state_path.write_text(json.dumps({"settings": settings}))
        env = {
            **os.environ, "HERMES_HOME": str(self.home), "HERMES_BIN": str(self.hermes), "FACTORY_NOW": now,
            "FAKE_HERMES_STATE": str(self.state_path), "LINEAR_CLIENT_ID": "id1", "LINEAR_CLIENT_SECRET": "secret", **env,
        }
        env = {key: value for key, value in env.items() if value is not None}
        return subprocess.run([sys.executable, str(SCRIPTS / "file-recurring-issues")], env=env, capture_output=True, text=True)

    def requests(self, key: str) -> list:
        return [entry[key] for entry in GitFixture.linear_requests(self.home) if key in entry]

    def writes(self) -> list:
        """Each issue write in the order the script made it."""
        names = ("issue_create", "attachment_create", "issue_update")
        return [(name, entry[name]) for entry in GitFixture.linear_requests(self.home) for name in names if name in entry]

    def test_files_one_triage_issue_for_the_slot_delegated_to_the_app(self) -> None:
        self.linear(team_labels=["Security"])

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, FILED + NEXT_MONDAY, ""))
        self.assertEqual(self.requests("open_titled"), [{"filter": TITLE_FILTER}, {"filter": PREFIX_FILTER}])
        self.assertEqual(self.writes(), [
            ("issue_create", {"input": {
                "teamId": "team-ABC", "title": "Audit the example app, week of 2026-11-02",
                "description": ENTRY["description"], "stateId": "state-triage", "labelIds": ["label-Security"],
            }}),
            ("attachment_create", REPOSITORY_LINK),
            ("issue_update", {"id": "issue-uuid", "input": {"delegateId": "app-user"}}),
        ])

    def test_an_open_issue_with_the_prefix_files_nothing(self) -> None:
        self.linear(team_labels=["Security"], issues=[issue("ABC-41", "2026-10-26", "started")])

        proc = self.run_script()

        self.assertEqual(
            (proc.returncode, proc.stdout), (0, "open 0 ABC-41 https://linear.app/example/issue/ABC-41\n" + NEXT_MONDAY),
        )
        self.assertEqual(self.writes(), [("attachment_create", REPOSITORY_LINK)])

    def test_a_closed_issue_for_the_slot_files_nothing_until_the_next_slot(self) -> None:
        self.linear(team_labels=["Security"], issues=[issue("ABC-42", "2026-11-02", "completed")])

        this_week = self.run_script()
        next_week = self.run_script(now="2026-11-09T00:00:00Z")

        self.assertEqual(
            (this_week.returncode, this_week.stdout),
            (0, "slot 0 ABC-42 https://linear.app/example/issue/ABC-42\n" + NEXT_MONDAY),
        )
        self.assertEqual(
            (next_week.returncode, next_week.stdout), (0, FILED + "next 0 2026-11-16T00:00:00Z\n"),
        )
        self.assertEqual(
            [created["input"]["title"] for created in self.requests("issue_create")],
            ["Audit the example app, week of 2026-11-09"],
        )
        self.assertEqual(self.requests("attachment_create")[0], REPOSITORY_LINK)

    def test_entries_on_two_teams_each_file_their_own_issue(self) -> None:
        self.linear(team_labels=["Security"])
        other = {**ENTRY, "team_key": "DEF", "cadence": "daily", "title_prefix": "Daily check, ", "labels": []}

        proc = self.run_script([ENTRY, other])

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(proc.stdout, (
            FILED + NEXT_MONDAY
            + "filed 1 DEF-900 https://linear.app/example/issue/DEF-900\nnext 1 2026-11-05T00:00:00Z\n"
        ))
        self.assertEqual(
            [(created["input"]["teamId"], created["input"]["title"]) for created in self.requests("issue_create")],
            [("team-ABC", "Audit the example app, week of 2026-11-02"), ("team-DEF", "Daily check, 2026-11-04")],
        )

    def test_a_malformed_entry_is_named_and_skipped_while_a_valid_one_files(self) -> None:
        self.linear(team_labels=["Security"])
        without_team = {key: value for key, value in ENTRY.items() if key != "team_key"}
        for entry, message in (
            ("text", "recurring_issues[1] must be an object, not 'text'"),
            (without_team, "recurring_issues[1] team_key None must be a non-empty string"),
            ({**ENTRY, "labels": "Security"}, "recurring_issues[1] labels 'Security' must be a list of non-empty strings"),
            ({**ENTRY, "title_prefix": 5}, "recurring_issues[1] title_prefix 5 must be a non-empty string"),
            ({**ENTRY, "cadence": "hourly"}, "recurring_issues[1] cadence 'hourly' must be one of daily, weekly, monthly"),
            ({**ENTRY, "repository": "example-app"}, "recurring_issues[1] repository 'example-app' must be <owner>/<repo>"),
        ):
            with self.subTest(message=message):
                (self.home / "linear-fake.log").unlink(missing_ok=True)

                proc = self.run_script([ENTRY, entry])

                self.assertEqual(
                    (proc.returncode, proc.stdout, proc.stderr), (2, FILED + NEXT_MONDAY, f"file-recurring-issues: {message}\n"),
                )
                self.assertEqual(len(self.requests("issue_create")), 1)

    def test_entries_whose_prefixes_collide_on_one_team_are_both_rejected(self) -> None:
        self.linear(team_labels=["Security"])
        longer = {**ENTRY, "title_prefix": "Audit the example app, week of 2026 "}
        elsewhere = {**ENTRY, "team_key": "DEF"}

        proc = self.run_script([ENTRY, longer, elsewhere])

        self.assertEqual((proc.returncode, proc.stdout), (2, "filed 2 DEF-900 https://linear.app/example/issue/DEF-900\nnext 2 2026-11-09T00:00:00Z\n"))
        self.assertEqual(proc.stderr, (
            "file-recurring-issues: recurring_issues[0] title_prefix 'Audit the example app, week of ' collides with "
            "recurring_issues[1] title_prefix 'Audit the example app, week of 2026 ' on team ABC\n"
            "file-recurring-issues: recurring_issues[1] title_prefix 'Audit the example app, week of 2026 ' collides with "
            "recurring_issues[0] title_prefix 'Audit the example app, week of ' on team ABC\n"
        ))
        self.assertEqual([created["input"]["teamId"] for created in self.requests("issue_create")], ["team-DEF"])

    def test_a_failed_read_exits_1_and_makes_no_further_linear_call(self) -> None:
        self.linear(team_labels=["Security"], open_titled_http_status=500)

        proc = self.run_script([ENTRY, {**ENTRY, "team_key": "DEF"}])

        self.assertEqual(
            (proc.returncode, proc.stdout, proc.stderr), (1, "", "file-recurring-issues: Linear failed: Internal Server Error\n"),
        )
        self.assertEqual(len(self.requests("open_titled")), 1)
        self.assertEqual(self.requests("issue_create"), [])

    def test_a_failure_outranks_a_malformed_entry_in_the_exit_code(self) -> None:
        self.linear(team_labels=["Security"], open_titled_http_status=500)

        proc = self.run_script([ENTRY, "text"])

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, (
            "file-recurring-issues: recurring_issues[1] must be an object, not 'text'\n"
            "file-recurring-issues: Linear failed: Internal Server Error\n"
        ))

    def test_a_missing_label_exits_1_before_creating_it_and_stops_the_run(self) -> None:
        self.linear(team_labels=["Tools"])

        proc = self.run_script([ENTRY, {**ENTRY, "team_key": "DEF"}])

        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertIn("no label named 'Security'", proc.stderr)
        self.assertEqual(self.requests("issue_create"), [])
        self.assertEqual(len(self.requests("open_titled")), 2)

    def test_no_entries_make_no_linear_call(self) -> None:
        for entries in (None, []):
            with self.subTest(entries=entries):
                proc = self.run_script(entries)

                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
                self.assertEqual(GitFixture.linear_requests(self.home), [])

    def test_a_section_that_is_not_a_list_exits_2_before_any_linear_call(self) -> None:
        proc = self.run_script("weekly")

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertEqual(proc.stderr, "file-recurring-issues: recurring_issues 'weekly' must be a list\n")
        self.assertEqual(GitFixture.linear_requests(self.home), [])

    def test_a_malformed_clock_or_missing_home_exits_2(self) -> None:
        clock = self.run_script(now="next tuesday")
        home = self.run_script(HERMES_HOME=None)

        self.assertEqual((clock.returncode, clock.stdout), (2, ""))
        self.assertEqual(clock.stderr, "file-recurring-issues: Invalid isoformat string: 'next tuesday'\n")
        self.assertEqual((home.returncode, home.stdout), (2, ""))
        self.assertEqual(
            home.stderr, "file-recurring-issues: HERMES_HOME is not set; point it at the claude-worker profile home\n",
        )
        self.assertEqual(GitFixture.linear_requests(self.home), [])


class CadenceTest(unittest.TestCase):
    def test_slot_and_next_slot_starts(self) -> None:
        sunday = datetime(2026, 11, 8, 23, 59, tzinfo=timezone.utc)
        for cadence, now, slot, after in (
            ("weekly", sunday, date(2026, 11, 2), date(2026, 11, 9)),
            ("weekly", datetime(2026, 11, 9, 0, 0, tzinfo=timezone.utc), date(2026, 11, 9), date(2026, 11, 16)),
            ("daily", sunday, date(2026, 11, 8), date(2026, 11, 9)),
            ("monthly", datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc), date(2026, 12, 1), date(2027, 1, 1)),
        ):
            with self.subTest(cadence=cadence, now=now):
                entry = recurring_issues.Entry(0, "o/r", "ABC", "P ", cadence, (), "d")

                self.assertEqual((entry.slot_start(now), entry.next_slot_start(now)), (slot, after))

    def test_the_slot_follows_the_utc_date(self) -> None:
        entry = recurring_issues.Entry(0, "o/r", "ABC", "P ", "weekly", (), "d")
        monday_in_utc_sunday_locally = datetime.fromisoformat("2026-11-09T01:00:00+09:00")

        self.assertEqual(entry.slot_start(monday_in_utc_sunday_locally), date(2026, 11, 2))


if __name__ == "__main__":
    unittest.main()
