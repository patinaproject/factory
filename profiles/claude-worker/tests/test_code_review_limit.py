from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import code_review_rounds as rounds  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("code_review_limit", str(SCRIPTS / "code-review-limit"))
_spec = importlib.util.spec_from_loader("code_review_limit", _loader)
LIMIT = importlib.util.module_from_spec(_spec)
_loader.exec_module(LIMIT)

PR = "example-org/example-repo#42"
BASE = "a" * 40
HEAD = "b" * 40
HEAD_2 = "c" * 40


def finding(axis: str, severity: str, id: str = "f", state: str = "open") -> dict:
    return {"id": id, "axis": axis, "severity": severity, "location": "a.py:1", "text": "x", "state": state}


def thought(round_number: int, head: str = HEAD, blocking: int = 1, pr: str = PR, findings=None, at: str = "") -> dict:
    line = f"code-review round {round_number} pr {pr} base {BASE} head {head} blocking {blocking}"
    body = f"{line}\n\n```json\n{json.dumps(findings if findings is not None else [])}\n```"
    return {"type": "thought", "body": body, "createdAt": at or f"2026-11-0{round_number}T00:00:00Z"}


class FakeLinear:
    @staticmethod
    def activity_content(kind, body=None):
        return {"type": kind, "body": body}


class FakeClient:
    def __init__(self, sessions: dict[str, list[dict]]) -> None:
        self.sessions = sessions
        self.posted = []

    def issue_session_ids(self, session_id):
        return list(self.sessions)

    def read_session(self, session_id):
        return {"id": session_id, "activities": self.sessions[session_id]}

    def create_activity(self, session_id, content, *, ephemeral=False):
        self.posted.append((session_id, content, ephemeral))
        return "activity-1"


class StatusTest(unittest.TestCase):
    def test_rounds_in_two_sessions_of_the_issue_are_counted_together(self) -> None:
        client = FakeClient({
            "old": [thought(1, HEAD), {"type": "response", "body": "done", "createdAt": "2026-11-01T01:00:00Z"}],
            "live": [thought(2, HEAD_2, blocking=2, findings=[finding("Bugs", "Important")])],
        })

        report = LIMIT.status_report(rounds.read_status(client, "live", PR), PR)

        self.assertEqual(report["rounds"], 2)
        self.assertEqual(report["history"], [{"round": 1, "base": BASE, "head": HEAD}, {"round": 2, "base": BASE, "head": HEAD_2}])
        self.assertEqual((report["last_head"], report["blocking"]), (HEAD_2, 2))
        self.assertEqual(json.loads(Path(report["findings"]).read_text()), [finding("Bugs", "Important")])

    def test_a_first_line_that_is_not_exactly_the_marker_is_not_counted(self) -> None:
        good = thought(1)
        near_misses = [
            {**good, "body": good["body"].replace("round 1 pr", "round 1  pr")},
            {**good, "body": good["body"].replace(BASE, "a" * 39)},
            {**good, "body": good["body"].replace(BASE, "A" * 40)},
            {**good, "body": good["body"].replace(PR, "example-org/other#42")},
            {**good, "body": good["body"].replace(" blocking 1", " blocking 1 https://github.com/x/y/pull/1")},
            {**good, "body": "Note\n" + good["body"]},
            {**good, "type": "response"},
        ]
        client = FakeClient({"live": [*near_misses, thought(1, at="2026-11-09T00:00:00Z")]})

        status = rounds.read_status(client, "live", PR)

        self.assertEqual(len(status.rounds), 1)

    def test_no_rounds_report_nulls(self) -> None:
        report = LIMIT.status_report(rounds.read_status(FakeClient({"live": []}), "live", PR), PR)

        self.assertEqual(report, {"rounds": 0, "history": [], "last_head": None, "blocking": None, "findings": None})

    def test_the_current_session_is_read_when_the_issue_list_omits_it(self) -> None:
        client = FakeClient({"old": []})
        client.sessions["live"] = [thought(1)]
        client.issue_session_ids = lambda session_id: ["old"]

        self.assertEqual(len(rounds.read_status(client, "live", PR).rounds), 1)


class RecordTest(unittest.TestCase):
    def test_a_round_is_posted_as_a_thought_numbered_after_the_count_with_blocking_computed(self) -> None:
        findings = [
            finding("Bugs", "Important", "1"),
            finding("Security", "Nit", "2"),
            finding("Security", "Pre-existing", "3"),
            finding("Standards", "Nit", "4"),
            finding("Bugs", "Important", "5", state="answered"),
        ]
        client = FakeClient({"old": [thought(1)], "live": [thought(2)]})

        report = LIMIT.record_round(FakeLinear, client, "live", PR, BASE, HEAD_2, findings)

        self.assertEqual(report, {"round": 3, "blocking": 2})
        (session, content, ephemeral), = client.posted
        self.assertEqual((session, content["type"], ephemeral), ("live", "thought", False))
        self.assertEqual(
            content["body"].split("\n")[0],
            f"code-review round 3 pr {PR} base {BASE} head {HEAD_2} blocking 2",
        )
        parsed = rounds.parse_round(content["body"], PR)
        self.assertEqual((parsed.number, parsed.blocking, parsed.findings), (3, 2, findings))

    def test_a_record_with_a_missing_field_or_unknown_severity_is_refused(self) -> None:
        for bad in ([{"id": "1"}], [finding("Bugs", "Critical")], {"id": "1"}, [finding("Docs", "Nit")]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rounds.parse_findings(bad)

    def test_a_sha_that_is_not_40_lowercase_hex_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            LIMIT.record_round(FakeLinear, FakeClient({"live": []}), "live", PR, "abc123", HEAD, [])


if __name__ == "__main__":
    unittest.main()
