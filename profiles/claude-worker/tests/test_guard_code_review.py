from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import SCRIPTS, with_interpreter
from tests.test_code_review_limit import BASE, HEAD, PR, FakeClient, finding, thought

sys.path.insert(0, str(SCRIPTS))
import code_review_rounds as rounds  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("guard_code_review", str(SCRIPTS / "guard-code-review"))
_spec = importlib.util.spec_from_loader("guard_code_review", _loader)
GUARD = importlib.util.module_from_spec(_spec)
_loader.exec_module(GUARD)

REVIEW = {"tool_name": "Skill", "tool_input": {"skill": "code-review"}}
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if os.environ.get("FAKE_GH_DOWN"):
    sys.exit("gh: connection refused")
if args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "example-org/example-repo"}))
elif args[:2] == ["pr", "list"]:
    print(json.dumps([{"number": 42}] if os.environ.get("FAKE_PR") else []))
else:
    sys.exit("unexpected gh call " + " ".join(args))
"""
ARGS = argparse.Namespace(session_id="live", task_id="t_1", board="/nonexistent/kanban.db")


class FakeLinearModule:
    constructed = 0
    client: FakeClient

    class TokenLinearClient:
        def __new__(cls, token, **kwargs):
            FakeLinearModule.constructed += 1
            return FakeLinearModule.client


def blocked_round(number: int, blocking: int) -> dict:
    return thought(number, blocking=blocking, findings=[finding("Bugs", "Important", str(n)) for n in range(blocking)])


class GuardCodeReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.gh = self.tmp / "fake-gh"
        self.gh.write_text(with_interpreter(FAKE_GH))
        os.chmod(self.gh, 0o755)
        self.tree = self.tmp / "tree"
        self.tree.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "feature", str(self.tree)], check=True)
        FakeLinearModule.constructed = 0
        env = {"GH_BIN": str(self.gh), "FAKE_PR": "1", "LINEAR_API_KEY": "token"}
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(tempfile, "tempdir", str(self.tmp)),
            mock.patch.object(rounds, "linear_module", lambda: FakeLinearModule),
            mock.patch.object(GUARD.kanban_board, "current_session_id", lambda board, task, fallback: fallback),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def decide(self, event=REVIEW, sessions=None) -> tuple[int, str]:
        FakeLinearModule.client = FakeClient({"live": sessions or []})
        return GUARD.decide({**event, "cwd": str(self.tree)}, ARGS)

    def test_a_fourth_round_after_three_with_blocking_findings_is_refused_naming_the_findings_file(self) -> None:
        code, text = self.decide(sessions=[blocked_round(1, 1), blocked_round(2, 1), blocked_round(3, 2)])

        self.assertEqual(code, 2)
        path = self.tmp / "code-review-example-org-example-repo-42-findings.json"
        self.assertIn(f"Refused: this pull request has had 3 code-review rounds and the last left 2 blocking", text)
        self.assertIn("`Needs input:` followed by a summary of the open findings", text)
        self.assertIn(str(path), text)
        self.assertEqual(len(json.loads(path.read_text())), 2)

    def test_a_refusal_whose_last_round_has_unreadable_findings_points_to_the_session(self) -> None:
        unreadable = {**blocked_round(3, 2), "body": blocked_round(3, 2)["body"].split("\n", 1)[0]}

        code, text = self.decide(sessions=[blocked_round(1, 1), blocked_round(2, 1), unreadable])

        self.assertEqual(code, 2)
        self.assertIn("which are in your session, because the last round's record holds no readable findings", text)
        self.assertFalse((self.tmp / "code-review-example-org-example-repo-42-findings.json").exists())

    def test_a_namespaced_skill_name_is_the_code_review_skill(self) -> None:
        event = {"tool_name": "Skill", "tool_input": {"skill": "code:code-review"}}

        code, _ = self.decide(event, [blocked_round(1, 1), blocked_round(2, 1), blocked_round(3, 1)])

        self.assertEqual(code, 2)

    def test_two_rounds_allow_a_third(self) -> None:
        self.assertEqual(self.decide(sessions=[blocked_round(1, 1), blocked_round(2, 1)]), (0, ""))

    def test_three_rounds_whose_last_has_no_blocking_finding_allow(self) -> None:
        self.assertEqual(self.decide(sessions=[blocked_round(1, 1), blocked_round(2, 1), thought(3, blocking=0)]), (0, ""))

    def test_a_branch_without_an_open_pull_request_allows(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_PR": ""}):
            self.assertEqual(self.decide(sessions=[blocked_round(n, 1) for n in (1, 2, 3)]), (0, ""))
        self.assertEqual(FakeLinearModule.constructed, 0)

    def test_a_failed_linear_read_allows_with_a_note(self) -> None:
        FakeLinearModule.client = FakeClient({"live": []})
        FakeLinearModule.client.read_session = mock.Mock(side_effect=RuntimeError("Linear is down"))

        code, text = GUARD.decide({**REVIEW, "cwd": str(self.tree)}, ARGS)

        self.assertEqual(code, 0)
        self.assertIn("allowed code-review without a round count (Linear is down)", text)

    def test_a_failed_gh_read_allows_with_a_note(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_GH_DOWN": "1"}):
            code, text = self.decide(sessions=[blocked_round(n, 1) for n in (1, 2, 3)])

        self.assertEqual(code, 0)
        self.assertIn("allowed code-review without a round count", text)

    def test_another_skill_is_allowed_without_any_lookup(self) -> None:
        FakeLinearModule.client = FakeClient({"live": [blocked_round(n, 1) for n in (1, 2, 3)]})
        FakeLinearModule.client.read_session = mock.Mock()

        with mock.patch.object(GUARD, "run", side_effect=AssertionError("ran a command")):
            code, text = GUARD.decide({"tool_name": "Skill", "tool_input": {"skill": "deslop"}}, ARGS)

        self.assertEqual((code, text, FakeLinearModule.constructed), (0, "", 0))
        FakeLinearModule.client.read_session.assert_not_called()

    def test_a_bad_time_limit_allows_with_a_note(self) -> None:
        with mock.patch.dict(os.environ, {"GUARD_CODE_REVIEW_SECONDS": "soon"}):
            code, text = self.decide(sessions=[blocked_round(n, 1) for n in (1, 2, 3)])

        self.assertEqual(code, 0)
        self.assertIn("GUARD_CODE_REVIEW_SECONDS='soon' is not a positive number of seconds", text)

    def test_run_as_a_hook_a_call_for_another_skill_or_tool_exits_0_silently(self) -> None:
        for payload, expected in (({"tool_name": "Skill", "tool_input": {"skill": "deslop"}}, 0), ({"tool_name": "Bash"}, 0)):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "guard-code-review"), "--session-id", "s", "--task-id", "t", "--board", "b"],
                input=json.dumps(payload), capture_output=True, text=True,
            )
            self.assertEqual((proc.returncode, proc.stderr), (expected, ""))


if __name__ == "__main__":
    unittest.main()
