from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.support import REPO, SCRIPTS, FakeBroker, git, with_interpreter
from tests.sentry_fakes import APP_USER, PERSON, STATES, FakeLinear, FakeSentry, linear_issue, sentry_issue

sys.path.insert(0, str(SCRIPTS))
import sentry_sync  # noqa: E402
from sentry_sync import (  # noqa: E402
    AddMembers, CloseInactive, Create, Hold, LinkKey, LinkRefused, MarkDuplicate, OpenRegression, Repair, SetPriority,
    TakeOver, Verify,
)

ORG = "example-org"
NOW = "2026-11-03T12:00:00+00:00"
RULES = {
    "projects": {"web": "web", "cms": "cms"},
    "exclude": [{"id": "browser-extension", "why": "Third-party script", "match": {"messageIncludes": ["window.ethereum"]}}],
    "merge": [{"id": "hydration", "why": "One cause splits by route", "match": {"exceptionType": "RecoverableHydrationError"},
               "key": "web:hydration"}],
    "impact": {"windowHours": 24, "minUsers": 3, "minEvents": 10},
    "priority": {"urgentUsers": 200, "highUsers": 25, "mediumUsers": 5},
    "delegation": "off", "maxOpen": 8, "staleDays": 14, "verifyDays": 7, "suppressDays": 90,
}
SOURCE_FILES = ("packages/web/src/routes/releases.tsx", "packages/web/src/app/search.tsx", "packages/cms/src/auth/session.ts")
RELEASES_FILE = "../../../src/routes/releases.tsx"
RELEASES_KEY = "web:TypeError:packages/web/src/routes/releases.tsx:ReleaseList"
SEARCH_KEY = "web:TypeError:packages/web/src/app/search.tsx:buildManifest"
SENTINELS = (
    "sentinel@example.invalid", "https://sentinel.example.invalid/x", "918273645",
    "0b1e5c3a-9f2d-4c11-8e7a-5d6f7a8b9c0d", "sentinel quoted",
)
FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:3] == ["config", "get"]:
    print(open(os.environ["FAKE_SETTINGS"]).read())
else:
    sys.exit("hermes: refused " + repr(sys.argv[1:]))
"""
WRAPPER = """
import importlib.machinery, importlib.util, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
loader = importlib.machinery.SourceFileLoader("sentry_linear_sync", sys.argv[1] + "/sentry-linear-sync")
module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(module)
sys.exit(module.run(sys.argv[5:], linear_api_url=sys.argv[2], sentry_base_url=sys.argv[3], now=datetime.fromisoformat(sys.argv[4])))
"""


def url(key: str) -> str:
    return sentry_sync.group_search_url(sentry_sync.GroupKey(key), ORG)


def releases_issue(short_id: str = "WEB-1", number: int = 101, **fields) -> dict:
    return sentry_issue(short_id, number=number, filename=RELEASES_FILE, function="ReleaseList", **fields)


def tracked(identifier: str, key: str, members=(("WEB-1", "101"),), users: int = 5, **fields) -> dict:
    links = "\n".join(f"- [{s}](https://{ORG}.sentry.io/issues/{n}/)" for s, n in members)
    description = f"## Sentry issues in this error group\n\n{links}\n\nSentry error group: {key} usersWhenCreated={users}\n"
    return linear_issue(identifier, description=description, attachments=[{"url": url(key), "sourceType": "api"}], **fields)


def legacy(identifier: str, sentry_id: str, created_at: str, **fields) -> dict:
    link = {"url": f"https://sentry.io/organizations/{ORG}/issues/{sentry_id}/events/abc123/", "sourceType": "sentry"}
    return linear_issue(identifier, creator=None, created_at=created_at, attachments=[link], **fields)


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "hermes" / "profiles" / "claude-worker"
        (self.home / "plugins").mkdir(parents=True)
        (self.home / "plugins" / "linear-agent-session").symlink_to(REPO / "plugins" / "linear-agent-session")
        self.hermes = self.tmp / "fake-hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        for path in SOURCE_FILES:
            (self.origin / path).parent.mkdir(parents=True, exist_ok=True)
            (self.origin / path).write_text("export {}\n")
        self.commit_rules(RULES)
        self.checkout = self.tmp / "checkout"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.checkout))
        self.settings = {
            "claude_session": {"mcp_servers": {"sentry": {"url": "https://mcp.example.invalid/mcp", "token_env": "SENTRY_TOKEN"}}},
            "sentry_sync": {
                "organization": ORG, "team_key": "ABC", "repository": "example-org/example-repo",
                "checkout_path": str(self.checkout), "rules_path": "config/sentry-error-groups.json",
                "sentry_label": "Sentry", "theme_label": "Performance & stability", "mcp_server": "sentry",
            },
        }
        self.broker = FakeBroker(
            self.tmp / "hermes" / "factory-uploads.sock",
            linear_token=[{"ok": True, "token": "linear-token", "expires_at": 4102444800}],
            mcp_token=[{"ok": True, "token": "sentry-token", "expires_at": None}],
        )
        self.addCleanup(self.broker.close)
        self.linear = self.sentry = None

    def commit_rules(self, rules) -> None:
        path = self.origin / "config" / "sentry-error-groups.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(rules if isinstance(rules, str) else json.dumps(rules))
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-q", "-m", "rules")

    def serve(self, linear=(), sentry=None, **linear_options) -> None:
        self.linear = FakeLinear(linear, **linear_options)
        self.addCleanup(self.linear.close)
        self.sentry = sentry or FakeSentry()
        self.addCleanup(self.sentry.close)

    def run_sync(self, *args: str, settings=None, now: str = NOW) -> subprocess.CompletedProcess:
        settings_path = self.tmp / "settings.json"
        settings_path.write_text(json.dumps(self.settings if settings is None else settings))
        env = {**os.environ, "HERMES_HOME": str(self.home), "HERMES_BIN": str(self.hermes), "FAKE_SETTINGS": str(settings_path)}
        for name in ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET"):
            env.pop(name, None)
        linear_url = f"{self.linear.url}/graphql" if self.linear else "http://127.0.0.1:9/graphql"
        sentry_url = self.sentry.url if self.sentry else "http://127.0.0.1:9"
        return subprocess.run(
            [sys.executable, "-c", WRAPPER, str(SCRIPTS), linear_url, sentry_url, now, *args],
            env=env, capture_output=True, text=True,
        )

    def writes(self, operation: str | None = None) -> list:
        return [v for op, v in self.linear.writes if operation in (None, op)]

    def lines(self, proc) -> list:
        return [line for line in proc.stdout.splitlines() if not line.startswith(("excluded", "no key", "below", "suppressed", "legacy"))]


class SettingsAndRulesTest(SyncTest):
    def test_bad_settings_write_nothing_and_exit_2_naming_the_field(self) -> None:
        self.serve()
        for change, message in (
            ({"team_key": "abc"}, "sentry_sync.team_key 'abc' must be a Linear team key such as ABC"),
            ({"rules_path": "../x.json"}, "sentry_sync.rules_path '../x.json' must be a path relative to the checkout, without .."),
            ({"mcp_server": "nope"}, "sentry_sync.mcp_server 'nope' is not a claude_session.mcp_servers entry"),
            ({"organization": None}, "sentry_sync.organization None must be a Sentry organization slug"),
        ):
            with self.subTest(message=message):
                settings = {**self.settings, "sentry_sync": {**self.settings["sentry_sync"], **change}}
                proc = self.run_sync(settings=settings)
                self.assertEqual((proc.returncode, proc.stdout), (2, ""))
                self.assertEqual(proc.stderr, f"sentry-linear-sync: {message}\n")
        proc = self.run_sync(settings={"claude_session": {}})
        self.assertEqual(proc.stderr, "sentry-linear-sync: sentry_sync is missing from the factory settings; it must be an object\n")
        self.assertEqual((self.linear.writes, self.sentry.requests), ([], []))

    def test_a_missing_or_malformed_rules_file_writes_nothing_and_exits_2(self) -> None:
        self.serve()
        for rules, message in (
            ("{not json", "config/sentry-error-groups.json at origin/main is not JSON"),
            ({**RULES, "maxOpen": 0}, "rules.maxOpen 0 must be an integer of at least 1"),
            ({**RULES, "merge": [{**RULES["merge"][0], "key": "ios:x"}]}, "rules.merge[0].key 'ios:x' must be <package>:<name>"),
            ({**RULES, "exclude": [RULES["exclude"][0], RULES["exclude"][0]]}, "rules.exclude[1].id 'browser-extension' is used by another rule"),
        ):
            with self.subTest(message=message):
                self.commit_rules(rules)
                proc = self.run_sync()
                self.assertEqual((proc.returncode, proc.stdout), (2, ""))
                self.assertIn(message, proc.stderr)
        git(self.origin, "rm", "-q", "config/sentry-error-groups.json")
        git(self.origin, "commit", "-q", "-m", "drop")
        proc = self.run_sync()
        self.assertEqual(proc.stderr, "sentry-linear-sync: sentry_sync.rules_path 'config/sentry-error-groups.json' does not exist at origin/main\n")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((self.linear.writes, self.sentry.requests), ([], []))

    def test_the_rules_file_is_read_at_the_fetched_default_branch_not_the_working_tree(self) -> None:
        (self.checkout / "config" / "sentry-error-groups.json").write_text("{broken")
        self.serve([], FakeSentry(issues=[releases_issue()]))
        proc = self.run_sync("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lines(proc), [f"created     new {RELEASES_KEY}  (Medium)"])

    def test_projects_that_differ_from_the_rules_write_nothing_and_exit_2_naming_both_sets(self) -> None:
        self.serve([tracked("ABC-1", RELEASES_KEY)], FakeSentry(projects=("web", "stores")))
        proc = self.run_sync()
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))
        self.assertEqual(proc.stderr, "sentry-linear-sync: Sentry returns projects stores, web, but rules.projects maps cms, web\n")
        self.assertEqual(self.linear.writes, [])


class ReadTest(SyncTest):
    def test_a_failed_second_sentry_page_writes_nothing_not_even_a_repair(self) -> None:
        unattached = tracked("ABC-1", SEARCH_KEY)
        unattached["attachments"] = []
        issues = [releases_issue("WEB-1", 101), releases_issue("WEB-2", 102), releases_issue("WEB-3", 103)]
        self.serve([unattached], FakeSentry(issues=issues, page_size=2, fail={("issues/", 2): 500}))
        proc = self.run_sync()
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertIn("read failed, so nothing was written: Sentry returned HTTP 500", proc.stderr)
        self.assertEqual(self.linear.writes, [])

    def test_a_failed_linear_read_writes_nothing(self) -> None:
        self.serve([], FakeSentry(issues=[releases_issue()]), fail={"LabeledIssues"})
        proc = self.run_sync()
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertEqual(self.linear.writes, [])
        self.assertEqual(self.sentry.requests, [])

    def test_every_page_of_sentry_issues_is_read(self) -> None:
        issues = [
            releases_issue("WEB-1", 101),
            sentry_issue("WEB-2", number=102, filename="../src/app/search.tsx", function="buildManifest"),
            sentry_issue("CMS-3", number=103, project="cms", filename="@x/cms/./src/auth/session.ts", function="read"),
        ]
        self.serve([], FakeSentry(issues=issues, page_size=1))
        proc = self.run_sync("--dry-run")
        self.assertEqual(self.lines(proc), [
            "created     new cms:TypeError:packages/cms/src/auth/session.ts:read  (Medium)",
            f"created     new {SEARCH_KEY}  (Medium)",
            f"created     new {RELEASES_KEY}  (Medium)",
        ])
        self.assertEqual([q.get("cursor") for _, p, q in self.sentry.requests if p.endswith("/issues/")], [None, ["1"], ["2"]])


class GroupingTest(SyncTest):
    def test_two_issues_with_one_exception_and_frame_make_one_linear_issue_listing_both(self) -> None:
        self.serve([], FakeSentry(issues=[releases_issue("WEB-1", 101, users=2), releases_issue("WEB-2", 102, users=4)]))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [f"created     ABC-900 {RELEASES_KEY}  (Low)"])
        (created,) = self.writes("IssueCreate")
        self.assertIn(f"- [WEB-1](https://{ORG}.sentry.io/issues/101/)\n- [WEB-2](https://{ORG}.sentry.io/issues/102/)", created["description"])

    def test_excluded_no_key_and_below_impact_issues_create_nothing_and_are_counted(self) -> None:
        issues = [
            sentry_issue("WEB-1", number=101, value="window.ethereum is undefined"),
            sentry_issue("WEB-2", number=102, type_=None, value="918273645 <> 42"),
            sentry_issue("WEB-3", number=103, filename=RELEASES_FILE, function="ReleaseList", users=2, count=9),
        ]
        self.serve([], FakeSentry(issues=issues))
        proc = self.run_sync()
        self.assertEqual((proc.returncode, proc.stdout), (0, (
            "excluded    1 issues  (browser-extension 1)\n"
            "no key      1 issues  (no in-repo frame, no message template)\n"
            "below       1 error groups under the impact threshold\n"
            "suppressed  0 error groups canceled within suppressDays\n"
            "legacy      0 untouched (excluded or no key), 0 kept (started or assigned)\n"
        )))
        self.assertEqual(self.linear.writes, [])

    def test_a_merge_rule_makes_one_group_with_its_key(self) -> None:
        issues = [
            sentry_issue("WEB-1", number=101, type_="RecoverableHydrationError", filename=RELEASES_FILE, function="A"),
            sentry_issue("WEB-2", number=102, type_="RecoverableHydrationError", value="other"),
        ]
        self.serve([], FakeSentry(issues=issues))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), ["created     ABC-900 web:hydration  (Medium)"])
        self.assertEqual(self.writes("IssueCreate")[0]["title"], "web: hydration")


class CreateTest(SyncTest):
    def test_an_eligible_group_gets_exactly_one_issue_and_a_rerun_creates_nothing(self) -> None:
        self.serve([], FakeSentry(issues=[releases_issue(users=30, count=96)]))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [f"created     ABC-900 {RELEASES_KEY}  (High)"])
        (created,) = self.writes("IssueCreate")
        self.assertEqual(
            {k: sorted(v) if k == "labelIds" else v for k, v in created.items() if k != "description"},
            {"teamId": "team-1", "stateId": "state-triage", "labelIds": ["label-Performance & stability", "label-Sentry"],
             "priority": 2, "title": "web: TypeError in ReleaseList"},
        )
        self.assertTrue(created["description"].endswith(f"\n\nSentry error group: {RELEASES_KEY} usersWhenCreated=30\n"))
        self.assertEqual(self.writes("AttachmentCreate"), [
            {"issueId": "id-ABC-900", "url": url(RELEASES_KEY), "title": f"Sentry error group {RELEASES_KEY}"},
        ])

        again = self.run_sync()
        self.assertEqual((again.returncode, self.lines(again)), (0, []))
        self.assertEqual(len(self.writes("IssueCreate")), 1)

    def test_an_issue_with_the_origin_line_and_no_attachment_is_repaired_not_created_again(self) -> None:
        crashed = tracked("ABC-1", RELEASES_KEY)
        crashed["attachments"] = []
        self.serve([crashed], FakeSentry(issues=[releases_issue()]))
        dry = self.run_sync("--dry-run")
        self.assertEqual((dry.returncode, self.lines(dry)), (0, [f"repaired    ABC-1 {RELEASES_KEY}"]))
        self.assertEqual(self.linear.writes, [])

        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [f"repaired    ABC-1 {RELEASES_KEY}"])
        self.assertEqual(self.writes(), [{"issueId": "id-ABC-1", "url": url(RELEASES_KEY), "title": f"Sentry error group {RELEASES_KEY}"}])

    def test_a_new_member_gets_one_marked_comment_and_no_description_edit(self) -> None:
        self.serve([tracked("ABC-1", RELEASES_KEY)], FakeSentry(issues=[releases_issue("WEB-1", 101), releases_issue("WEB-9", 109)]))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), ["added       ABC-1 +WEB-9"])
        self.assertEqual(self.writes(), [{
            "issueId": "id-ABC-1",
            "body": f"Sentry issue [WEB-9](https://{ORG}.sentry.io/issues/109/) joined this error group.\n\n"
                    "<!-- sentry-linear-sync:member:WEB-9 -->",
        }])
        again = self.run_sync()
        self.assertEqual(self.lines(again), [])
        self.assertEqual(len(self.writes()), 1)

    def test_creates_stop_at_max_open_in_rank_order_but_urgent_passes_the_cap(self) -> None:
        issues = [
            releases_issue("WEB-1", 101, users=10),
            sentry_issue("WEB-2", number=102, filename="../src/app/search.tsx", function="buildManifest", users=30),
            sentry_issue("CMS-3", number=103, project="cms", filename="src/auth/session.ts", function="read", users=300),
        ]
        open_issues = [tracked(f"ABC-{n}", f"web:msg:00000000000{n}", members=((f"WEB-{n}0", f"{n}0"),)) for n in (1, 2)]
        recent = {"10": 1, "20": 1}
        for name, max_open, existing, expected in (
            ("room for one", 1, [], [
                "created     ABC-900 cms:TypeError:packages/cms/src/auth/session.ts:read  (Urgent)",
                f"held        {SEARCH_KEY}  (cap 1 reached, rank 2)",
                f"held        {RELEASES_KEY}  (cap 1 reached, rank 3)",
            ]),
            ("cap already reached", 2, open_issues, [
                "created     ABC-900 cms:TypeError:packages/cms/src/auth/session.ts:read  (Urgent)",
                f"held        {SEARCH_KEY}  (cap 2 reached, rank 2)",
                f"held        {RELEASES_KEY}  (cap 2 reached, rank 3)",
            ]),
            ("room for all but the last", 3, open_issues[:1], [
                "created     ABC-900 cms:TypeError:packages/cms/src/auth/session.ts:read  (Urgent)",
                f"created     ABC-901 {SEARCH_KEY}  (High)",
                f"held        {RELEASES_KEY}  (cap 3 reached, rank 3)",
            ]),
        ):
            with self.subTest(name):
                self.commit_rules({**RULES, "maxOpen": max_open})
                self.serve(existing, FakeSentry(issues=issues, recent=recent))
                self.assertEqual(self.lines(self.run_sync()), expected)


class TextSafetyTest(SyncTest):
    def test_no_sentinel_reaches_a_title_description_attachment_or_comment(self) -> None:
        value = f"Failed for {SENTINELS[0]} at {SENTINELS[1]} id {SENTINELS[2]} {SENTINELS[3]} \"{SENTINELS[4]}\""
        extra = {"tags": [{"key": "url", "value": SENTINELS[1]}], "user": {"email": SENTINELS[0], "id": SENTINELS[2]},
                 "culprit": SENTINELS[1]}
        issues = [
            sentry_issue("WEB-1", number=101, type_=None, value=value, **extra),
            sentry_issue("WEB-2", number=102, value=value, filename=RELEASES_FILE, function="ReleaseList", **extra),
            sentry_issue("WEB-3", number=103, value=value, filename=RELEASES_FILE, function="ReleaseList", **extra),
        ]
        self.serve([tracked("ABC-1", RELEASES_KEY, members=(("WEB-2", "102"),))], FakeSentry(issues=issues))
        proc = self.run_sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([op for op, _ in self.linear.writes], ["CommentOnIssue", "IssueCreate", "AttachmentCreate"])
        written = json.dumps(self.linear.writes) + proc.stdout
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, written)
        self.assertEqual(self.writes("IssueCreate")[0]["title"], "web: Failed for <email> at <url> id <n> <uuid> <str>")


def record(change: str) -> str:
    return f"This job changed the priority.\n\n<!-- sentry-linear-sync:priority:{change} -->"


class PriorityTest(SyncTest):
    def test_a_priority_the_job_writes_is_recorded_and_the_job_keeps_owning_it(self) -> None:
        self.serve([tracked("ABC-1", RELEASES_KEY, priority=3)], FakeSentry(issues=[releases_issue(users=30)]))
        dry = self.run_sync("--dry-run")
        self.assertEqual(self.lines(dry), ["priority    ABC-1 Medium -> High"])
        self.assertEqual(self.linear.writes, [])

        proc = self.run_sync()
        self.assertEqual(self.lines(proc), ["priority    ABC-1 Medium -> High"])
        self.assertEqual(self.writes("IssueUpdate"), [{"id": "id-ABC-1", "priority": 2}])
        comments = self.writes("CommentOnIssue")
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0]["body"].endswith("<!-- sentry-linear-sync:priority:3-2 -->"))

        self.sentry.issues[0]["userCount"] = 300
        again = self.run_sync()
        self.assertEqual(self.lines(again), ["priority    ABC-1 High -> Urgent"])
        self.assertEqual(self.writes("IssueUpdate"), [{"id": "id-ABC-1", "priority": 2}, {"id": "id-ABC-1", "priority": 1}])

    def test_app_changes_that_all_match_a_record_leave_the_issue_owned(self) -> None:
        issue = tracked("ABC-1", RELEASES_KEY, priority=3, history=[(4, 3, APP_USER)], comments=[record("4-3")])
        self.serve([issue], FakeSentry(issues=[releases_issue(users=30)]))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), ["priority    ABC-1 Medium -> High"])
        self.assertEqual(self.writes("IssueUpdate"), [{"id": "id-ABC-1", "priority": 2}])
        bodies = [w["body"] for w in self.writes("CommentOnIssue")]
        self.assertEqual(len(bodies), 1)
        self.assertTrue(bodies[0].endswith("<!-- sentry-linear-sync:priority:3-2 -->"))

    def test_no_write_of_any_kind_for_an_app_change_with_no_matching_record(self) -> None:
        for name, history, comments in (
            ("unrecorded", [(3, 4, APP_USER), (4, 3, APP_USER)], []),
            ("before records existed", [(3, 2, APP_USER)], []),
            ("repeated value", [(4, 3, APP_USER), (3, 4, APP_USER), (4, 3, APP_USER)], [record("4-3")]),
        ):
            with self.subTest(name):
                issue = tracked("ABC-1", RELEASES_KEY, priority=3, history=history, comments=comments)
                self.serve([issue], FakeSentry(issues=[releases_issue(users=30)]))
                proc = self.run_sync()
                self.assertEqual((proc.returncode, self.lines(proc), self.linear.writes), (0, [], []))
                dry = self.run_sync("--dry-run")
                self.assertEqual(self.lines(dry), [])

    def test_no_priority_write_after_a_person_changed_it_or_the_issue_left_triage(self) -> None:
        for name, issue in (
            ("person", tracked("ABC-1", RELEASES_KEY, priority=3, history=[(3, 4, PERSON), (4, 3, APP_USER)], comments=[record("4-3")])),
            ("todo", tracked("ABC-1", RELEASES_KEY, priority=3, state="unstarted")),
        ):
            with self.subTest(name):
                self.serve([issue], FakeSentry(issues=[releases_issue(users=30)]))
                proc = self.run_sync()
                self.assertEqual((proc.returncode, self.lines(proc), self.linear.writes), (0, [], []))


class PriorityOwnershipTest(unittest.TestCase):
    def test_ownership_needs_every_change_by_the_app_and_one_record_per_change(self) -> None:
        def change(f, t, by_me=True):
            return {"from": f, "to": t, "byMe": by_me}

        def rec(value):
            return f"text\n\n<!-- sentry-linear-sync:priority:{value} -->"

        for name, bodies, changes, expected in (
            ("empty", [], [], True),
            ("match", [rec("4-3"), "plain comment"], [change(4, 3)], True),
            ("extra app change", [rec("4-3")], [change(4, 3), change(3, 2)], False),
            ("person change", [rec("4-3")], [change(4, 3, False)], False),
            ("repeated value", [rec("4-3")], [change(4, 3), change(3, 4), change(4, 3)], False),
            ("extra record", [rec("4-3"), rec("3-2")], [change(4, 3)], False),
        ):
            with self.subTest(name):
                self.assertIs(sentry_sync.priority_is_recorded(bodies, changes), expected)


class LifecycleTest(SyncTest):
    def test_a_quiet_unstarted_issue_is_canceled_with_one_marked_comment(self) -> None:
        self.serve([tracked("ABC-1", RELEASES_KEY)], FakeSentry())
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), ["canceled    ABC-1 no events for 14 days"])
        self.assertEqual(self.writes(), [
            {"id": "id-ABC-1", "stateId": "state-canceled"},
            {"issueId": "id-ABC-1", "body": "No member Sentry issue had an event for 14 days, so this job canceled the issue.\n\n"
                                            "<!-- sentry-linear-sync:cancel -->"},
        ])
        self.assertEqual(self.lines(self.run_sync()), [])
        self.assertEqual(len(self.writes()), 2)

    def test_a_cancel_in_a_team_without_one_canceled_state_exits_1_and_changes_no_state(self) -> None:
        extra = {"id": "state-canceled-2", "name": "Won't do", "type": "canceled"}
        for states in ([s for s in STATES if s["type"] != "canceled"], [*STATES, extra]):
            with self.subTest(count=len([s for s in states if s["type"] == "canceled"])):
                self.serve([tracked("ABC-1", RELEASES_KEY)], FakeSentry(), states=states)
                proc = self.run_sync()
                self.assertEqual(proc.returncode, 1)
                self.assertIn("status of type canceled", proc.stderr)
                self.assertEqual(self.linear.writes, [])

    def test_a_done_issue_is_verified_once_after_verify_days_without_events_from_later_releases(self) -> None:
        done = tracked("ABC-1", RELEASES_KEY, state="completed", completed_at="2026-10-20T00:00:00.000Z")
        releases = {"p-web": [{"version": "web@3", "dateCreated": "2026-10-25T00:00:00Z"},
                              {"version": "web@2", "dateCreated": "2026-10-21T00:00:00Z"},
                              {"version": "web@1", "dateCreated": "2026-10-01T00:00:00Z"}]}
        self.serve([done], FakeSentry(releases=releases, release_events={("101", "web@1"): 4}))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), ["verified    ABC-1 0 events in web@2 and later for 7 days"])
        self.assertEqual(self.writes(), [{"issueId": "id-ABC-1", "body": (
            "Verified: 0 events in `web@2` and later releases for 7 days after this issue completed.\n\n"
            "<!-- sentry-linear-sync:verify:web@2 -->")}])
        self.assertEqual(self.lines(self.run_sync()), [])

    def test_an_event_from_a_later_release_opens_a_related_regression_and_leaves_the_old_issue_closed(self) -> None:
        done = tracked("ABC-1", RELEASES_KEY, state="completed", completed_at="2026-10-30T00:00:00.000Z")
        releases = {"p-web": [{"version": "web@2", "dateCreated": "2026-10-31T00:00:00Z"}]}
        self.serve([done], FakeSentry(issues=[releases_issue()], releases=releases, release_events={("101", "web@2"): 3}))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), ["regressed   ABC-1 -> created ABC-900 (related)"])
        self.assertEqual(self.writes("IssueRelationCreate"), [{"issueId": "id-ABC-900", "relatedIssueId": "id-ABC-1", "type": "related"}])
        self.assertEqual(self.linear.find("ABC-1")["state"]["type"], "completed")
        self.assertEqual(self.writes("IssueUpdate"), [])

    def test_a_duplicate_teaches_its_canonical_issue_its_key(self) -> None:
        canonical = tracked("ABC-1", RELEASES_KEY, state="started")
        duplicate = tracked("ABC-2", SEARCH_KEY, members=(("WEB-2", "102"),), state="duplicate", duplicate_of="ABC-1")
        search = sentry_issue("WEB-2", number=102, filename="../src/app/search.tsx", function="buildManifest")
        self.serve([canonical, duplicate], FakeSentry(issues=[search]))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [f"linked      ABC-1 +{SEARCH_KEY} (from ABC-2)"])
        self.assertEqual(self.writes(), [{"issueId": "id-ABC-1", "url": url(SEARCH_KEY), "title": f"Sentry error group {SEARCH_KEY}"}])
        self.assertEqual(self.lines(self.run_sync()), [])
        self.assertEqual(len(self.writes()), 1)

    def test_a_duplicate_of_an_issue_the_job_does_not_own_is_reported_and_not_linked(self) -> None:
        person = linear_issue("ABC-5", creator=PERSON, labels=("Bug",))
        duplicate = tracked("ABC-2", SEARCH_KEY, state="duplicate", duplicate_of="ABC-5")
        self.serve([person, duplicate], FakeSentry())
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [
            f"not linked  ABC-2 {SEARCH_KEY}: ABC-5 is not an issue this job created or took over",
        ])
        self.assertEqual(self.linear.writes, [])


class TakeOverTest(SyncTest):
    def test_two_legacy_issues_for_one_key_keep_the_older_and_mark_the_newer_duplicate(self) -> None:
        details = {"501": releases_issue("WEB-1", 501), "502": releases_issue("WEB-2", 502)}
        old, new = legacy("ABC-10", "501", "2026-09-01T00:00:00.000Z"), legacy("ABC-11", "502", "2026-10-01T00:00:00.000Z")
        self.serve([new, old], FakeSentry(issues=list(details.values()), details=details, recent={"501": 1, "502": 1}))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [f"taken over  ABC-10 {RELEASES_KEY}", "duplicate   ABC-11 -> ABC-10"])
        self.assertEqual(self.writes("AttachmentCreate"), [
            {"issueId": "id-ABC-10", "url": url(RELEASES_KEY), "title": f"Sentry error group {RELEASES_KEY}"},
        ])
        self.assertEqual(self.writes("IssueRelationCreate"), [{"issueId": "id-ABC-11", "relatedIssueId": "id-ABC-10", "type": "duplicate"}])
        self.assertEqual(self.linear.find("ABC-11")["state"]["type"], "duplicate")
        writes = len(self.linear.writes)
        again = self.run_sync()
        self.assertEqual((again.returncode, self.lines(again), len(self.linear.writes)), (0, [], writes))

    def test_a_started_or_assigned_legacy_issue_is_never_marked_duplicate_and_becomes_canonical(self) -> None:
        details = {"501": releases_issue("WEB-1", 501), "502": releases_issue("WEB-2", 502), "503": releases_issue("WEB-3", 503)}
        issues = [
            legacy("ABC-10", "501", "2026-09-01T00:00:00.000Z"),
            legacy("ABC-11", "502", "2026-10-01T00:00:00.000Z", state="started"),
            legacy("ABC-12", "503", "2026-10-02T00:00:00.000Z", assigned=True),
        ]
        self.serve(issues, FakeSentry(details=details))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [f"taken over  ABC-11 {RELEASES_KEY}", "duplicate   ABC-10 -> ABC-11"])
        self.assertIn("legacy      0 untouched (excluded or no key), 1 kept (started or assigned)", proc.stdout)

    def test_a_legacy_issue_whose_sentry_issue_is_excluded_or_keyless_is_untouched_and_counted(self) -> None:
        details = {
            "501": sentry_issue("WEB-1", number=501, value="window.ethereum is undefined"),
            "502": sentry_issue("WEB-2", number=502, type_=None, value="42"),
        }
        self.serve([legacy("ABC-10", "501", "2026-09-01T00:00:00.000Z"), legacy("ABC-11", "502", "2026-09-02T00:00:00.000Z"),
                    legacy("ABC-12", "599", "2026-09-03T00:00:00.000Z")], FakeSentry(details=details))
        proc = self.run_sync()
        self.assertEqual(self.lines(proc), [])
        self.assertIn("legacy      3 untouched (excluded or no key), 0 kept (started or assigned)", proc.stdout)
        self.assertEqual(self.linear.writes, [])


class ScopeAndOutputTest(SyncTest):
    def scenario(self) -> None:
        details = {"501": releases_issue("WEB-1", 501)}
        others = [
            linear_issue("ABC-20", creator=PERSON),
            linear_issue("ABC-21", creator=PERSON, attachments=[{"url": url(SEARCH_KEY), "sourceType": "api"}]),
            linear_issue("ABC-22", creator=None, labels=("Bug",), attachments=[
                {"url": f"https://sentry.io/organizations/{ORG}/issues/501/", "sourceType": "sentry"}]),
            legacy("ABC-23", "501", "2026-09-01T00:00:00.000Z", state="completed"),
        ]
        issues = [
            releases_issue("WEB-1", 501, users=30),
            sentry_issue("WEB-2", number=102, filename="../src/app/search.tsx", function="buildManifest"),
        ]
        self.serve([*others, legacy("ABC-10", "501", "2026-09-02T00:00:00.000Z")], FakeSentry(issues=issues, details=details))

    def test_the_job_changes_only_issues_it_created_or_took_over_and_never_writes_to_sentry(self) -> None:
        self.scenario()
        proc = self.run_sync()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.lines(proc), [f"taken over  ABC-10 {RELEASES_KEY}", f"created     ABC-900 {SEARCH_KEY}  (Medium)"])
        touched = {v.get("issueId") or v.get("id") for _, v in self.linear.writes} - {None}
        self.assertEqual(touched, {"id-ABC-10", "id-ABC-900"})
        self.assertEqual({method for method, _, _ in self.sentry.requests}, {"GET"})

    def test_a_dry_run_prints_the_same_plan_and_writes_nothing(self) -> None:
        self.scenario()
        dry = self.run_sync("--dry-run")
        self.assertEqual(self.linear.writes, [])
        real = self.run_sync()
        self.assertEqual(dry.stdout, real.stdout.replace("ABC-900", "new"))

    def test_a_held_lock_prints_locked_and_writes_nothing(self) -> None:
        self.scenario()
        with open(self.home / "sentry-linear-sync.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = self.run_sync()
        self.assertEqual((proc.returncode, proc.stdout), (0, "locked\n"))
        self.assertEqual((self.linear.writes, self.sentry.requests), ([], []))


# ---------------------------------------------------------------------------------------------------------------
# plan_sync, one table row for each action kind
# ---------------------------------------------------------------------------------------------------------------

T0 = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
PLAN_RULES = sentry_sync.parse_rules(RULES)


def observed_issue(short_id: str, number: str, *, path="packages/web/src/routes/releases.tsx", function="ReleaseList", users=5,
                   events=20, exception="TypeError", message="boom") -> sentry_sync.SentryIssue:
    frame = sentry_sync.InRepoFrame(path, function) if path else None
    return sentry_sync.SentryIssue(number, short_id, "web", exception, message, frame, False, T0, users, events)


def tracked_issue(identifier: str, key=RELEASES_KEY, *, state="triage", members=("101",), attached=True, created_by_me=True,
                  priority=3, owned=True, markers=(), completed=None, canceled=None, duplicate_of=None, users_when_created=5):
    key = sentry_sync.GroupKey(key)
    return sentry_sync.TrackedIssue(
        f"id-{identifier}", identifier, (key,), frozenset({key} if attached else ()), state, datetime(2026, 10, 1, tzinfo=timezone.utc),
        completed, canceled, duplicate_of, priority, users_when_created, frozenset(members), frozenset(markers), created_by_me, owned,
    )


def legacy_issue(identifier: str, sentry_id: str, day: int, *, started=False, assigned=False) -> sentry_sync.LegacyIssue:
    return sentry_sync.LegacyIssue(f"id-{identifier}", identifier, sentry_id, datetime(2026, 9, day, tzinfo=timezone.utc), started, assigned)


def plan(issues=(), tracked=(), legacy=(), recent=None, counts=None, sources=None, rules=PLAN_RULES):
    observation = sentry_sync.Observation(tuple(issues), recent or {}, counts or {}, sources or {})
    return sentry_sync.plan_sync(observation, sentry_sync.TrackedRead(tuple(tracked), tuple(legacy)), rules, ORG, T0)


def summarize(result) -> list:
    rows = []
    for a in result.actions:
        match a:
            case Repair():
                rows.append(("repair", a.issue.identifier, a.key))
            case Create():
                rows.append(("create", a.group.key, a.rank, a.delegate, a.previous))
            case TakeOver():
                rows.append(("takeOver", a.legacy.identifier, a.key))
            case MarkDuplicate():
                rows.append(("markDuplicate", a.legacy.identifier, a.canonical))
            case AddMembers():
                rows.append(("addMembers", a.issue.identifier, tuple(m.short_id for m in a.added)))
            case SetPriority():
                rows.append(("setPriority", a.issue.identifier, a.to))
            case LinkKey():
                rows.append(("linkKey", a.canonical.identifier, a.key))
            case LinkRefused():
                rows.append(("linkRefused", a.duplicate, a.canonical))
            case CloseInactive():
                rows.append(("closeInactive", a.issue.identifier, a.quiet_days))
            case Verify():
                rows.append(("verify", a.issue.identifier, a.fix_release))
            case OpenRegression():
                rows.append(("openRegression", a.previous.identifier, a.release))
            case Hold():
                rows.append(("hold", a.group.key, a.rank))
    return rows


class PlanSyncTest(unittest.TestCase):
    def test_each_action_kind(self) -> None:
        oct20 = datetime(2026, 10, 20, tzinfo=timezone.utc)
        after = sentry_sync.ReleaseCounts({}, {"web@2": datetime(2026, 10, 21, tzinfo=timezone.utc)})
        regressed = sentry_sync.ReleaseCounts({"web@2": 1}, after.created_after)
        two = sentry_sync.parse_rules({**RULES, "maxOpen": 1})
        web1 = observed_issue("WEB-1", "101")
        table = [
            ("repair", dict(tracked=[tracked_issue("ABC-1", attached=False)], issues=[web1]),
             [("repair", "ABC-1", RELEASES_KEY)]),
            ("create", dict(issues=[web1]), [("create", RELEASES_KEY, 1, False, None)]),
            ("create after a long-canceled issue", dict(issues=[web1], tracked=[tracked_issue(
                "ABC-1", state="canceled", canceled=datetime(2026, 1, 1, tzinfo=timezone.utc))]),
             [("create", RELEASES_KEY, 1, False, "ABC-1")]),
            ("suppressed", dict(issues=[web1], tracked=[tracked_issue("ABC-1", state="canceled", canceled=oct20)]), []),
            ("takeOver and markDuplicate", dict(
                legacy=[legacy_issue("ABC-2", "201", 2), legacy_issue("ABC-3", "202", 1)],
                sources={"201": observed_issue("WEB-2", "201"), "202": observed_issue("WEB-3", "202")}),
             [("takeOver", "ABC-3", RELEASES_KEY), ("markDuplicate", "ABC-2", "ABC-3")]),
            ("markDuplicate onto the job's open issue", dict(
                tracked=[tracked_issue("ABC-1")], legacy=[legacy_issue("ABC-2", "201", 2)], recent={"101": 1},
                sources={"201": observed_issue("WEB-2", "201")}),
             [("markDuplicate", "ABC-2", "ABC-1")]),
            ("addMembers", dict(issues=[web1, observed_issue("WEB-9", "109")], tracked=[tracked_issue("ABC-1")], recent={"101": 1}),
             [("addMembers", "ABC-1", ("WEB-9",))]),
            ("addMembers skips a marked member", dict(issues=[observed_issue("WEB-9", "109")],
             tracked=[tracked_issue("ABC-1", markers=("member:WEB-9",))], recent={"101": 1}), []),
            ("setPriority", dict(issues=[observed_issue("WEB-1", "101", users=30)], tracked=[tracked_issue("ABC-1")], recent={"101": 1}),
             [("setPriority", "ABC-1", 2)]),
            ("no setPriority when not owned", dict(issues=[observed_issue("WEB-1", "101", users=30)],
             tracked=[tracked_issue("ABC-1", owned=False)], recent={"101": 1}), []),
            ("linkKey", dict(tracked=[tracked_issue("ABC-1", state="started"), tracked_issue(
                "ABC-2", SEARCH_KEY, state="duplicate", duplicate_of="ABC-1")]),
             [("linkKey", "ABC-1", SEARCH_KEY)]),
            ("linkRefused", dict(tracked=[tracked_issue("ABC-2", SEARCH_KEY, state="duplicate", duplicate_of="ABC-5")]),
             [("linkRefused", "ABC-2", "ABC-5")]),
            ("closeInactive", dict(tracked=[tracked_issue("ABC-1")], recent={"101": 0}), [("closeInactive", "ABC-1", 14)]),
            ("verify", dict(tracked=[tracked_issue("ABC-1", state="completed", completed=oct20)],
             counts={RELEASES_KEY: after}), [("verify", "ABC-1", "web@2")]),
            ("verify waits for verifyDays", dict(tracked=[tracked_issue(
                "ABC-1", state="completed", completed=datetime(2026, 11, 1, tzinfo=timezone.utc))],
             counts={RELEASES_KEY: sentry_sync.ReleaseCounts({}, {"web@2": datetime(2026, 11, 2, tzinfo=timezone.utc)})}), []),
            ("openRegression", dict(issues=[web1], tracked=[tracked_issue("ABC-1", state="completed", completed=oct20)],
             counts={RELEASES_KEY: regressed}), [("openRegression", "ABC-1", "web@2")]),
            ("hold", dict(rules=two, issues=[web1, observed_issue("WEB-2", "102", path="packages/web/src/app/search.tsx",
                                                                  function="buildManifest", users=6)]),
             [("create", SEARCH_KEY, 1, False, None), ("hold", RELEASES_KEY, 2)]),
        ]
        for name, inputs, expected in table:
            with self.subTest(name):
                self.assertEqual(summarize(plan(**inputs)), expected)

    def test_normalize_template_keeps_words_and_replaces_values(self) -> None:
        self.assertEqual(
            sentry_sync.normalize_template("Cannot read 'x' of user 918273645 at /reviews/a/b (sentinel@example.invalid)"),
            "Cannot read <str> of user <n> at <path> <email>",
        )
        self.assertIsNone(sentry_sync.normalize_template("918273645 42"))

    def test_repo_paths_resolve_by_their_one_longest_suffix(self) -> None:
        index = sentry_sync.repo_path_index(["packages/web/src/a/index.ts", "packages/app/src/a/index.ts", "packages/web/src/b.ts"])
        for filename, path in (
            ("../../../../web/src/a/index.ts", "packages/web/src/a/index.ts"),
            ("app:///src/b.ts", "packages/web/src/b.ts"),
            ("/packages/app/src/a/index.ts", "packages/app/src/a/index.ts"),
            ("src/a/index.ts", None),
            ("../node_modules/x/src/b.ts", None),
            ("app:///index.android.bundle", None),
        ):
            with self.subTest(filename):
                self.assertEqual(sentry_sync.resolve_repo_path(filename, index), path)

    def test_group_search_urls_are_unique_per_key_and_round_trip(self) -> None:
        keys = [RELEASES_KEY, "web:msg:0123456789ab", "web:msg:0123456789ac", "web:hydration"]
        urls = [url(k) for k in keys]
        self.assertEqual(len(set(urls)), len(keys))
        self.assertEqual([sentry_sync.key_of_url(u, ORG) for u in urls], keys)
        self.assertIsNone(sentry_sync.key_of_url(url(RELEASES_KEY), "other-org"))


if __name__ == "__main__":
    unittest.main()
