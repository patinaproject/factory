from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tests.support import BOT_LOGIN, SCRIPTS, with_interpreter

sys.path.insert(0, str(SCRIPTS))
from stale_reviews import REVIEWS_QUERY  # noqa: E402

ROOT = SCRIPTS.parents[2]
REPO = "example-org/example-repo"
OTHER_REPO = "example-org/other-repo"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
MERGE_BASE = "c" * 40
# Hermes' `_RESPAWN_GUARD_PR_URL_RE`: a match in a card comment holds the reopened card from its worker.
HERMES_PULL_URL = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)
SETTINGS_KEY = "plugins.entries.linear-agent-session.settings"
VIEW_FIELDS = "statusCheckRollup,isDraft,mergeStateStatus,labels"
QUEUE_KEY = "plugins.entries.pr-ready-gate.settings.merge_queue"

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_STATE"]))
argv = sys.argv[1:]
with open(os.environ["FAKE_STATE"] + ".hermes", "a") as log:
    log.write(json.dumps(argv) + "\\n")
if argv[:2] == ["config", "get"]:
    configured = {"plugins.entries.linear-agent-session.settings": state["settings"], **state.get("config", {})}
    if argv[2] not in configured:
        sys.exit("hermes: no such key")
    print(json.dumps(configured[argv[2]]))
elif argv[:2] == ["kanban", "list"]:
    print(json.dumps(state["tasks"]))
elif argv[:3] == ["kanban", "boards", "list"]:
    print(json.dumps([{"slug": "default", "db_path": state["board_db"]}]))
elif argv[:2] == ["kanban", "show"]:
    print(json.dumps({"task": {"id": argv[2]}, "comments": state["comments"].get(argv[2], [])}))
elif argv[:2] == ["kanban", "comment"] and argv[2] in state.get("comment_fails", []):
    sys.exit("hermes: cannot comment")
elif argv[:2] == ["kanban", "comment"]:
    state["comments"].setdefault(argv[2], []).append({"body": argv[3]})
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
elif argv[1] in ("unblock", "request-review", "reopen-review"):
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
if argv[0] == "api" and "/compare/" in argv[1]:
    print(json.dumps(state["compare"][argv[1].split("?")[0]]))
    sys.exit(0)
if argv[:2] == ["api", "graphql"]:
    number = next(a.split("=", 1)[1] for a in argv if a.startswith("number="))
    if any("isInMergeQueue" in a for a in argv):
        print(json.dumps({"data": {"repository": {"pullRequest": {"isInMergeQueue": number in state.get("queued", [])}}}}))
    else:
        print(json.dumps(state["graphql"][number]))
    sys.exit(0)
recorded = any("_attempt: " in c["body"] for comments in state["comments"].values() for c in comments)
if (argv[:2] == ["api", "-X"] and argv[2] in ("PUT", "POST") and "/requested_reviewers" not in argv[3]) or argv[:2] == ["run", "rerun"]:
    if not recorded:
        sys.exit("gh: a repair ran before its attempt line was written")
if argv[:2] == ["api", "-X"] and argv[2] == "PUT":
    if argv[3] in state.get("rate_limited_puts", []):
        sys.exit("gh: API rate limit exceeded (HTTP 429)")
    if argv[3] in state.get("update_fails", []):
        sys.exit("gh: Head branch was modified. Review and try the merge again. (HTTP 422)")
    print("{}")
    sys.exit(0)
if argv[:3] == ["api", "-X", "POST"]:
    print("{}")
    sys.exit(0)
if argv[:2] == ["run", "rerun"]:
    sys.exit(0)
if argv[0] == "api" and "/timeline" in argv[1]:
    print(json.dumps(state["timeline"][argv[1].split("/")[4]]))
    sys.exit(0)
if argv[0] == "api" and "/check-runs" in argv[1]:
    print(json.dumps({"check_runs": state["base_runs"].get(argv[1].split("/")[4], [])}))
    sys.exit(0)
if argv[0] == "api":
    repo = "/".join(argv[1].split("/")[1:3])
    if repo in state.get("rules_fails", []):
        sys.exit("gh: HTTP 403")
    print(json.dumps(state["rules"].get(repo, [])))
    sys.exit(0)
if argv[:2] == ["pr", "view"]:
    pulls = [p for repo_pulls in state["pulls"].values() for p in repo_pulls if p["url"] == argv[2]]
    view = {"isDraft": False, "mergeStateStatus": "CLEAN", "labels": [], **pulls[0]}
    print(json.dumps({k: view[k] for k in ("statusCheckRollup", "isDraft", "mergeStateStatus", "labels")}))
    sys.exit(0)
repo = argv[argv.index("-R") + 1]
if repo in state.get("gh_fails", []):
    sys.exit("gh: HTTP 502")
if repo in state.get("rate_limited", []):
    sys.exit("gh: API rate limit exceeded (HTTP 403)")
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
        (cli.parent / "outcomes.py").symlink_to(ROOT / "plugins" / "linear-agent-session" / "outcomes.py")
        for fake in (self.hermes, self.gh):
            os.chmod(fake, 0o755)
        self.state_path = self.dir / "state.json"
        self.board_db = self.dir / "default.db"
        with closing(sqlite3.connect(self.board_db)) as db:
            db.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, last_failure_error TEXT, consecutive_failures INTEGER DEFAULT 0)"
            )
            db.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER)")
            db.commit()
        self.state = {
            "settings": {"github": {"login": BOT_LOGIN}}, "tasks": [], "comments": {}, "pulls": {}, "graphql": {}, "rules": {},
            "board_db": str(self.board_db),
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
        proc = subprocess.run([sys.executable, str(SCRIPTS / "reopen-conflicts")], env=env, capture_output=True, text=True)
        self.state["comments"] = json.loads(self.state_path.read_text())["comments"]
        return proc

    def hermes_calls(self) -> list:
        log = Path(str(self.state_path) + ".hermes")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def writes(self) -> list:
        reads = (["config", "get"], ["kanban", "list"], ["kanban", "show"], ["kanban", "boards"])
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

    QUOTA_ERROR = "pid 1 exited rate-limited (quota wall) — requeued without counting a failure"

    def seed_failure(self, *runs: tuple[str, int]) -> None:
        with closing(sqlite3.connect(self.board_db)) as db:
            db.execute("INSERT INTO tasks VALUES ('t_1', ?, 2)", (self.QUOTA_ERROR,))
            db.executemany("INSERT INTO task_runs (task_id, outcome, ended_at) VALUES ('t_1', ?, ?)", runs)
            db.commit()

    def failure_row(self) -> tuple:
        with closing(sqlite3.connect(self.board_db)) as db:
            return db.execute("SELECT last_failure_error, consecutive_failures FROM tasks WHERE id = 't_1'").fetchone()

    def test_a_handback_clears_an_old_quota_error_after_a_review_run(self) -> None:
        self.seed_failure(("rate_limited", 100), ("review_requested", 200))
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        result = self.run_script()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.failure_row(), (None, 2))

    def test_a_handback_keeps_the_error_when_the_newest_ended_run_is_rate_limited(self) -> None:
        self.seed_failure(("review_requested", 100), ("rate_limited", 200))
        self.state["tasks"] = [card("t_1", "review", "abc-1")]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        result = self.run_script()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.failure_row(), (self.QUOTA_ERROR, 2))

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
            ["pr", "view", f"https://github.com/{REPO}/pull/7", "--json", VIEW_FIELDS],
            ["api", "graphql", "-f", f"query={REVIEWS_QUERY}", "-f", "owner=example-org", "-f", "name=example-repo", "-F", "number=7"],
            ["api", f"repos/{REPO}/rules/branches/main?per_page=100"],
            ["api", "-X", "POST", f"repos/{REPO}/pulls/7/requested_reviewers", "-f", "reviewers[]=rev"],
        ])
        self.assertEqual(self.cli_calls(), [[
            "activity", "--agent-session-id", SESSION, "--type", "response",
            "--body", (
                f"Changes are in, re-review: https://github.com/{REPO}/pull/7\n\n"
                f"Re-requested review from @rev: their changes-requested review is on an older head than the current head {HEAD}. "
                "Review the pull request on GitHub, then merge it or request changes."
            ),
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
        self.assertEqual(self.cli_calls()[0][6], (
            f"Changes are in, re-review: https://github.com/{REPO}/pull/7\n\n"
            f"Re-requested review from @a, @b: their changes-requested review is on an older head than the current head {HEAD}. "
            "Review the pull request on GitHub, then merge it or request changes."
        ))

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
            ["pr", "view", f"https://github.com/{REPO}/pull/7", "--json", VIEW_FIELDS], self.RULES_CALL,
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

        listed = f"- `ci`: {REPO}#7"
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

    INCOMPLETE_NOTE = "GitHub listed only the first 300 changed files, so more files may conflict."

    def compare_state(self, head_files: list, base_files: list) -> None:
        self.state["compare"] = {
            f"repos/{REPO}/compare/main...{HEAD}": {
                "merge_base_commit": {"sha": MERGE_BASE}, "files": [{"filename": name} for name in head_files],
            },
            f"repos/{REPO}/compare/{MERGE_BASE}...main": {"files": [{"filename": name} for name in base_files]},
        }

    def triage_case(self, *, session: str | None = SESSION, **pull_kwargs) -> subprocess.CompletedProcess:
        self.state["tasks"] = [card("t_1", "triage", "abc-1", session=session)]
        self.state["pulls"][REPO] = [pull(7, "abc-1", **pull_kwargs)]
        self.compare_state(["a.py", "b.py", "only_head.py"], ["b.py", "a.py", "only_base.py"])
        return self.run_script()

    def triage_calls(self) -> list:
        return [["api", f"repos/{REPO}/compare/main...{HEAD}?per_page=1"],
                ["api", f"repos/{REPO}/compare/{MERGE_BASE}...main?per_page=1"]]

    def test_a_conflicting_triage_card_tells_its_session_then_comments_and_never_moves(self) -> None:
        proc = self.triage_case()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, f"t_1 https://github.com/{REPO}/pull/7 notified\n", ""))
        self.assertEqual(self.cli_calls(), [[
            "activity", "--agent-session-id", SESSION, "--type", "elicitation",
            "--body", (
                f"Pull request {REPO}#7 conflicts with its base main. This card stays parked, and the worker merges "
                "main into the branch first once someone replies. Conflicting files:\n- `a.py`\n- `b.py`"
            ),
            "--external-url", f"Pull request=https://github.com/{REPO}/pull/7",
        ]])
        self.assertEqual(self.writes(), [[
            "kanban", "comment", "t_1",
            f"Pull request {REPO}#7 conflicts with main. The card stays parked. When a person replies and the card "
            f"resumes, the worker merges origin/main first.\n\nparked: notice only\nconflict_head: {HEAD}",
        ]])
        self.assertEqual(self.gh_calls()[1:], self.triage_calls())

    def test_a_triage_card_that_holds_the_head_is_left_alone(self) -> None:
        self.state["comments"] = {"t_1": [{"body": f"earlier\nparked: notice only\nconflict_head: {HEAD}"}]}

        proc = self.triage_case()

        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
        self.assertEqual((self.cli_calls(), self.writes()), ([], []))
        self.assertEqual(len(self.gh_calls()), 1)

    def test_a_triage_card_is_left_alone_unless_the_pull_request_conflicts(self) -> None:
        for mergeable in ("MERGEABLE", "UNKNOWN"):
            with self.subTest(mergeable=mergeable):
                proc = self.triage_case(mergeable=mergeable, reviewDecision="CHANGES_REQUESTED", checks=[check_run("lint")])

                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))
                self.assertEqual((self.cli_calls(), self.writes()), ([], []))
                self.assertEqual(len(self.gh_calls()), 1)
                for suffix in (".gh", ".cli", ".hermes"):
                    Path(str(self.state_path) + suffix).unlink(missing_ok=True)

    def test_a_triage_card_whose_pull_request_is_from_a_person_or_has_no_branch_is_left_alone(self) -> None:
        proc = self.triage_case(author="person")
        self.assertEqual((proc.stdout, self.writes(), self.cli_calls()), ("", [], []))

        self.state["tasks"] = [{"id": "t_1", "status": "triage", "branch_name": None, "body": f"repo: {REPO}\n"}]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        proc = self.run_script()
        self.assertEqual((proc.stdout, self.writes(), self.cli_calls()), ("", [], []))

    def test_a_triage_card_without_a_session_gets_the_comment_and_fails_while_others_still_run(self) -> None:
        self.state["tasks"] = [card("t_1", "triage", "abc-1"), card("t_2", "review", "abc-2")]
        self.state["pulls"][REPO] = [pull(7, "abc-1"), pull(8, "abc-2")]
        self.compare_state(["a.py"], ["a.py"])

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr, "reopen-conflicts: t_1: the card has no linear_agent_session_id line\n")
        self.assertEqual(proc.stdout, f"t_2 https://github.com/{REPO}/pull/8\n")
        self.assertEqual(self.cli_calls(), [])
        self.assertEqual([call[:3] for call in self.writes()], [
            ["kanban", "comment", "t_1"], ["kanban", "comment", "t_2"], ["kanban", "reopen-review", "t_2"],
        ])

    def test_a_truncated_compare_says_the_file_list_may_be_incomplete(self) -> None:
        self.state["tasks"] = [card("t_1", "triage", "abc-1", session=SESSION)]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        self.compare_state(["a.py"], [f"f{n}.py" for n in range(299)] + ["a.py"])

        proc = self.run_script()

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(self.cli_calls()[0][6].endswith(f"Conflicting files:\n- `a.py`\n{self.INCOMPLETE_NOTE}"))

    def test_an_empty_intersection_says_the_file_list_is_unavailable(self) -> None:
        self.state["tasks"] = [card("t_1", "triage", "abc-1", session=SESSION)]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        self.compare_state(["a.py"], ["b.py"])

        self.run_script()

        self.assertTrue(self.cli_calls()[0][6].endswith("Conflicting files:\nThe list of conflicting files is unavailable."))

    def test_a_compare_github_refuses_still_tells_the_session_the_list_is_unavailable(self) -> None:
        self.state["tasks"] = [card("t_1", "triage", "abc-1", session=SESSION)]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        self.state["compare"] = {}

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_1 https://github.com/{REPO}/pull/7 notified\n"))
        self.assertTrue(self.cli_calls()[0][6].endswith("Conflicting files:\nThe list of conflicting files is unavailable."))
        self.assertEqual([call[:3] for call in self.writes()], [["kanban", "comment", "t_1"]])

    def test_a_parked_notice_does_not_stand_for_a_handback_when_the_card_leaves_triage(self) -> None:
        self.state["tasks"] = [card("t_1", "blocked", "abc-1"), card("t_2", "review", "abc-2")]
        self.state["pulls"][REPO] = [pull(7, "abc-1"), pull(8, "abc-2")]
        notice = {"body": f"parked\n\nparked: notice only\nconflict_head: {HEAD}"}
        self.state["comments"] = {"t_1": [notice], "t_2": [notice]}

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stdout), (0, f"t_1 https://github.com/{REPO}/pull/7\nt_2 https://github.com/{REPO}/pull/8\n"))
        self.assertEqual(self.writes(), [
            ["kanban", "unblock", "t_1"],
            ["kanban", "request-review", "t_1"],
            ["kanban", "comment", "t_1", self.comment(7)],
            ["kanban", "reopen-review", "t_1", "--reason", "merge conflict with the base branch; see the card comments"],
            ["kanban", "comment", "t_2", self.comment(8)],
            ["kanban", "reopen-review", "t_2", "--reason", "merge conflict with the base branch; see the card comments"],
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


    PR = f"{REPO}#7"
    QUEUE = {"labels": ["queue"], "checks": ["Queue"], "requeue": {"command": "@queue"}}
    ESCALATED = f"earlier escalation\n\ndequeued_base_failure_head: {HEAD}"
    URL = f"https://github.com/{REPO}/pull/7"

    def clear_logs(self) -> None:
        for suffix in (".hermes", ".gh", ".cli"):
            Path(str(self.state_path) + suffix).unlink(missing_ok=True)

    def sweep_again(self) -> subprocess.CompletedProcess:
        self.clear_logs()
        return self.run_script()

    def gh_writes(self) -> list:
        return [call for call in self.gh_calls() if call[:2] in (["api", "-X"], ["run", "rerun"])]

    def assert_quiet(self) -> None:
        proc = self.sweep_again()
        self.assertEqual((proc.returncode, proc.stderr, self.writes(), self.gh_writes(), self.cli_calls()), (0, "", [], [], []))

    def stuck_case(
        self, *, checks: list | None = None, entry: dict | None = QUEUE, rules: list | None = None, base: list | None = None,
        queued: bool = False, timeline: list | None = None, comments: list | None = None, status: str = "review",
        **extra,
    ) -> None:
        self.state["tasks"] = [card("t_1", status, "abc-1", session=SESSION)]
        self.state["pulls"][REPO] = [pull(
            7, "abc-1", mergeable="MERGEABLE", reviewDecision="APPROVED", checks=checks or [check("ci")], **extra,
        )]
        self.state["rules"][REPO] = rules or required("ci", "Queue")
        self.state["config"] = {QUEUE_KEY: {REPO: entry} if entry is not None else {}}
        self.state["base_runs"] = {"main": base or []}
        self.state["timeline"] = {"7": timeline or []}
        self.state["queued"] = ["7"] if queued else []
        self.state["comments"] = {"t_1": [{"body": body} for body in comments or []]}

    def dropped(self) -> list:
        return [check("ci"), check_run("Queue", "FAILURE")]

    def base_run(self, conclusion: str | None, name: str = "ci") -> list:
        return [{
            "name": name, "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
            "started_at": "2026-01-01T00:00:00Z",
        }]

    def by_person(self) -> list:
        return [{"event": "commented", "actor": {"login": "alice"}, "body": "@queue"}]

    def activity(self, body: str) -> list:
        return ["activity", "--agent-session-id", SESSION, "--type", "elicitation", "--body", body, "--external-url", f"Pull request={self.URL}"]

    def escalation(self, body: str, marker: str) -> tuple:
        return ["kanban", "comment", "t_1", f"{body}\n\n{marker}: {HEAD}"], self.activity(body)

    BASE_FAILED = (
        f"The merge queue dropped {PR} because a required check failed on the head of main, so the pull request is not "
        "at fault. A person fixes main. If a person had queued it, the sweep queues it again once the required checks "
        "on the head of main pass."
    )

    def test_a_dequeued_pull_request_with_a_failing_base_escalates_and_never_reopens_the_card(self) -> None:
        self.stuck_case(checks=self.dropped(), base=self.base_run("failure"), timeline=self.by_person())

        proc = self.run_script()

        comment, activity = self.escalation(self.BASE_FAILED, "dequeued_base_failure_head")
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, f"t_1 {self.URL} escalated dequeued_base_failure\n", ""))
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assertFalse(self.BASE_FAILED.startswith(("Waiting for", "Linear reply:")))
        self.assert_quiet()

    def test_a_dequeued_pull_request_is_queued_again_after_its_base_passes(self) -> None:
        self.stuck_case(checks=self.dropped(), base=self.base_run("failure"), timeline=self.by_person())
        self.run_script()
        self.state["base_runs"] = {"main": self.base_run("success")}

        proc = self.sweep_again()

        attempt = (
            f"The required checks on the head of main pass again, so the sweep queues {self.PR} again.\n\nrequeue_attempt: {HEAD} 1"
        )
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.writes(), [["kanban", "comment", "t_1", attempt]])
        self.assertEqual(self.gh_writes(), [["api", "-X", "POST", f"repos/{REPO}/issues/7/comments", "-f", "body=@queue"]])
        self.assertEqual(self.cli_calls(), [])
        self.state["queued"] = ["7"]
        self.state["pulls"][REPO][0]["statusCheckRollup"] = [check("ci"), check_run("Queue", None)]
        self.assert_quiet()

    def test_a_label_requeue_adds_the_label_only_when_it_is_missing(self) -> None:
        entry = {**self.QUEUE, "requeue": {"label": "queue"}}
        labeled = [{"event": "labeled", "actor": {"login": "alice"}, "label": {"name": "queue"}}]
        self.stuck_case(checks=self.dropped(), entry=entry, base=self.base_run("success"), timeline=labeled, comments=[self.ESCALATED])

        self.run_script()

        self.assertEqual(self.gh_writes(), [["api", "-X", "POST", f"repos/{REPO}/issues/7/labels", "-f", "labels[]=queue"]])

        self.stuck_case(
            checks=self.dropped(), entry=entry, base=self.base_run("success"), timeline=labeled, comments=[self.ESCALATED],
            labels=[{"name": "queue"}],
        )
        self.sweep_again()
        stuck = (
            f"The label `queue` is still on {self.PR}, and removing and adding it again does not queue it. The required "
            f"checks on the head of main pass. A person queues {self.PR} again with the queue tool."
        )
        self.assertEqual(self.writes(), [
            ["kanban", "comment", "t_1", f"{stuck}\n\ndequeued_base_failure_requeue_head: {HEAD}"],
        ])
        self.assertEqual(self.gh_writes(), [])

    def test_two_requeues_on_one_head_end_in_one_escalation_that_lists_both(self) -> None:
        attempts = [f"The sweep queues it.\n\nrequeue_attempt: {HEAD} {n}" for n in (1, 2)]
        self.stuck_case(checks=self.dropped(), base=self.base_run("success"), timeline=self.by_person(), comments=[self.ESCALATED, *attempts])

        proc = self.run_script()

        body = (
            f"The sweep requeued {self.PR} 2 times on head {HEAD} and the merge queue dropped it each time. A person reads "
            "the failing queue run and queues the pull request again, or fixes what fails.\n"
            f"- attempt 1 on head {HEAD}\n- attempt 2 on head {HEAD}"
        )
        comment, activity = self.escalation(body, "dequeued_base_failure_limit_head")
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assert_quiet()

    def test_a_dequeue_with_a_passing_base_hands_the_card_back_with_the_queue_check_named(self) -> None:
        self.stuck_case(checks=self.dropped(), base=self.base_run("success"))

        proc = self.run_script()

        text = (
            f"The merge queue dropped pull request {self.PR} at head {HEAD}. The required checks on the head of main pass, "
            "so the failure was in the queue's own run of the pull request. Read the queue run of `Queue`, fix the cause, "
            "and publish the fix with push-signed. When you cannot fix it with confidence, stop and make the first line of "
            f"your final message `Needs input:` followed by the reason.\n\ndequeued_own_failure_head: {HEAD}"
        )
        reopen = ["kanban", "reopen-review", "t_1", "--reason", "the merge queue dropped the pull request; see the card comments"]
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.writes(), [["kanban", "comment", "t_1", text], reopen])
        self.assertEqual(self.gh_writes(), [])
        self.sweep_again()
        self.assertEqual(self.writes(), [reopen])

    def test_a_dequeue_with_no_base_run_says_the_base_run_was_missing(self) -> None:
        self.stuck_case(checks=self.dropped(), base=[])

        self.run_script()

        text = self.writes()[0][3]
        self.assertIn(
            "The base branch main has no run of its required checks at its head, so the base run was missing and the sweep "
            "cannot tell whether main is healthy.", text,
        )
        self.assertTrue(text.endswith(f"dequeued_own_failure_head: {HEAD}"))
        self.assertEqual(self.gh_writes(), [])

    def test_a_pull_request_only_the_app_queued_is_not_queued_again(self) -> None:
        app_only = [{"event": "commented", "actor": {"login": BOT_LOGIN}, "body": "@queue"}]
        self.stuck_case(checks=self.dropped(), base=self.base_run("success"), timeline=app_only, comments=[self.ESCALATED])

        self.run_script()

        body = (
            f"No person has {self.PR} in the queue, so the sweep does not queue it. The required checks on the head of main "
            f"pass. A person queues {self.PR}."
        )
        comment, activity = self.escalation(body, "dequeued_base_failure_requeue_head")
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assert_quiet()

    def test_a_person_who_removed_the_label_blocks_a_requeue(self) -> None:
        entry = {**self.QUEUE, "requeue": {"label": "queue"}}
        events = [
            {"event": "labeled", "actor": {"login": "alice"}, "label": {"name": "queue"}},
            {"event": "unlabeled", "actor": {"login": "alice"}, "label": {"name": "queue"}},
        ]
        self.stuck_case(checks=self.dropped(), entry=entry, base=self.base_run("success"), timeline=events, comments=[self.ESCALATED])

        self.run_script()

        self.assertEqual(self.gh_writes(), [])
        self.assertTrue(self.writes()[0][3].startswith(f"No person has {self.PR} in the queue"))

    def test_a_requeue_of_none_escalates_with_the_command_grant(self) -> None:
        self.stuck_case(
            checks=self.dropped(), entry={**self.QUEUE, "requeue": "none"}, base=self.base_run("success"), comments=[self.ESCALATED],
        )

        self.run_script()

        body = (
            f"The sweep cannot queue {self.PR} again, because `merge_queue.{REPO}.requeue` is not set to a label or a command. "
            f"The required checks on the head of main pass. A person queues {self.PR} again. To let the sweep do it, grant "
            "the App the queue tool's command (for Mergify, PAT-6029) and set `requeue`."
        )
        comment, activity = self.escalation(body, "dequeued_base_failure_requeue_head")
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assert_quiet()

    def test_a_malformed_requeue_is_treated_as_none(self) -> None:
        for bad in ({"label": ""}, {"command": 5}, {"label": "queue", "command": "@queue"}, ["queue"]):
            with self.subTest(requeue=bad):
                self.stuck_case(
                    checks=self.dropped(), entry={**self.QUEUE, "requeue": bad}, base=self.base_run("success"),
                    timeline=self.by_person(), comments=[self.ESCALATED],
                )
                self.sweep_again()
                self.assertEqual(self.gh_writes(), [])
                self.assertIn("PAT-6029", self.writes()[0][3])

    def last_push_rules(self) -> list:
        return [{"type": "pull_request", "parameters": {"require_last_push_approval": True}}, *required("ci")[1:]]

    def test_a_stale_approval_escalates_before_any_branch_update(self) -> None:
        self.stuck_case(entry=None, rules=self.last_push_rules(), mergeStateStatus="BEHIND")
        self.state["graphql"]["7"] = graphql([review("rev", "APPROVED", NEW_HEAD, "2026-01-01T00:00:00Z")])

        proc = self.run_script()

        body = (
            f"{self.PR} has no approval on its current head {HEAD}, and the base branch rules require approval of the most "
            "recent push. A reviewer approves the pull request again on GitHub."
        )
        comment, activity = self.escalation(body, "approval_stale_head")
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assert_quiet()

    def test_a_current_approval_that_an_update_would_void_blocks_the_update(self) -> None:
        self.stuck_case(entry=None, rules=self.last_push_rules(), mergeStateStatus="BEHIND")
        self.state["graphql"]["7"] = graphql([review("rev", "APPROVED", HEAD, "2026-01-01T00:00:00Z")])

        self.run_script()

        self.assertEqual((self.writes(), self.gh_writes(), self.cli_calls()), ([], [], []))

    def test_a_behind_branch_is_updated_once_with_the_expected_head_until_the_limit(self) -> None:
        self.stuck_case(entry=None, mergeStateStatus="BEHIND")

        proc = self.run_script()

        attempt = f"{self.PR} is behind main, so the sweep updates the branch.\n\nbranch_update_attempt: 7 {HEAD}"
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.writes(), [["kanban", "comment", "t_1", attempt]])
        self.assertEqual(self.gh_writes(), [["api", "-X", "PUT", f"repos/{REPO}/pulls/7/update-branch", "-f", f"expected_head_sha={HEAD}"]])
        self.assertEqual(self.cli_calls(), [])

        older = [f"x\n\nbranch_update_attempt: 7 {sha * 40}" for sha in "de"]
        self.stuck_case(entry=None, mergeStateStatus="BEHIND", comments=older)
        self.sweep_again()
        body = (
            f"The sweep updated {self.PR} from main 2 times and it is behind again. A person updates the branch, or turns "
            f"on branch updates in the merge queue and sets `updates_branch` for the repository.\n"
            f"- updated from head {'d' * 40}\n- updated from head {'e' * 40}"
        )
        comment, activity = self.escalation(body, "behind_base_limit_head")
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assert_quiet()

    def test_a_handback_since_the_last_update_resets_the_branch_update_count(self) -> None:
        old = f"x\n\nbranch_update_attempt: 7 {'d' * 40}"
        reset = f"handed back\n\nfailed_check_head: {'e' * 40}"
        self.stuck_case(entry=None, mergeStateStatus="BEHIND", comments=[old, old, reset, old])

        self.run_script()

        self.assertEqual(len(self.gh_writes()), 1)

    def test_a_refused_branch_update_escalates_and_is_not_retried_on_that_head(self) -> None:
        self.stuck_case(entry=None, mergeStateStatus="BEHIND")
        self.state["update_fails"] = [f"repos/{REPO}/pulls/7/update-branch"]

        self.run_script()

        body = (
            f"GitHub refused to update {self.PR} from main: gh: Head branch was modified. Review and try the merge again. "
            "(HTTP 422). A person updates the branch on GitHub or grants the App what the refusal names."
        )
        comment, activity = self.escalation(body, "behind_base_head")
        self.assertEqual(self.writes()[1:], [comment])
        self.assertEqual(self.cli_calls(), [activity])
        self.assert_quiet()

    def test_a_branch_that_the_queue_updates_is_left_alone(self) -> None:
        self.stuck_case(entry={**self.QUEUE, "updates_branch": True}, mergeStateStatus="BEHIND")

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stderr, self.writes(), self.gh_writes(), self.cli_calls()), (0, "", [], [], []))

    def test_a_flake_signal_is_re_run_once_per_head_and_then_escalated(self) -> None:
        flaky = [check("ci"), check_run("e2e", "FAILURE", url=f"https://github.com/{REPO}/actions/runs/555")]
        self.stuck_case(checks=flaky, entry={"flake_signals": ["e2e"]}, rules=required("ci", "e2e"))

        proc = self.run_script()

        attempt = f"`e2e` failed on {self.PR} and is a declared flake signal, so the sweep re-runs the failed jobs.\n\nrerun_attempt: {HEAD} 555"
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(self.writes(), [["kanban", "comment", "t_1", attempt]])
        self.assertEqual(self.gh_writes(), [["run", "rerun", "555", "--failed", "-R", REPO]])

        self.sweep_again()
        body = (
            f"`e2e` failed on {self.PR} at head {HEAD} again after the sweep re-ran it 1 time(s). A person reads the failing "
            "job and re-runs it or fixes the cause.\n- re-ran run 555"
        )
        comment, activity = self.escalation(body, "flaky_check_limit_head")
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assert_quiet()

    def test_a_failed_check_that_is_not_a_flake_signal_is_handed_back_as_today(self) -> None:
        failing = [check("ci"), check_run("e2e", "FAILURE", url=f"https://github.com/{REPO}/actions/runs/555")]
        self.stuck_case(checks=failing, entry={"flake_signals": ["lint"]}, rules=required("ci", "e2e"))

        self.run_script()

        self.assertEqual([call[1] for call in self.writes()], ["comment", "reopen-review"])
        self.assertTrue(self.writes()[0][3].endswith(f"failed_check_head: {HEAD}"))
        self.assertEqual(self.gh_writes(), [])

    def test_an_approved_green_pull_request_nobody_queued_escalates_to_the_person_who_queues(self) -> None:
        rules = [*required("ci"), {"type": "update", "parameters": {}}]
        self.stuck_case(rules=rules, mergeStateStatus="BLOCKED")

        proc = self.run_script()

        body = (
            f"{self.PR} is approved and its required checks pass, and only the merge queue can merge it. "
            "A person adds it to the merge queue."
        )
        comment, activity = self.escalation(body, "awaiting_queue_entry_head")
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual((self.writes(), self.cli_calls(), self.gh_writes()), ([comment], [activity], []))
        self.assert_quiet()

    def test_a_draft_gets_no_write_and_no_comment(self) -> None:
        self.stuck_case(checks=self.dropped(), base=self.base_run("failure"), isDraft=True, mergeStateStatus="BEHIND")

        proc = self.run_script()

        self.assertEqual((proc.returncode, proc.stderr, self.writes(), self.gh_writes(), self.cli_calls()), (0, "", [], [], []))

    def test_a_blocked_or_triage_card_gets_nothing_new_for_the_new_reasons(self) -> None:
        cases = {
            "behind": dict(entry=None, mergeStateStatus="BEHIND"),
            "dequeued": dict(checks=self.dropped(), base=self.base_run("failure")),
            "flaky": dict(
                checks=[check("ci"), check_run("e2e", "FAILURE", url=f"https://github.com/{REPO}/actions/runs/555")],
                entry={"flake_signals": ["e2e"]}, rules=required("ci", "e2e"),
            ),
            "awaiting": dict(rules=[*required("ci"), {"type": "update", "parameters": {}}], mergeStateStatus="BLOCKED"),
        }
        for status in ("blocked", "triage"):
            for name, case in cases.items():
                with self.subTest(status=status, case=name):
                    self.stuck_case(status=status, **case)
                    proc = self.sweep_again()
                    self.assertEqual(
                        (proc.returncode, proc.stderr, self.writes(), self.gh_writes(), self.cli_calls()), (0, "", [], [], []),
                    )

    def test_a_pending_queue_check_or_a_pending_base_run_settles_nothing(self) -> None:
        pending = [
            ("queue check", dict(checks=[check("ci"), check_run("Queue", None)], base=self.base_run("failure"))),
            ("base run", dict(checks=self.dropped(), base=self.base_run(None))),
            ("mergeability", dict(checks=[check("ci")], mergeStateStatus="UNKNOWN")),
        ]
        for name, case in pending:
            with self.subTest(pending=name):
                self.stuck_case(**case)
                proc = self.sweep_again()
                self.assertEqual(
                    (proc.returncode, proc.stderr, self.writes(), self.gh_writes(), self.cli_calls()), (0, "", [], [], []),
                )

    def test_a_pull_request_that_is_in_the_merge_queue_is_not_dequeued(self) -> None:
        self.stuck_case(checks=self.dropped(), base=self.base_run("failure"), queued=True)

        proc = self.run_script()

        self.assertEqual((proc.returncode, self.writes(), self.cli_calls()), (0, [], []))

    def test_a_failed_repair_for_one_card_does_not_stop_the_next_card(self) -> None:
        self.stuck_case(entry=None, mergeStateStatus="BEHIND")
        self.state["tasks"] = [card("t_1", "review", "abc-1", session=SESSION), card("t_2", "review", "abc-2", session=SESSION)]
        self.state["pulls"][REPO].append(pull(8, "abc-2", mergeable="MERGEABLE", checks=[check("ci")], mergeStateStatus="BEHIND"))
        self.state["comment_fails"] = ["t_1"]

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertIn("t_1: hermes kanban comment failed", proc.stderr)
        self.assertEqual(self.gh_writes(), [["api", "-X", "PUT", f"repos/{REPO}/pulls/8/update-branch", "-f", f"expected_head_sha={HEAD}"]])

    def test_a_rate_limit_ends_that_repository_and_the_next_repository_still_runs(self) -> None:
        self.stuck_case(entry=None, mergeStateStatus="BEHIND")
        self.state["tasks"] = [
            card("t_1", "review", "abc-1", session=SESSION), card("t_2", "review", "abc-2", session=SESSION),
            card("t_3", "review", "abc-3", repo=OTHER_REPO),
        ]
        self.state["pulls"][REPO].append(pull(8, "abc-2", mergeable="MERGEABLE", checks=[check("ci")], mergeStateStatus="BEHIND"))
        self.state["pulls"][OTHER_REPO] = [pull(3, "abc-3").copy()]
        self.state["pulls"][OTHER_REPO][0]["url"] = f"https://github.com/{OTHER_REPO}/pull/3"
        self.state["rate_limited_puts"] = [f"repos/{REPO}/pulls/7/update-branch"]

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertIn("HTTP 429", proc.stderr)
        self.assertEqual([call[3] for call in self.gh_writes()], [f"repos/{REPO}/pulls/7/update-branch"])
        self.assertEqual(
            [call[2] for call in self.writes() if call[1] == "reopen-review"], ["t_3"],
        )

    def test_a_rate_limit_on_the_pull_request_list_leaves_the_other_repository_to_run(self) -> None:
        self.state["tasks"] = [card("t_1", "review", "abc-1"), card("t_2", "review", "abc-2", repo=OTHER_REPO)]
        self.state["pulls"][REPO] = [pull(7, "abc-1")]
        self.state["pulls"][OTHER_REPO] = [{**pull(8, "abc-2"), "url": f"https://github.com/{OTHER_REPO}/pull/8"}]
        self.state["rate_limited"] = [REPO]

        proc = self.run_script()

        self.assertEqual(proc.returncode, 1)
        self.assertEqual([call[2] for call in self.writes() if call[1] == "reopen-review"], ["t_2"])

    def test_stuck_limits_come_from_the_settings(self) -> None:
        flaky = [check("ci"), check_run("e2e", "FAILURE", url=f"https://github.com/{REPO}/actions/runs/555")]
        self.stuck_case(checks=flaky, entry={"flake_signals": ["e2e"]}, rules=required("ci", "e2e"), comments=[f"x\n\nrerun_attempt: {HEAD} 554"])
        self.state["settings"]["stuck_limits"] = {"reruns": 2}

        self.run_script()

        self.assertEqual(len(self.gh_writes()), 1)

        self.state["settings"]["stuck_limits"] = {"reruns": 0}
        proc = self.sweep_again()
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertIn("stuck_limits", proc.stderr)


if __name__ == "__main__":
    unittest.main()
