from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import BOT_LOGIN, SCRIPTS, with_interpreter

REPO = "example-org/example-repo"
OTHER_REPO = "example-org/other-repo"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
SETTINGS_KEY = "plugins.entries.linear-agent-session.settings"

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_STATE"]))
argv = sys.argv[1:]
with open(os.environ["FAKE_STATE"] + ".hermes", "a") as log:
    log.write(json.dumps(argv) + "\\n")
if argv[:2] == ["config", "get"]:
    print(json.dumps(state["settings"]))
elif argv[:2] == ["kanban", "list"]:
    print(json.dumps(state["tasks"]))
elif argv[:2] == ["kanban", "show"]:
    print(json.dumps({"task": {"id": argv[2]}, "comments": state["comments"].get(argv[2], [])}))
elif argv[:2] == ["kanban", "comment"] and argv[2] in state.get("comment_fails", []):
    sys.exit("hermes: cannot comment")
elif argv[:2] == ["kanban", "comment"] or argv[1] in ("unblock", "request-review", "reopen-review"):
    pass
else:
    sys.exit("unexpected " + repr(argv))
"""

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_STATE"]))
argv = sys.argv[1:]
with open(os.environ["FAKE_STATE"] + ".gh", "a") as log:
    log.write(json.dumps(argv) + "\\n")
repo = argv[argv.index("-R") + 1]
if repo in state.get("gh_fails", []):
    sys.exit("gh: HTTP 502")
print(json.dumps(state["pulls"].get(repo, [])))
"""


def pull(number: int, branch: str, *, head: str = HEAD, mergeable: str = "CONFLICTING", author: str = "app/factory-app", base: str = "main") -> dict:
    return {
        "number": number, "url": f"https://github.com/{REPO}/pull/{number}", "headRefName": branch,
        "headRefOid": head, "baseRefName": base, "author": {"login": author}, "mergeable": mergeable,
    }


def card(task_id: str, status: str, branch: str, repo: str = REPO) -> dict:
    return {"id": task_id, "status": status, "branch_name": branch, "body": f"issue text\nrepo: {repo}\nbranch: {branch}\n"}


class ReopenConflictsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "profiles" / "claude-worker").mkdir(parents=True)
        self.hermes = self.dir / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        self.gh = self.dir / "gh"
        self.gh.write_text(with_interpreter(FAKE_GH))
        for fake in (self.hermes, self.gh):
            os.chmod(fake, 0o755)
        self.state_path = self.dir / "state.json"
        self.state = {"settings": {"github": {"login": BOT_LOGIN}}, "tasks": [], "comments": {}, "pulls": {}}

    def run_script(self, home: Path | None = None) -> subprocess.CompletedProcess:
        self.state_path.write_text(json.dumps(self.state))
        env = {
            **os.environ,
            "HERMES_HOME": str(home or self.dir / "profiles" / "claude-worker"),
            "HERMES_BIN": str(self.hermes),
            "GH_BIN": str(self.gh),
            "FAKE_STATE": str(self.state_path),
        }
        return subprocess.run([sys.executable, str(SCRIPTS / "reopen-conflicts")], env=env, capture_output=True, text=True)

    def hermes_calls(self) -> list:
        log = Path(str(self.state_path) + ".hermes")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def writes(self) -> list:
        reads = (["config", "get"], ["kanban", "list"], ["kanban", "show"])
        return [call for call in self.hermes_calls() if call[:2] not in reads]

    def gh_calls(self) -> list:
        log = Path(str(self.state_path) + ".gh")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def comment(self, number: int, head: str = HEAD, base: str = "main") -> list:
        text = (
            f"Pull request https://github.com/{REPO}/pull/{number} conflicts with {base}. Merge origin/{base} into the "
            "branch, resolve the conflicts, and publish the merge with push-signed. When you cannot resolve a conflict "
            "with confidence, block this card as needs_input and name each conflicting file.\n\n"
            f"conflict_head: {head}"
        )
        return text

    def test_a_conflicting_review_card_gets_a_comment_then_a_review_reopen(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, f"t_1 https://github.com/{REPO}/pull/7\n", ""))
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_1", self.comment(7)],
            ["kanban", "reopen-review", "t_1", "--reason", "merge conflict with the base branch; see the card comments"],
        ])
        self.assertEqual(self.gh_calls(), [[
            "pr", "list", "-R", REPO, "--state", "open", "--limit", "200", "--json",
            "number,url,headRefName,headRefOid,baseRefName,author,mergeable",
        ]])

    def test_a_blocked_card_is_unblocked_and_sent_to_review_before_the_reopen(self) -> None:
        self.state["tasks"] = [card("t_2", "blocked", "abc-2")]
        self.state["pulls"][REPO] = [pull(8, "abc-2", base="develop")]

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_2 https://github.com/{REPO}/pull/8\n"))
        self.assertEqual(self.writes(), [
            ["kanban", "unblock", "t_2"],
            ["kanban", "request-review", "t_2"],
            ["kanban", "comment", "t_2", self.comment(8, base="develop")],
            ["kanban", "reopen-review", "t_2", "--reason", "merge conflict with the base branch; see the card comments"],
        ])

    def test_a_mergeable_or_unknown_pull_request_changes_nothing(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1"), card("t_2", "review", "abc-2")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", mergeable="MERGEABLE"), pull(8, "abc-2", mergeable="UNKNOWN")]

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(self.hermes_calls(), [["config", "get", SETTINGS_KEY, "--json", "--raw"], ["kanban", "list", "--json"]])

    def test_a_card_that_already_carries_the_head_is_skipped_and_a_new_head_is_acted_on(self) -> None:
        self.state["tasks"] = [card("t_1", "blocked", "abc-1"), card("t_2", "review", "abc-2")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", head=HEAD), pull(8, "abc-2", head=NEW_HEAD)]
        self.state["comments"] = {
            "t_1": [{"body": "unrelated"}, {"body": f"earlier text\n{self.comment(7, HEAD)}"}],
            "t_2": [{"body": self.comment(8, HEAD)}],
        }

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_2 https://github.com/{REPO}/pull/8\n"))
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_2", self.comment(8, NEW_HEAD)],
            ["kanban", "reopen-review", "t_2", "--reason", "merge conflict with the base branch; see the card comments"],
        ])

    def test_a_review_card_that_carries_the_head_only_reopens_again(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        self.state["comments"] = {"t_1": [{"body": self.comment(7)}]}

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_1 https://github.com/{REPO}/pull/7\n"))
        self.assertEqual(self.writes(), [
            ["kanban", "reopen-review", "t_1", "--reason", "merge conflict with the base branch; see the card comments"],
        ])

    def test_a_pull_request_with_no_author_is_ignored(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1"), card("t_2", "review", "abc-2")]
        self.state["pulls"][REPO] = [{**pull(7, "abc-1"), "author": None}, pull(8, "abc-2")]

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_2 https://github.com/{REPO}/pull/8\n"))

    def test_a_pull_request_from_a_person_is_ignored(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", author="person")]

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(self.writes(), [])

    def test_the_app_is_matched_by_either_login_form(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", author=BOT_LOGIN)]

        proc = self.run_script()

        self.assertEqual(proc.stdout, f"t_1 https://github.com/{REPO}/pull/7\n")

    def test_cards_in_other_states_or_without_a_branch_or_repo_are_ignored(self) -> None:
        self.state["tasks"] = [
            card("t_1", "running", "abc-1"), card("t_2", "done", "abc-2"), card("t_3", "ready", "abc-3"),
            {"id": "t_4", "status": "review", "branch_name": None, "body": f"repo: {REPO}\n"},
            {"id": "t_5", "status": "review", "branch_name": "abc-5", "body": "no repository line"},
        ]
        self.state["pulls"][REPO] = [pull(n, f"abc-{n}") for n in range(1, 6)]

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.gh_calls(), [])

    def test_one_repository_failing_still_processes_the_other(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1", OTHER_REPO), card("t_2", "review", "abc-2")]
        self.state["gh_fails"] = [OTHER_REPO]
        self.state["pulls"][REPO] = [pull(8, "abc-2")]

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, f"t_2 https://github.com/{REPO}/pull/8\n")
        self.assertEqual(proc.stderr, f"reopen-conflicts: {OTHER_REPO}: gh pr list failed: gh: HTTP 502\n")
        self.assertEqual([call[:3] for call in self.writes()], [["kanban", "comment", "t_2"], ["kanban", "reopen-review", "t_2"]])

    def test_one_card_failing_still_processes_the_others(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1"), card("t_2", "review", "abc-2")]
        self.state["pulls"][REPO] = [pull(7, "abc-1"), pull(8, "abc-2")]
        self.state["comment_fails"] = ["t_1"]

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, f"t_2 https://github.com/{REPO}/pull/8\n")
        self.assertIn("reopen-conflicts: t_1:", proc.stderr)
        self.assertNotIn(["kanban", "reopen-review", "t_1"], [call[:3] for call in self.writes()])

    def test_a_home_that_is_not_a_profile_home_exits_2(self) -> None:
        proc = self.run_script(home=self.dir)

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertIn("is not a profile home", proc.stderr)
        self.assertEqual(self.hermes_calls(), [])

    def test_missing_settings_exit_2_without_printing_them(self) -> None:
        self.state["settings"] = {"secret": "do-not-print"}

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertEqual(proc.stderr, "reopen-conflicts: github.login is not set in the factory settings\n")


if __name__ == "__main__":
    unittest.main()
