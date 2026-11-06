from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.dependabot_fakes import APP_LOGIN, FAKE_GH, FakeGitHub, FakeKev, github_issue, raw_alert, raw_repo
from tests.sentry_fakes import APP_USER, FakeLinear, linear_issue
from tests.support import REPO, SCRIPTS, FakeBroker, with_interpreter

sys.path.insert(0, str(SCRIPTS))
import dependabot_sweep as sweep  # noqa: E402

DEFAULT = "example-org/app"
PUBLIC = "example-org/oss"
LABEL = "Security"
NOW = "2026-11-03T12:00:00+00:00"
FAKE_HERMES = """#!/usr/bin/env python3
import os, sys
if sys.argv[1:3] == ["config", "get"]:
    print(open(os.environ["FAKE_SETTINGS"]).read())
else:
    sys.exit("hermes: refused " + repr(sys.argv[1:]))
"""
WRAPPER = """
import importlib.machinery, importlib.util, sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
loader = importlib.machinery.SourceFileLoader("dependabot_sweep_script", sys.argv[1] + "/dependabot-sweep")
module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(module)
sys.exit(module.run(sys.argv[5:], linear_api_url=sys.argv[2], kev_url=sys.argv[3], now=datetime.fromisoformat(sys.argv[4])))
"""


def origin(repo: str, package: str = "lodash", ecosystem: str = "npm") -> str:
    return sweep.origin_line(sweep.Key(repo, ecosystem, package), repo)


class SweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "hermes" / "profiles" / "claude-worker"
        (self.home / "plugins").mkdir(parents=True)
        (self.home / "plugins" / "linear-agent-session").symlink_to(REPO / "plugins" / "linear-agent-session")
        self.hermes = self.tmp / "fake-hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        self.gh = self.tmp / "fake-gh"
        self.gh.write_text(with_interpreter(FAKE_GH))
        for path in (self.hermes, self.gh):
            os.chmod(path, 0o755)
        self.settings = {
            "repositories": [{"full_name": DEFAULT, "routing": "default"}, {"full_name": PUBLIC, "routing": "synced_github"}],
            "github": {"login": APP_LOGIN},
            "dependabot_sweep": {"team_key": "ABC", "label": LABEL},
        }
        self.broker = FakeBroker(
            self.tmp / "hermes" / "factory-uploads.sock",
            linear_token=[{"ok": True, "token": "linear-token", "expires_at": 4102444800}] * 20,
        )
        self.addCleanup(self.broker.close)

    def serve(self, *, linear=(), linear_fail=(), kev=("CVE-2026-0001",), kev_status: int = 200, **github) -> None:
        github.setdefault("repos", [raw_repo(DEFAULT)])
        self.github = FakeGitHub(**github)
        self.linear = FakeLinear(linear, fail=linear_fail, labels=[LABEL])
        self.kev = FakeKev(kev, status=kev_status)
        for server in (self.github, self.linear, self.kev):
            self.addCleanup(server.close)

    def run_sweep(self, *args: str, settings=None) -> subprocess.CompletedProcess:
        settings_path = self.tmp / "settings.json"
        settings_path.write_text(json.dumps(self.settings if settings is None else settings))
        env = {
            **os.environ, "HERMES_HOME": str(self.home), "HERMES_BIN": str(self.hermes), "FAKE_SETTINGS": str(settings_path),
            "GH_BIN": str(self.gh), "FAKE_GH_URL": self.github.url,
        }
        for name in ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "GH_CONFIG_DIR"):
            env.pop(name, None)
        return subprocess.run(
            [sys.executable, "-c", WRAPPER, str(SCRIPTS), f"{self.linear.url}/graphql", self.kev.url, NOW, *args],
            env=env, capture_output=True, text=True,
        )

    def linear_writes(self, operation: str | None = None) -> list:
        return [v for op, v in self.linear.writes if operation in (None, op)]

    def state(self) -> dict:
        return json.loads((self.home / sweep.STATE_FILE).read_text())

    def assertNoWrites(self) -> None:
        self.assertEqual(self.linear.writes, [])
        self.assertEqual(self.github.writes, [])

    def sweep_once(self, *args: str) -> subprocess.CompletedProcess:
        result = self.run_sweep(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result


class CreateTest(SweepTest):
    def test_two_alerts_on_one_package_make_one_delegated_triage_issue_in_linear(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1), raw_alert(2, cves=("CVE-2026-0002",))]})
        self.sweep_once()
        [created] = self.linear_writes("IssueCreate")
        self.assertEqual(created["title"], f"Remediate lodash Dependabot alerts in {DEFAULT}")
        self.assertEqual(created["stateId"], "state-triage")
        self.assertEqual(created["labelIds"], [f"label-{LABEL}"])
        self.assertNotIn("delegateId", created)
        self.assertNotIn("assigneeId", created)
        self.assertEqual(
            [op for op, _ in self.linear.writes if op in ("IssueCreate", "AttachmentCreate", "IssueUpdate")],
            ["IssueCreate", "AttachmentCreate", "IssueUpdate"],
        )
        [link] = self.linear_writes("AttachmentCreate")
        self.assertEqual(link["url"], f"https://github.com/{DEFAULT}")
        [delegated] = self.linear_writes("IssueUpdate")
        self.assertEqual(delegated["delegateId"], APP_USER)
        self.assertEqual(created["priority"], 1)
        self.assertEqual(created["description"].split("\n", 1)[0], origin(DEFAULT))
        self.assertIn("[alert 1](", created["description"])
        self.assertIn("[alert 2](", created["description"])
        self.assertEqual(self.github.writes, [])
        self.assertEqual({str(self.home / "gh")}, self.github.config_dirs)

    def test_the_same_package_in_two_ecosystems_makes_two_issues(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1), raw_alert(2, ecosystem="pip")]})
        self.sweep_once()
        self.assertEqual(
            sorted(c["description"].split("\n", 1)[0] for c in self.linear_writes("IssueCreate")),
            sorted([origin(DEFAULT), origin(DEFAULT, ecosystem="pip")]),
        )

    def test_an_alert_without_a_patch_shows_none_the_summary_and_the_references(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1, patched=None, summary="Prototype pollution", references=("https://example.invalid/a",))]})
        self.sweep_once()
        [created] = self.linear_writes("IssueCreate")
        self.assertIn("first patched `none`", created["description"])
        self.assertIn("Prototype pollution", created["description"])
        self.assertIn("https://example.invalid/a", created["description"])

    def test_a_synced_public_repository_gets_a_github_issue_and_no_linear_issue(self) -> None:
        self.serve(repos=[raw_repo(PUBLIC, "public")], alerts={PUBLIC: [raw_alert(1)]}, labels={PUBLIC: [LABEL, "Needs triage"]})
        self.sweep_once()
        [(_, repo, details)] = self.github.written("create")
        self.assertEqual(repo, PUBLIC)
        self.assertEqual(details["labels"], [LABEL, "Needs triage"])
        self.assertNotIn("--assignee", details["args"])
        self.assertEqual(details["body"].split("\n", 1)[0], origin(PUBLIC))
        self.assertEqual(self.linear_writes("IssueCreate"), [])

    def test_repositories_the_factory_cannot_build_file_nothing_and_are_reported(self) -> None:
        others = ["example-org/private-other", "example-org/public-other", "example-org/inner"]
        self.serve(
            repos=[raw_repo(others[0]), raw_repo(others[1], "public"), raw_repo(others[2], "internal")],
            alerts={name: [raw_alert(1), raw_alert(2)] for name in others},
        )
        self.sweep_once()
        self.assertNoWrites()
        entries = self.state()["repositories"]
        self.assertEqual([(e["repository"], e["open_alerts"]) for e in entries], [(name, 2) for name in others])
        self.assertEqual(len(sweep.advisory_lines(self.state())), 3)

    def test_a_missing_label_skips_its_repository_and_the_rest_is_swept(self) -> None:
        self.serve(
            repos=[raw_repo(PUBLIC, "public"), raw_repo(DEFAULT)],
            alerts={PUBLIC: [raw_alert(1)], DEFAULT: [raw_alert(2)]}, labels={PUBLIC: ["Needs triage"]},
        )
        self.sweep_once()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(len(self.linear_writes("IssueCreate")), 1)
        [entry] = self.state()["repositories"]
        self.assertEqual((entry["repository"], entry["kind"]), (PUBLIC, "skipped"))


class CopyTest(SweepTest):
    def public_issue(self, flags: str = "") -> dict:
        key = sweep.Key(PUBLIC, "npm", "lodash")
        repo = sweep.classify_repo(PUBLIC, "public", sweep.parse_settings(self.settings))
        alerts = [sweep.parse_alert(raw_alert(1))]
        priority = sweep.issue_priority(alerts, frozenset({"CVE-2026-0001"}))
        footer = sweep.Footer(sweep.fingerprint(alerts, priority, frozenset({"CVE-2026-0001"})), priority, frozenset({1}),
                              flags=frozenset(filter(None, [flags])))
        return github_issue(5, PUBLIC, sweep.render_body(key, repo, alerts, footer, frozenset({"CVE-2026-0001"})))

    def copy(self, **fields) -> dict:
        return linear_issue("ABC-7", creator=None, priority=0, labels=(),
                            attachments=[{"url": f"https://github.com/{PUBLIC}/issues/5", "sourceType": "github"}], **fields)

    def serve_public(self, issue: dict, copies=()) -> None:
        self.serve(repos=[raw_repo(PUBLIC, "public")], alerts={PUBLIC: [raw_alert(1)]}, labels={PUBLIC: [LABEL, "Needs triage"]},
                   issues={PUBLIC: [issue]}, linear=copies)

    def test_a_present_copy_is_delegated_prioritized_and_recorded(self) -> None:
        self.serve_public(self.public_issue(), [self.copy()])
        self.sweep_once()
        updates = self.linear_writes("IssueUpdate")
        self.assertIn({"id": "id-ABC-7", "delegateId": APP_USER}, updates)
        self.assertIn({"id": "id-ABC-7", "priority": 1}, updates)
        [(_, _, edit)] = self.github.written("edit")
        self.assertIn("copy-delegated", sweep.parse_footer(edit["body"]).flags)

    def test_a_recorded_delegation_is_not_repeated_after_a_person_undelegates(self) -> None:
        self.serve_public(self.public_issue("copy-delegated"), [self.copy()])
        self.sweep_once()
        self.assertNoWrites()

    def test_a_copy_another_agent_holds_is_not_delegated_and_the_rest_is_written(self) -> None:
        copy = self.copy()
        copy["delegate"] = {"id": "other-agent"}
        self.serve_public(self.public_issue(), [copy])
        self.sweep_once()
        self.assertEqual(self.linear_writes("IssueUpdate"), [{"id": "id-ABC-7", "priority": 1}])
        [(_, _, edit)] = self.github.written("edit")
        self.assertIn("copy-delegated", sweep.parse_footer(edit["body"]).flags)

    def test_a_label_cased_differently_still_counts(self) -> None:
        self.serve(repos=[raw_repo(PUBLIC, "public")], alerts={PUBLIC: [raw_alert(1)]}, labels={PUBLIC: [LABEL.upper(), "needs triage"]})
        self.sweep_once()
        self.assertEqual(len(self.github.written("create")), 1)

    def test_an_absent_copy_writes_nothing(self) -> None:
        self.serve_public(self.public_issue())
        self.sweep_once()
        self.assertNoWrites()


class FailureTest(SweepTest):
    def assertStopped(self, **serve) -> None:
        serve.setdefault("alerts", {DEFAULT: [raw_alert(1)]})
        self.serve(**serve)
        result = self.run_sweep()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNoWrites()
        self.assertFalse((self.home / sweep.STATE_FILE).exists())

    def test_a_failed_kev_read_stops_the_run(self) -> None:
        self.assertStopped(kev_status=500)

    def test_a_failed_repository_listing_stops_the_run(self) -> None:
        self.assertStopped(errors={"repositories": (500, "Server Error")})

    def test_a_failed_linear_read_stops_the_run(self) -> None:
        self.assertStopped(linear_fail={"LabeledIssues"})

    def test_a_failed_github_issues_read_stops_the_run(self) -> None:
        self.assertStopped(
            repos=[raw_repo(DEFAULT), raw_repo(PUBLIC, "public")], labels={PUBLIC: [LABEL, "Needs triage"]},
            errors={f"issues:{PUBLIC}": (502, "Bad Gateway")},
        )

    def test_a_5xx_401_or_integration_403_on_alerts_stops_the_run(self) -> None:
        for status, message in ((503, "Service Unavailable"), (401, "Bad credentials"), (403, "Resource not accessible by integration")):
            with self.subTest(status=status):
                self.assertStopped(errors={DEFAULT: (status, message)})

    def test_a_repository_with_alerts_disabled_is_reported_and_the_rest_is_swept(self) -> None:
        self.serve(
            repos=[raw_repo("example-org/off", "public"), raw_repo(DEFAULT)], alerts={DEFAULT: [raw_alert(1)]},
            errors={"example-org/off": (403, "Dependabot alerts are disabled for this repository.")},
        )
        self.sweep_once()
        self.assertEqual(len(self.linear_writes("IssueCreate")), 1)
        [entry] = self.state()["repositories"]
        self.assertEqual((entry["repository"], entry["kind"]), ("example-org/off", "skipped"))

    def test_a_write_failure_stops_that_repository_and_exits_1(self) -> None:
        self.serve(
            repos=[raw_repo(PUBLIC, "public"), raw_repo(DEFAULT)], labels={PUBLIC: [LABEL, "Needs triage"]},
            alerts={PUBLIC: [raw_alert(1), raw_alert(2, package="left-pad")], DEFAULT: [raw_alert(3)]},
            fail_writes={("create", PUBLIC)},
        )
        result = self.run_sweep()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.github.writes, [])
        self.assertEqual(len(self.linear_writes("IssueCreate")), 1)
        self.assertEqual(sum(1 for argv in self.github.requests if argv[:2] == ["issue", "create"]), 1)


class RerunTest(SweepTest):
    def test_dry_run_prints_the_plan_and_writes_nothing(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1)]})
        result = self.sweep_once("--dry-run")
        self.assertIn("create", result.stdout)
        self.assertNoWrites()
        self.assertFalse((self.home / sweep.STATE_FILE).exists())

    def test_a_rerun_with_no_change_and_an_epss_drift_below_the_threshold_writes_nothing(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1, epss="0.10e0")]})
        self.sweep_once()
        self.linear.writes.clear()
        self.sweep_once()
        self.github.alerts[DEFAULT] = [raw_alert(1, epss="0.5e0")]
        self.sweep_once()
        self.assertEqual(self.linear.writes, [])

    def test_a_new_alert_updates_the_same_issue_and_its_priority(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1, severity="low", cves=())]})
        self.sweep_once()
        [issue] = self.linear.issues.values()
        self.assertEqual(issue["priority"], 4)
        self.github.alerts[DEFAULT].append(raw_alert(2, severity="high", cves=()))
        self.sweep_once()
        self.assertEqual(self.linear_writes("IssueCreate")[1:], [])
        self.assertEqual(issue["priority"], 2)
        self.assertIn("[alert 2](", issue["description"])

    def test_a_priority_a_person_changed_is_not_overwritten(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1, severity="low", cves=())]})
        self.sweep_once()
        [issue] = self.linear.issues.values()
        issue["history"] = [{"fromPriority": 4, "toPriority": 3, "actor": {"id": "person-1"}}]
        issue["priority"] = 3
        self.github.alerts[DEFAULT].append(raw_alert(2, severity="high", cves=()))
        self.sweep_once()
        self.assertEqual(issue["priority"], 3)
        self.assertIn("[alert 2](", issue["description"])


class CancelTest(SweepTest):
    def own_issue(self, state: str = "triage", **fields) -> dict:
        return linear_issue("ABC-1", state=state, description=origin(DEFAULT) + "\n", labels=(LABEL,), **fields)

    def test_an_issue_whose_alerts_all_closed_is_commented_then_canceled_once(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1, state="fixed"), raw_alert(2, state="dismissed")]}, linear=[self.own_issue()])
        self.sweep_once()
        [comment] = self.linear_writes("CommentOnIssue")
        self.assertIn("dismissed, fixed", comment["body"])
        self.assertEqual(self.linear.issues["ABC-1"]["state"]["type"], "canceled")
        self.linear.writes.clear()
        self.sweep_once()
        self.assertEqual(self.linear_writes("CommentOnIssue"), [])

    def test_a_started_or_todo_issue_is_commented_once_and_stays_open(self) -> None:
        for state in ("started", "unstarted"):
            with self.subTest(state=state):
                self.serve(alerts={DEFAULT: [raw_alert(1, state="fixed")]}, linear=[self.own_issue(state)])
                self.sweep_once()
                self.sweep_once()
                self.assertEqual(len(self.linear_writes("CommentOnIssue")), 1)
                self.assertEqual(self.linear.issues["ABC-1"]["state"]["type"], state)

    def test_a_github_issue_with_an_assignee_is_commented_once_and_stays_open(self) -> None:
        footer = sweep.Footer("f", sweep.Priority.HIGH, frozenset({1}))
        issue = github_issue(5, PUBLIC, origin(PUBLIC) + "\n\n" + footer.render() + "\n", assignees=("someone",))
        self.serve(repos=[raw_repo(PUBLIC, "public")], alerts={PUBLIC: [raw_alert(1, state="fixed")]},
                   labels={PUBLIC: [LABEL, "Needs triage"]}, issues={PUBLIC: [issue]})
        self.sweep_once()
        self.sweep_once()
        self.assertEqual(len(self.github.written("comment")), 1)
        self.assertEqual(self.github.written("close"), [])

    def test_a_key_without_alerts_in_the_read_cancels_nothing(self) -> None:
        self.serve(alerts={DEFAULT: []}, linear=[self.own_issue()])
        self.sweep_once()
        self.assertNoWrites()

    def test_an_issue_without_the_origin_line_or_not_created_by_the_app_is_left_alone(self) -> None:
        lookalike = linear_issue("ABC-2", description="Remediate lodash Dependabot alerts\n", labels=(LABEL,))
        foreign = linear_issue("ABC-3", description=origin(DEFAULT) + "\n", labels=(LABEL,), creator="person-1")
        self.serve(alerts={DEFAULT: [raw_alert(1, state="fixed")]}, linear=[lookalike, foreign])
        self.sweep_once()
        self.assertNoWrites()


class RecurrenceTest(SweepTest):
    def closed_issue(self, marker: str | None, alerts=frozenset({1})) -> dict:
        footer = sweep.Footer("f", sweep.Priority.HIGH, alerts)
        comments = [sweep.COMMENT_MARKER.format(kind=marker)] if marker else []
        return linear_issue("ABC-1", state="canceled", description=f"{origin(DEFAULT)}\n\n{footer.render()}\n", labels=(LABEL,),
                            comments=comments, canceled_at="2026-10-02T00:00:00.000Z")

    def test_a_package_the_app_canceled_gets_a_new_related_issue_and_the_old_one_stays_closed(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1, state="fixed"), raw_alert(2)]}, linear=[self.closed_issue("canceled")])
        self.sweep_once()
        [created] = self.linear_writes("IssueCreate")
        [relation] = self.linear_writes("IssueRelationCreate")
        self.assertEqual((relation["relatedIssueId"], relation["type"]), ("id-ABC-1", "related"))
        self.assertEqual(self.linear.issues["ABC-1"]["state"]["type"], "canceled")
        self.assertEqual(created["description"].split("\n", 1)[0], origin(DEFAULT))

    def test_a_package_a_person_canceled_is_refiled_only_for_a_new_alert_number(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1)]}, linear=[self.closed_issue(None)])
        self.sweep_once()
        self.assertNoWrites()
        self.github.alerts[DEFAULT].append(raw_alert(2))
        self.sweep_once()
        self.assertEqual(len(self.linear_writes("IssueCreate")), 1)

    def test_a_github_recurrence_names_the_previous_issue(self) -> None:
        footer = sweep.Footer("f", sweep.Priority.HIGH, frozenset({1}), flags=frozenset({"notified", "canceled"}))
        old = github_issue(5, PUBLIC, f"{origin(PUBLIC)}\n\n{footer.render()}\n", state="closed")
        self.serve(repos=[raw_repo(PUBLIC, "public")], alerts={PUBLIC: [raw_alert(2)]},
                   labels={PUBLIC: [LABEL, "Needs triage"]}, issues={PUBLIC: [old]})
        self.sweep_once()
        [(_, _, details)] = self.github.written("create")
        self.assertIn("Follows #5", details["body"])
        self.assertEqual(self.github.written("edit"), [])


class PaginationTest(SweepTest):
    def test_every_page_of_repositories_alerts_and_issues_is_read_and_pull_requests_are_ignored(self) -> None:
        repos = [raw_repo(f"example-org/r{n}") for n in range(3)] + [raw_repo(PUBLIC, "public"), raw_repo(DEFAULT)]
        issues = [github_issue(n, PUBLIC, "unrelated", pull_request=n % 2 == 0) for n in range(1, 4)]
        issues.append(github_issue(9, PUBLIC, origin(PUBLIC) + "\n", pull_request=True))
        self.serve(
            page_size=2, repos=repos, labels={PUBLIC: [LABEL, "Needs triage"]}, issues={PUBLIC: issues},
            alerts={DEFAULT: [raw_alert(n, package=f"p{n}") for n in range(1, 6)], PUBLIC: [raw_alert(1)]},
        )
        self.sweep_once()
        self.assertEqual(len(self.linear_writes("IssueCreate")), 5)
        self.assertEqual(len(self.github.written("create")), 1)

    def test_an_archived_repository_is_never_read(self) -> None:
        self.serve(repos=[raw_repo(DEFAULT, archived=True)], alerts={DEFAULT: [raw_alert(1)]})
        self.sweep_once()
        self.assertNoWrites()
        self.assertFalse(any("dependabot" in " ".join(argv) for argv in self.github.requests))


class SettingsTest(SweepTest):
    def test_without_the_section_nothing_is_read_and_the_run_exits_0(self) -> None:
        self.serve()
        del self.settings["dependabot_sweep"]
        self.sweep_once()
        self.assertEqual((self.github.requests, self.linear.writes, self.kev.requests), ([], [], 0))

    def test_a_malformed_section_exits_2_and_writes_nothing(self) -> None:
        self.serve(alerts={DEFAULT: [raw_alert(1)]})
        self.settings["dependabot_sweep"] = {"team_key": "abc", "label": LABEL}
        result = self.run_sweep()
        self.assertEqual(result.returncode, 2)
        self.assertEqual((self.github.requests, self.linear.writes, self.kev.requests), ([], [], 0))


if __name__ == "__main__":
    unittest.main()
