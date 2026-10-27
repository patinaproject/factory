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

sys.path.insert(0, str(SCRIPTS))
from stale_reviews import REVIEWS_QUERY  # noqa: E402

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
if argv[:2] == ["api", "graphql"]:
    number = next(a.split("=", 1)[1] for a in argv if a.startswith("number="))
    print(json.dumps(state["graphql"][number]))
elif argv[0] == "api" and "/rules/branches/" in argv[1]:
    print(json.dumps(state["rules"]))
elif argv[:3] == ["api", "-X", "POST"]:
    print("{}")
else:
    repo = argv[argv.index("-R") + 1]
    if repo in state.get("gh_fails", []):
        sys.exit("gh: HTTP 502")
    print(json.dumps(state["pulls"].get(repo, [])))
"""

FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_STATE"] + ".cli", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
"""

SESSION = "11111111-2222-3333-4444-555555555555"
CHECK_RUN = {"__typename": "CheckRun", "workflowName": "ci", "startedAt": "2026-01-01T00:00:00Z"}


def check(name: str, conclusion: str | None = "SUCCESS") -> dict:
    status = "COMPLETED" if conclusion else "IN_PROGRESS"
    return {**CHECK_RUN, "name": name, "status": status, "conclusion": conclusion}


def review(login: str, state: str, oid: str, at: str) -> dict:
    return {"state": state, "submittedAt": at, "author": {"login": login}, "commit": {"oid": oid}}


def graphql(reviews: list, requested: list | None = None, threads: list | None = None) -> dict:
    nodes = lambda items: {"nodes": items}
    return {"data": {"repository": {"pullRequest": {
        "reviews": nodes(reviews),
        "reviewRequests": nodes([{"requestedReviewer": {"login": login}} for login in requested or []]),
        "reviewThreads": nodes([
            {"isResolved": resolved, "comments": nodes([{"author": {"login": login}}])} for login, resolved in threads or []
        ]),
    }}}}


def pull(number: int, branch: str, *, head: str = HEAD, mergeable: str = "CONFLICTING", author: str = "app/factory-app", base: str = "main", **extra) -> dict:
    return {
        **extra,
        "number": number, "url": f"https://github.com/{REPO}/pull/{number}", "headRefName": branch,
        "headRefOid": head, "baseRefName": base, "author": {"login": author}, "mergeable": mergeable,
    }


def card(task_id: str, status: str, branch: str, repo: str = REPO, session: str | None = None) -> dict:
    line = f"linear_agent_session_id: {session}\n" if session else ""
    return {"id": task_id, "status": status, "branch_name": branch, "body": f"issue text\nrepo: {repo}\nbranch: {branch}\n{line}"}


class ReopenConflictsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "profiles" / "claude-worker").mkdir(parents=True)
        self.hermes = self.dir / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        self.gh = self.dir / "gh"
        self.gh.write_text(with_interpreter(FAKE_GH))
        cli = self.dir / "profiles" / "claude-worker" / "plugins" / "linear-agent-session" / "cli.py"
        cli.parent.mkdir(parents=True)
        cli.write_text(with_interpreter(FAKE_CLI))
        for fake in (self.hermes, self.gh):
            os.chmod(fake, 0o755)
        self.state_path = self.dir / "state.json"
        self.state = {
            "settings": {"github": {"login": BOT_LOGIN}}, "tasks": [], "comments": {}, "pulls": {},
            "graphql": {}, "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci"}]}}],
        }

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

    def cli_calls(self) -> list:
        log = Path(str(self.state_path) + ".cli")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def review_comment(self, number: int, reason: str, head: str = HEAD) -> str:
        return (
            f"Pull request https://github.com/{REPO}/pull/{number} has a changes-requested review and is not ready for "
            f"another look: {reason}. Fix it and publish the fix with push-signed. When you cannot, stop and make the "
            "first line of your final message `Needs input:` followed by the reason.\n\n"
            f"review_head: {head}"
        )

    def review_case(self, *, reviews: list, checks: list | None = None, requested: list | None = None,
                    threads: list | None = None, status: str = "review", session: str | None = SESSION) -> subprocess.CompletedProcess:
        self.state["tasks"] = [card("t_1", status, "abc-1", session=session)]
        self.state["pulls"][REPO] = [pull(
            7, "abc-1", mergeable="MERGEABLE", reviewDecision="CHANGES_REQUESTED",
            statusCheckRollup=[check("ci")] if checks is None else checks,
        )]
        self.state["graphql"]["7"] = graphql(reviews, requested, threads)
        return self.run_script()

    def comment(self, number: int, head: str = HEAD, base: str = "main") -> list:
        text = (
            f"Pull request https://github.com/{REPO}/pull/{number} conflicts with {base}. Merge origin/{base} into the "
            "branch, resolve the conflicts, and publish the merge with push-signed. When you cannot resolve a conflict "
            "with confidence, stop and make the first line of your final message `Needs input:` followed by a reason "
            "naming each conflicting file.\n\n"
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
            "number,url,headRefName,headRefOid,baseRefName,author,mergeable,reviewDecision,statusCheckRollup",
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

    def test_a_stale_changes_request_with_passing_checks_is_requested_again_and_the_session_is_told(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale)

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, f"t_1 https://github.com/{REPO}/pull/7 re-requested rev\n", ""))
        self.assertEqual(self.gh_calls()[1:], [
            ["api", "graphql", "-f", f"query={REVIEWS_QUERY}", "-f", "owner=example-org", "-f", "name=example-repo", "-F", "number=7"],
            ["api", f"repos/{REPO}/rules/branches/main?per_page=100"],
            ["api", "-X", "POST", f"repos/{REPO}/pulls/7/requested_reviewers", "-f", "reviewers[]=rev"],
        ])
        self.assertEqual(self.cli_calls(), [[
            "activity", "--agent-session-id", SESSION, "--type", "elicitation",
            "--body", "Changes are in, re-requested review from @rev",
            "--external-url", f"Pull request=https://github.com/{REPO}/pull/7",
        ]])
        self.assertEqual(self.writes(), [])

    def test_two_stale_reviewers_are_requested_in_one_call(self) -> None:
        stale = [
            review("a", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z"),
            review("b", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-02T00:00:00Z"),
        ]

        proc = self.review_case(reviews=stale)

        self.assertEqual(proc.stdout, f"t_1 https://github.com/{REPO}/pull/7 re-requested a,b\n")
        self.assertEqual(self.gh_calls()[-1], ["api", "-X", "POST", f"repos/{REPO}/pulls/7/requested_reviewers", "-f", "reviewers[]=a", "-f", "reviewers[]=b"])
        self.assertEqual(self.cli_calls()[0][6], "Changes are in, re-requested review from @a, @b")

    def test_a_stale_changes_request_with_a_failing_required_check_goes_back_to_the_worker(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale, checks=[check("ci", "FAILURE")])

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_1 https://github.com/{REPO}/pull/7\n"))
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_1", self.review_comment(7, "required checks failed: ci")],
            ["kanban", "reopen-review", "t_1", "--reason", "changes-requested review still needs work; see the card comments"],
        ])
        self.assertEqual(self.cli_calls(), [])
        self.assertEqual([call for call in self.gh_calls() if call[:2] == ["api", "-X"]], [])

    def test_a_changes_request_on_the_current_head_goes_back_to_the_worker(self) -> None:
        current = [review("rev", "CHANGES_REQUESTED", HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=current)

        self.assertEqual(proc.stdout, f"t_1 https://github.com/{REPO}/pull/7\n")
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_1", self.review_comment(7, "rev requested changes on the current head")],
            ["kanban", "reopen-review", "t_1", "--reason", "changes-requested review still needs work; see the card comments"],
        ])
        self.assertEqual([call for call in self.gh_calls() if call[:2] == ["api", "-X"]], [])

    def test_a_blocked_card_with_a_current_head_request_is_unblocked_first(self) -> None:
        current = [review("rev", "CHANGES_REQUESTED", HEAD, "2026-01-01T00:00:00Z")]

        self.review_case(reviews=current, status="blocked")

        self.assertEqual([call[:2] for call in self.writes()], [
            ["kanban", "unblock"], ["kanban", "request-review"], ["kanban", "comment"], ["kanban", "reopen-review"],
        ])

    def test_a_review_card_that_carries_the_review_head_only_reopens_again(self) -> None:
        current = [review("rev", "CHANGES_REQUESTED", HEAD, "2026-01-01T00:00:00Z")]
        self.state["comments"] = {"t_1": [{"body": self.review_comment(7, "rev requested changes on the current head")}]}

        self.review_case(reviews=current)

        self.assertEqual(self.writes(), [
            ["kanban", "reopen-review", "t_1", "--reason", "changes-requested review still needs work; see the card comments"],
        ])

    def test_a_blocked_card_that_carries_the_review_head_is_skipped(self) -> None:
        current = [review("rev", "CHANGES_REQUESTED", HEAD, "2026-01-01T00:00:00Z")]
        self.state["comments"] = {"t_1": [{"body": self.review_comment(7, "rev requested changes on the current head")}]}

        proc = self.review_case(reviews=current, status="blocked")

        self.assertEqual((proc.stdout, self.writes()), ("", []))

    def test_a_reviewer_who_reviewed_again_since_is_left_alone(self) -> None:
        later = [
            review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z"),
            review("rev", "COMMENTED", HEAD, "2026-01-02T00:00:00Z"),
        ]

        proc = self.review_case(reviews=later)

        self.assertEqual((proc.returncode, proc.stdout, self.writes(), self.cli_calls()), (0, "", [], []))
        self.assertEqual([call for call in self.gh_calls() if call[:2] == ["api", "-X"]], [])

    def test_a_reviewer_who_is_already_requested_is_left_alone(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale, requested=["rev"])

        self.assertEqual((proc.stdout, self.writes(), self.cli_calls()), ("", [], []))

    def test_a_reviewer_with_an_unresolved_thread_sends_the_card_back_instead(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale, threads=[("rev", False), ("other", True)])

        self.assertEqual(proc.stdout, f"t_1 https://github.com/{REPO}/pull/7\n")
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_1", self.review_comment(7, "unresolved review threads from rev")],
            ["kanban", "reopen-review", "t_1", "--reason", "changes-requested review still needs work; see the card comments"],
        ])
        self.assertEqual((self.cli_calls(), [call for call in self.gh_calls() if call[:2] == ["api", "-X"]]), ([], []))

    def test_a_pending_required_check_changes_nothing(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale, checks=[check("ci", None)])

        self.assertEqual((proc.returncode, proc.stdout, self.writes(), self.cli_calls()), (0, "", [], []))

    def test_a_card_without_a_session_still_requests_the_reviewer_and_exits_1(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale, session=None)

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, f"t_1 https://github.com/{REPO}/pull/7 re-requested rev\n")
        self.assertEqual(proc.stderr, "reopen-conflicts: t_1: the card has no linear_agent_session_id line\n")
        self.assertEqual(self.cli_calls(), [])

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
