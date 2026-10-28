from __future__ import annotations

import json
import os
import re
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
# Hermes' `_RESPAWN_GUARD_PR_URL_RE`: a match in a card comment holds the reopened card from its worker.
HERMES_PULL_URL = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)
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
    sys.exit(0)
if argv[:3] == ["api", "-X", "POST"]:
    print("{}")
    sys.exit(0)
if argv[0] == "api":
    repo = "/".join(argv[1].split("/")[1:3])
    if repo in state.get("rules_fails", []):
        sys.exit("gh: HTTP 403")
    print(json.dumps(state["rules"].get(repo, [])))
    sys.exit(0)
if argv[:2] == ["pr", "view"]:
    pulls = [p for repo_pulls in state["pulls"].values() for p in repo_pulls if p["url"] == argv[2]]
    print(json.dumps({"statusCheckRollup": pulls[0]["statusCheckRollup"]}))
    sys.exit(0)
repo = argv[argv.index("-R") + 1]
if repo in state.get("gh_fails", []):
    sys.exit("gh: HTTP 502")
print(json.dumps([{k: v for k, v in p.items() if k != "statusCheckRollup"} for p in state["pulls"].get(repo, [])]))
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


def pull(
    number: int, branch: str, *, head: str = HEAD, mergeable: str = "CONFLICTING", author: str = "app/factory-app",
    base: str = "main", checks: list | None = None, **extra,
) -> dict:
    return {
        **extra,
        "number": number, "url": f"https://github.com/{REPO}/pull/{number}", "headRefName": branch,
        "headRefOid": head, "baseRefName": base, "author": {"login": author}, "mergeable": mergeable,
        "statusCheckRollup": checks or [],
    }


def check_run(name: str, conclusion: str | None = "FAILURE", *, started: str = "2026-01-01T00:00:00Z", url: str | None = None) -> dict:
    return {
        "__typename": "CheckRun", "name": name, "workflowName": "ci", "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion or "", "startedAt": started, "detailsUrl": url or f"https://github.com/{REPO}/actions/runs/{name}",
    }


def status_context(name: str, state: str, url: str | None = None) -> dict:
    return {"__typename": "StatusContext", "context": name, "state": state, "targetUrl": url}


def required(*names: str) -> list:
    return [
        {"type": "pull_request", "parameters": {}},
        {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": name} for name in names]}},
    ]


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
            "settings": {"github": {"login": BOT_LOGIN}}, "tasks": [], "comments": {}, "pulls": {}, "graphql": {}, "rules": {},
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
        calls = [call for call in self.hermes_calls() if call[:2] not in reads]
        for call in calls:
            if call[:2] == ["kanban", "comment"]:
                self.assertIsNone(HERMES_PULL_URL.search(call[3]), call[3])
        return calls

    def gh_calls(self) -> list:
        log = Path(str(self.state_path) + ".gh")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def cli_calls(self) -> list:
        log = Path(str(self.state_path) + ".cli")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def review_comment(self, number: int, reason: str, head: str = HEAD) -> str:
        return (
            f"Pull request {REPO}#{number} has a changes-requested review and is not ready for "
            f"another look: {reason}. Fix it and publish the fix with push-signed. When you cannot, stop and make the "
            "first line of your final message `Needs input:` followed by the reason.\n\n"
            f"review_head: {head}"
        )

    def review_case(self, *, reviews: list, checks: list | None = None, requested: list | None = None,
                    threads: list | None = None, status: str = "review", session: str | None = SESSION) -> subprocess.CompletedProcess:
        self.state["tasks"] = [card("t_1", status, "abc-1", session=session)]
        self.state["pulls"][REPO] = [pull(
            7, "abc-1", mergeable="MERGEABLE", reviewDecision="CHANGES_REQUESTED",
            checks=[check("ci")] if checks is None else checks,
        )]
        self.state["rules"][REPO] = required("ci")
        self.state["graphql"]["7"] = graphql(reviews, requested, threads)
        return self.run_script()

    def comment(self, number: int, head: str = HEAD, base: str = "main") -> list:
        text = (
            f"Pull request {REPO}#{number} conflicts with {base}. Merge origin/{base} into the "
            "branch, resolve the conflicts, and publish the merge with push-signed. When you cannot resolve a conflict "
            "with confidence, stop and make the first line of your final message `Needs input:` followed by a reason "
            "naming each conflicting file.\n\n"
            f"conflict_head: {head}"
        )
        return text

    def failed_comment(self, number: int, listed: str, head: str = HEAD) -> str:
        return (
            f"Pull request {REPO}#{number} at head {head} fails a required check:\n{listed}\n\n"
            "Read the failing job, fix the cause, and publish the fix with push-signed. When the failure lies in a "
            "step the pull request does not change, re-run it once with `gh run rerun <run-id> --failed`. When the "
            "pull request body fails the repository's body check, rewrite the body. When you cannot fix it with "
            "confidence, stop and make the first line of your final message `Needs input:` followed by the reason.\n\n"
            f"failed_check_head: {head}"
        )

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
            "number,url,headRefName,headRefOid,baseRefName,author,mergeable,reviewDecision",
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
            ["pr", "view", f"https://github.com/{REPO}/pull/7", "--json", "statusCheckRollup"],
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

    def test_a_stale_changes_request_with_a_failing_required_check_gets_the_failed_check_handback(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale, checks=[check("ci", "FAILURE")])

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_1 https://github.com/{REPO}/pull/7\n"))
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_1", self.failed_comment(7, "- `ci`")],
            ["kanban", "reopen-review", "t_1", "--reason", "a required check failed; see the card comments"],
        ])
        self.assertEqual(self.cli_calls(), [])
        self.assertEqual([call for call in self.gh_calls() if call[:2] == ["api", "graphql"] or call[:2] == ["api", "-X"]], [])

    def test_a_blocked_card_with_a_stale_changes_request_and_a_failing_required_check_changes_nothing(self) -> None:
        stale = [review("rev", "CHANGES_REQUESTED", NEW_HEAD, "2026-01-01T00:00:00Z")]

        proc = self.review_case(reviews=stale, checks=[check("ci", "FAILURE")], status="blocked")

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual((self.writes(), self.cli_calls()), ([], []))
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

    def test_a_pending_required_check_holds_the_review_request(self) -> None:
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

    FAILED_REASON = "a required check failed; see the card comments"
    RULES_CALL = ["api", f"repos/{REPO}/rules/branches/main?per_page=100"]

    def failing_review_card(self, **kwargs) -> None:
        checks = [check_run("lint", url="https://ci.example/lint"), check_run("build", "SUCCESS"), check_run("docs", "FAILURE")]
        self.state["tasks"] = [card("t_1", kwargs.get("status", "review"), "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", mergeable="MERGEABLE", checks=checks, head=kwargs.get("head", HEAD))]
        self.state["rules"][REPO] = required("lint", "build")

    def test_a_review_card_whose_required_check_failed_gets_a_comment_then_a_review_reopen(self) -> None:
        self.failing_review_card()

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, f"t_1 https://github.com/{REPO}/pull/7\n", ""))
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_1", self.failed_comment(7, "- `lint`: https://ci.example/lint")],
            ["kanban", "reopen-review", "t_1", "--reason", self.FAILED_REASON],
        ])
        self.assertEqual(self.gh_calls()[1:], [
            ["pr", "view", f"https://github.com/{REPO}/pull/7", "--json", "statusCheckRollup"], self.RULES_CALL,
        ])

    def test_a_blocked_card_whose_required_check_failed_changes_nothing(self) -> None:
        self.failing_review_card(status="blocked")

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr, self.writes()), (0, "", "", []))
        self.assertEqual([call[0] for call in self.gh_calls()], ["pr"])

    def test_a_status_context_failure_is_listed_with_its_target_url_and_names_are_sorted(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", mergeable="MERGEABLE", checks=[
            status_context("zeta", "FAILURE", "https://ci.example/zeta"), status_context("alpha", "ERROR"),
        ])]
        self.state["rules"][REPO] = required("zeta", "alpha")

        self.run_script()

        listed = "- `alpha`\n- `zeta`: https://ci.example/zeta"
        self.assertEqual(self.writes()[0], ["kanban", "comment", "t_1", self.failed_comment(7, listed)])

    def test_a_target_url_on_a_pull_request_page_is_written_as_a_reference(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", mergeable="MERGEABLE", checks=[
            status_context("ci", "FAILURE", f"https://github.com/{REPO}/pull/7/checks?check_run_id=9"),
        ])]
        self.state["rules"][REPO] = required("ci")

        self.run_script()

        listed = f"- `ci`: {REPO}#7/checks?check_run_id=9"
        self.assertEqual(self.writes()[0], ["kanban", "comment", "t_1", self.failed_comment(7, listed)])

    def test_a_failed_check_head_already_on_the_card_skips_blocked_and_only_reopens_review(self) -> None:
        self.state["tasks"] = [card("t_1", "blocked", "abc-1"), card("t_2", "review", "abc-2")]
        self.state["pulls"][REPO] = [
            pull(7, "abc-1", mergeable="MERGEABLE", checks=[check_run("lint")]),
            pull(8, "abc-2", mergeable="MERGEABLE", checks=[check_run("lint")]),
        ]
        self.state["rules"][REPO] = required("lint")
        marker = {"body": f"earlier\nfailed_check_head: {HEAD}"}
        self.state["comments"] = {"t_1": [marker], "t_2": [marker]}

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_2 https://github.com/{REPO}/pull/8\n"))
        self.assertEqual(self.writes(), [["kanban", "reopen-review", "t_2", "--reason", self.FAILED_REASON]])

    def test_a_conflict_marker_does_not_stand_for_a_failed_check_and_a_new_head_is_told_again(self) -> None:
        self.failing_review_card(head=NEW_HEAD)
        self.state["comments"] = {"t_1": [{"body": f"failed_check_head: {HEAD}\nconflict_head: {NEW_HEAD}"}]}

        proc = self.run_script()

        self.assertEqual(proc.stdout, f"t_1 https://github.com/{REPO}/pull/7\n")
        self.assertEqual(self.writes()[0][:3], ["kanban", "comment", "t_1"])
        self.assertTrue(self.writes()[0][3].endswith(f"failed_check_head: {NEW_HEAD}"))

    def test_a_failure_no_rule_requires_changes_nothing(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", mergeable="MERGEABLE", checks=[check_run("lint"), check_run("build", "SUCCESS")])]
        self.state["rules"][REPO] = required("build")

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr, self.writes()), (0, "", "", []))

    def test_a_required_check_whose_newer_run_passed_changes_nothing(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", mergeable="MERGEABLE", checks=[
            check_run("lint", "FAILURE", started="2026-01-01T00:00:00Z"), check_run("lint", "SUCCESS", started="2026-01-01T01:00:00Z"),
            check_run("docs"),
        ])]
        self.state["rules"][REPO] = required("lint")

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, self.writes()), (0, "", []))

    def test_a_pending_required_check_changes_nothing(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1"), card("t_2", "review", "abc-2"), card("t_3", "review", "abc-3")]
        self.state["pulls"][REPO] = [
            pull(7, "abc-1", mergeable="MERGEABLE", checks=[check_run("lint"), check_run("build", None)]),
            pull(8, "abc-2", mergeable="MERGEABLE", checks=[check_run("lint"), status_context("build", "PENDING")]),
            pull(9, "abc-3", mergeable="MERGEABLE", checks=[check_run("lint")]),
        ]
        self.state["rules"][REPO] = required("lint", "build")

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr, self.writes()), (0, "", "", []))

    def test_unknown_mergeability_reads_no_checks_even_when_one_failed(self) -> None:
        self.failing_review_card()
        self.state["pulls"][REPO][0]["mergeable"] = "UNKNOWN"

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, self.writes()), (0, "", []))
        self.assertEqual([call[0] for call in self.gh_calls()], ["pr"])

    def test_a_mergeable_pull_request_with_passing_checks_reads_no_rules(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", mergeable="MERGEABLE", checks=[check_run("lint", "SUCCESS")])]

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout, self.writes()), (0, "", []))
        self.assertEqual([call[0] for call in self.gh_calls()], ["pr", "pr"])

    def test_a_conflict_wins_over_a_failed_check(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1", checks=[check_run("lint")])]
        self.state["rules"][REPO] = required("lint")

        self.run_script()

        self.assertEqual([call[:2] for call in self.writes()], [["kanban", "comment"], ["kanban", "reopen-review"]])
        self.assertEqual(self.writes()[0][3], self.comment(7))
        self.assertEqual(len(self.gh_calls()), 1)

    def test_a_rules_read_failure_is_reported_and_the_other_repository_still_runs(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1", OTHER_REPO), card("t_2", "review", "abc-2")]
        self.state["pulls"][OTHER_REPO] = [pull(7, "abc-1", mergeable="MERGEABLE", checks=[check_run("lint")])]
        self.state["pulls"][REPO] = [pull(8, "abc-2")]
        self.state["rules_fails"] = [OTHER_REPO]

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, f"t_2 https://github.com/{REPO}/pull/8\n")
        self.assertEqual(proc.stderr, "reopen-conflicts: t_1: gh api repos/example-org/other-repo/rules/branches/main?per_page=100 failed: gh: HTTP 403\n")

    def test_the_rules_are_read_once_for_each_repository_and_base(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1"), card("t_2", "review", "abc-2"), card("t_3", "review", "abc-3")]
        failing = [check_run("lint")]
        self.state["pulls"][REPO] = [
            pull(7, "abc-1", mergeable="MERGEABLE", checks=failing),
            pull(8, "abc-2", mergeable="MERGEABLE", checks=failing),
            pull(9, "abc-3", mergeable="MERGEABLE", checks=failing, base="release/1"),
        ]
        self.state["rules"][REPO] = required("lint")

        proc = self.run_script()

        self.assertEqual(proc.stdout.count("\n"), 3)
        self.assertEqual([call for call in self.gh_calls() if call[0] == "api"], [
            self.RULES_CALL, ["api", f"repos/{REPO}/rules/branches/release%2F1?per_page=100"],
        ])

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
