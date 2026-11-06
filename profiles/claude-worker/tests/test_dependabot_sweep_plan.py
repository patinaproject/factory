from __future__ import annotations

import sys
import unittest
from dataclasses import replace

from tests.dependabot_fakes import raw_alert
from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import dependabot_sweep as sweep  # noqa: E402
from dependabot_sweep import (  # noqa: E402
    Buildability, Cancel, Comment, Copy, Create, DelegateCopy, ExistingIssue, Footer, Key, Priority, Relate, RepoRead,
    SetPriority, UpdateBody,
)

DEFAULT = "example-org/example-repo"
PUBLIC = "example-org/public-repo"
SETTINGS = sweep.Settings("ABC", "Theme", DEFAULT, frozenset({PUBLIC}), "example-app[bot]")
PRIVATE = sweep.classify_repo(DEFAULT, "private", SETTINGS)
SYNCED = sweep.classify_repo(PUBLIC, "public", SETTINGS)
KEV = frozenset({"CVE-2026-9999"})


def alert(number: int, **fields) -> sweep.Alert:
    return sweep.parse_alert(raw_alert(number, **fields))


def level(**fields) -> Priority:
    return sweep.issue_priority([alert(1, **fields)], KEV)


def body_for(alerts: list, repo=PRIVATE, kev=frozenset(), follows=None, flags=(), priority=None) -> str:
    open_alerts = [a for a in alerts if a.open]
    priority = priority or sweep.issue_priority(open_alerts, kev)
    footer = Footer(sweep.fingerprint(alerts, priority, kev), priority, frozenset(a.number for a in alerts), follows, frozenset(flags))
    key = Key(repo.full_name, alerts[0].ecosystem, alerts[0].package)
    return sweep.render_body(key, repo, open_alerts, footer, kev)


def issue(alerts: list, *, repo=PRIVATE, tracker="linear", state="triage", created_at="2026-10-01", **fields) -> ExistingIssue:
    body_args = fields.pop("body_args", {})
    body = fields.pop("body", None) or body_for(alerts, repo, **body_args)
    github = tracker == "github"
    if github:
        fields.setdefault("markers", frozenset(body_args.get("flags", ())))
    fields.setdefault("assigned", False)
    return ExistingIssue(
        tracker=tracker, key=Key(repo.full_name, alerts[0].ecosystem, alerts[0].package), ref="5" if github else "id-1",
        identifier="#5" if github else "ABC-1", url="https://github.com/x/y/issues/5" if github else "", is_open=state in sweep.OPEN_LINEAR | {"open"},
        state_type=state, created_at=created_at, body=body, **fields,
    )


def plan(alerts: list, issues=(), repo=PRIVATE, kev=KEV, skipped="") -> sweep.Plan:
    return sweep.plan([RepoRead(repo, tuple(alerts), skipped)], list(issues), kev)


class PriorityTest(unittest.TestCase):
    def test_a_low_runtime_alert_in_kev_is_urgent(self) -> None:
        self.assertEqual(level(severity="low", scope="runtime", cves=("CVE-2026-9999",)), Priority.URGENT)

    def test_a_low_null_scope_alert_in_kev_is_urgent(self) -> None:
        self.assertEqual(level(severity="low", scope=None, cves=("CVE-2026-9999",)), Priority.URGENT)

    def test_a_medium_runtime_alert_with_a_high_epss_percentile_is_high(self) -> None:
        self.assertEqual(level(severity="medium", epss="0.95e0"), Priority.HIGH)

    def test_a_medium_alert_without_epss_is_medium(self) -> None:
        self.assertEqual(level(severity="medium", epss=None), Priority.MEDIUM)

    def test_an_epss_percentile_below_the_threshold_does_not_raise(self) -> None:
        self.assertEqual(level(severity="medium", epss="0.89e0"), Priority.MEDIUM)

    def test_the_issue_takes_the_highest_level_of_its_alerts(self) -> None:
        alerts = [alert(1, severity="critical", scope="runtime"), alert(2, severity="low")]
        self.assertEqual(sweep.issue_priority(alerts, KEV), Priority.URGENT)

    def test_a_high_development_alert_is_medium_with_low_or_high_epss(self) -> None:
        self.assertEqual(
            [level(severity="high", scope="development", epss=epss) for epss in ("0.10e0", "0.95e0")], [Priority.MEDIUM, Priority.MEDIUM],
        )

    def test_a_high_null_scope_alert_is_high(self) -> None:
        self.assertEqual(level(severity="high", scope=None), Priority.HIGH)

    def test_a_low_development_alert_stays_low(self) -> None:
        self.assertEqual(level(severity="low", scope="development"), Priority.LOW)

    def test_a_development_alert_in_kev_is_not_urgent(self) -> None:
        self.assertEqual(level(severity="critical", scope="development", cves=("CVE-2026-9999",)), Priority.HIGH)

    def test_an_advisory_without_a_cve_is_not_in_kev(self) -> None:
        self.assertEqual(level(severity="low", cves=()), Priority.LOW)

    def test_the_linear_value_of_each_level(self) -> None:
        self.assertEqual([p.linear for p in (Priority.URGENT, Priority.HIGH, Priority.MEDIUM, Priority.LOW)], [1, 2, 3, 4])

    def test_an_unknown_severity_is_refused_at_the_boundary(self) -> None:
        with self.assertRaises(ValueError):
            sweep.parse_alert(raw_alert(1, severity="moderate"))


class OriginTest(unittest.TestCase):
    def test_the_origin_line_percent_encodes_each_part_and_round_trips(self) -> None:
        key = Key(DEFAULT, "npm", "@scope/pkg name")
        line = sweep.origin_line(key, DEFAULT)
        self.assertEqual(line, "Dependabot package: example-org%2Fexample-repo npm %40scope%2Fpkg%20name")
        self.assertEqual(sweep.parse_origin(line + "\nmore"), key)

    def test_a_body_without_the_origin_line_first_has_no_key(self) -> None:
        self.assertIsNone(sweep.parse_origin("intro\nDependabot package: a%2Fb npm x"))


class CreateTest(unittest.TestCase):
    def test_two_alerts_on_one_package_make_one_create_that_lists_both(self) -> None:
        [create] = plan([alert(1), alert(2, ghsa="GHSA-zzzz-zzzz-zzzz")]).actions
        self.assertEqual((type(create), create.priority, create.previous), (Create, Priority.HIGH, None))
        self.assertEqual(create.body.split("\n")[0], sweep.origin_line(create.key, DEFAULT))
        self.assertIn("GHSA-aaaa-bbbb-0001", create.body)
        self.assertIn("GHSA-zzzz-zzzz-zzzz", create.body)
        self.assertEqual(create.title, f"Remediate lodash Dependabot alerts in {DEFAULT}")

    def test_the_same_package_in_two_ecosystems_makes_two_creates(self) -> None:
        actions = plan([alert(1, ecosystem="npm"), alert(2, ecosystem="pip")]).actions
        self.assertEqual(sorted(a.key.ecosystem for a in actions), ["npm", "pip"])

    def test_an_alert_without_a_patch_lists_none_the_summary_and_the_references(self) -> None:
        [create] = plan([alert(1, patched=None, summary="Prototype pollution", references=("https://a.invalid/1", "https://b.invalid/2"))]).actions
        self.assertIn("first patched `none`", create.body)
        self.assertIn("Summary: Prototype pollution", create.body)
        self.assertIn("References: https://a.invalid/1, https://b.invalid/2", create.body)

    def test_a_patched_alert_has_no_summary_line(self) -> None:
        [create] = plan([alert(1, summary="Prototype pollution")]).actions
        self.assertNotIn("Prototype pollution", create.body)

    def test_a_missing_epss_shows_unknown_and_kev_shows_yes(self) -> None:
        [create] = plan([alert(1, cves=("CVE-2026-9999",))]).actions
        self.assertIn("EPSS percentile unknown, KEV yes", create.body)

    def test_the_body_ends_with_the_how_to_fix_section_then_the_footer(self) -> None:
        [create] = plan([alert(1)]).actions
        lines = create.body.rstrip("\n").split("\n")
        self.assertEqual((lines[-5], lines[-3].startswith("Triage uses"), lines[-1].startswith("Dependabot sweep: ")), ("## How to fix", True, True))

    def test_a_body_stays_under_the_github_limit_and_lists_the_rest_by_link(self) -> None:
        alerts = [alert(n, summary="s" * 400, patched=None, references=tuple(f"https://r.invalid/{n}/{i}" for i in range(30))) for n in range(1, 251)]
        [create] = plan(alerts, repo=SYNCED).actions
        self.assertLess(len(create.body), 65_536)
        self.assertIn("- GHSA-aaaa-bbbb-0250: [alert 250](", create.body)
        self.assertEqual(sweep.parse_footer(create.body).alerts, frozenset(range(1, 251)))

    def test_an_absurd_alert_count_still_stays_under_the_limit(self) -> None:
        [create] = plan([alert(n) for n in range(1, 1501)], repo=SYNCED).actions
        self.assertLess(len(create.body), 65_536)
        self.assertRegex(create.body, r"- \d+ more open alerts")


class UpdateTest(unittest.TestCase):
    def test_an_unchanged_fingerprint_makes_no_action(self) -> None:
        alerts = [alert(1)]
        self.assertEqual(plan(alerts, [issue(alerts, priority=Priority.HIGH.linear, priority_owned=True)]).actions, ())

    def test_an_epss_change_below_the_threshold_makes_no_action(self) -> None:
        old = [alert(1, epss="0.10e0")]
        existing = issue(old, priority=Priority.HIGH.linear, priority_owned=True)
        self.assertEqual(plan([alert(1, epss="0.20e0")], [existing]).actions, ())

    def test_an_epss_change_across_the_threshold_updates_the_body(self) -> None:
        old = [alert(1, severity="medium", epss="0.10e0")]
        existing = issue(old, priority=Priority.MEDIUM.linear, priority_owned=True)
        actions = plan([alert(1, severity="medium", epss="0.95e0")], [existing]).actions
        self.assertEqual([type(a) for a in actions], [SetPriority, UpdateBody])
        self.assertEqual(actions[0].to, Priority.HIGH)

    def test_priority_is_written_before_the_body(self) -> None:
        existing = issue([alert(1)], priority=Priority.HIGH.linear, priority_owned=True)
        actions = plan([alert(1), alert(2, severity="critical")], [existing]).actions
        self.assertEqual([type(a) for a in actions], [SetPriority, UpdateBody])

    def test_a_priority_someone_else_changed_is_not_written_but_the_body_is(self) -> None:
        existing = issue([alert(1)], priority=Priority.LOW.linear, priority_owned=False)
        actions = plan([alert(1), alert(2, severity="critical")], [existing]).actions
        self.assertEqual([type(a) for a in actions], [UpdateBody])

    def test_priority_is_not_written_outside_triage(self) -> None:
        existing = issue([alert(1)], state="started", priority=Priority.HIGH.linear, priority_owned=True)
        actions = plan([alert(1), alert(2, severity="critical")], [existing]).actions
        self.assertEqual([type(a) for a in actions], [UpdateBody])

    def test_a_missing_relation_to_the_previous_issue_is_added(self) -> None:
        alerts = [alert(1)]
        existing = issue(alerts, body_args={"follows": "ABC-0"}, priority=Priority.HIGH.linear)
        [action] = plan(alerts, [existing]).actions
        self.assertEqual((type(action), action.previous), (Relate, "ABC-0"))

    def test_a_present_relation_is_left_alone(self) -> None:
        alerts = [alert(1)]
        existing = issue(alerts, body_args={"follows": "ABC-0"}, priority=Priority.HIGH.linear, related=frozenset({"ABC-0"}))
        self.assertEqual(plan(alerts, [existing]).actions, ())


class RetireTest(unittest.TestCase):
    def closed(self, state="fixed") -> list:
        return [alert(1, state=state)]

    def test_an_unstarted_unassigned_issue_gets_the_comment_naming_the_states_then_is_canceled(self) -> None:
        old = [alert(1), alert(2)]
        actions = plan([alert(1, state="fixed"), alert(2, state="dismissed")], [issue(old)]).actions
        self.assertEqual([type(a) for a in actions], [Comment, Cancel])
        self.assertIn("states seen: dismissed, fixed", actions[0].text)
        self.assertIn("<!-- dependabot-sweep:canceled -->", actions[0].text)

    def test_a_commented_issue_is_canceled_without_a_second_comment(self) -> None:
        existing = issue([alert(1)], markers=frozenset({"canceled"}))
        self.assertEqual([type(a) for a in plan(self.closed(), [existing]).actions], [Cancel])

    def test_a_started_issue_gets_the_comment_once_and_stays_open(self) -> None:
        self.assertEqual([type(a) for a in plan(self.closed(), [issue([alert(1)], state="started")]).actions], [Comment])
        notified = issue([alert(1)], state="started", markers=frozenset({"notified"}))
        self.assertEqual(plan(self.closed(), [notified]).actions, ())

    def test_a_todo_issue_is_not_canceled(self) -> None:
        self.assertEqual([type(a) for a in plan(self.closed(), [issue([alert(1)], state="unstarted")]).actions], [Comment])

    def test_an_assigned_issue_is_not_canceled(self) -> None:
        existing = issue([alert(1)], assigned=True)
        self.assertEqual([type(a) for a in plan(self.closed(), [existing]).actions], [Comment])

    def test_a_key_without_any_alert_is_never_canceled(self) -> None:
        self.assertEqual(plan([], [issue([alert(1)])]).actions, ())

    def test_a_skipped_repository_cancels_nothing(self) -> None:
        self.assertEqual(plan([], [issue([alert(1)])], skipped="Dependabot alerts are disabled").actions, ())

    def test_a_github_issue_records_the_flags_before_the_comment_and_close(self) -> None:
        existing = issue([alert(1)], repo=SYNCED, tracker="github", state="open")
        actions = plan(self.closed(), [existing], repo=SYNCED).actions
        self.assertEqual([type(a) for a in actions], [UpdateBody, Comment, Cancel])
        self.assertEqual(sweep.parse_footer(actions[0].body).flags, frozenset({"notified", "canceled"}))

    def test_a_github_issue_whose_copy_is_started_is_commented_but_not_closed(self) -> None:
        copy = Copy("c1", "ABC-9", "started", False, True, 2, True)
        existing = issue([alert(1)], repo=SYNCED, tracker="github", state="open", copy=copy)
        actions = plan(self.closed(), [existing], repo=SYNCED).actions
        self.assertEqual([type(a) for a in actions], [UpdateBody, Comment])
        self.assertEqual(sweep.parse_footer(actions[0].body).flags, frozenset({"notified"}))

    def test_a_github_issue_with_an_assignee_is_commented_once(self) -> None:
        existing = issue([alert(1)], repo=SYNCED, tracker="github", state="open", assigned=True)
        first = plan(self.closed(), [existing], repo=SYNCED).actions
        self.assertEqual([type(a) for a in first], [UpdateBody, Comment])
        again = replace(existing, body=first[0].body, markers=frozenset({"notified"}))
        self.assertEqual(plan(self.closed(), [again], repo=SYNCED).actions, ())


class RecurrenceTest(unittest.TestCase):
    def test_a_package_the_app_canceled_gets_a_new_issue_that_follows_the_old_one(self) -> None:
        old = issue([alert(1)], state="canceled", markers=frozenset({"canceled"}))
        [create] = plan([alert(1)], [old]).actions
        self.assertEqual((type(create), create.previous), (Create, "ABC-1"))

    def test_a_github_recurrence_names_the_previous_issue_in_the_body(self) -> None:
        old = issue([alert(1)], repo=SYNCED, tracker="github", state="closed", body_args={"flags": ("canceled",)})
        [create] = plan([alert(1)], [old], repo=SYNCED).actions
        self.assertEqual(create.body.split("\n")[1], "Follows #5")

    def test_an_issue_a_person_closed_is_not_replaced_for_the_same_alert_numbers(self) -> None:
        old = issue([alert(1)], state="canceled")
        self.assertEqual(plan([alert(1)], [old]).actions, ())

    def test_an_issue_a_person_closed_is_replaced_for_a_new_alert_number(self) -> None:
        old = issue([alert(1)], state="completed")
        [create] = plan([alert(1), alert(2)], [old]).actions
        self.assertEqual(create.previous, "ABC-1")

    def test_the_newest_issue_by_creation_time_is_the_previous_one(self) -> None:
        older = issue([alert(1)], state="canceled", created_at="2026-09-01", markers=frozenset({"canceled"}))
        newer = replace(older, identifier="ABC-7", ref="id-7", created_at="2026-10-05")
        [create] = plan([alert(1)], [older, newer]).actions
        self.assertEqual(create.previous, "ABC-7")


class CopyDelegationTest(unittest.TestCase):
    def github(self, copy, flags=()) -> ExistingIssue:
        return issue([alert(1)], repo=SYNCED, tracker="github", state="open", copy=copy, body_args={"flags": flags})

    def test_a_present_copy_is_delegated_prioritized_and_recorded_in_the_footer(self) -> None:
        copy = Copy("c1", "ABC-9", "triage", False, False, 0, True)
        actions = plan([alert(1)], [self.github(copy)], repo=SYNCED).actions
        self.assertEqual([type(a) for a in actions], [DelegateCopy, UpdateBody])
        self.assertEqual(actions[0].priority, Priority.HIGH)
        self.assertEqual(sweep.parse_footer(actions[1].body).flags, frozenset({"copy-delegated"}))

    def test_a_recorded_delegation_is_never_repeated(self) -> None:
        copy = Copy("c1", "ABC-9", "triage", False, False, 2, True)
        self.assertEqual(plan([alert(1)], [self.github(copy, ("copy-delegated",))], repo=SYNCED).actions, ())

    def test_an_absent_copy_writes_nothing(self) -> None:
        self.assertEqual(plan([alert(1)], [self.github(None)], repo=SYNCED).actions, ())

    def test_a_copy_priority_changed_by_a_person_is_not_overwritten(self) -> None:
        copy = Copy("c1", "ABC-9", "triage", False, False, 4, False)
        [delegate, _] = plan([alert(1)], [self.github(copy)], repo=SYNCED).actions
        self.assertIsNone(delegate.priority)

    def test_a_later_priority_change_goes_to_the_copy(self) -> None:
        copy = Copy("c1", "ABC-9", "triage", False, True, 2, True)
        existing = self.github(copy, ("copy-delegated",))
        actions = plan([alert(1), alert(2, severity="critical")], [existing], repo=SYNCED).actions
        self.assertEqual([(type(a), getattr(a, "target", None)) for a in actions], [(SetPriority, "c1"), (UpdateBody, None)])


class ReportTest(unittest.TestCase):
    def test_each_unbuildable_repository_with_open_alerts_is_reported_with_its_count(self) -> None:
        other = sweep.classify_repo("example-org/other", "private", SETTINGS)
        open_source = sweep.classify_repo("example-org/oss", "public", SETTINGS)
        internal = sweep.classify_repo("example-org/inner", "internal", SETTINGS)
        reads = [RepoRead(r, (alert(1), alert(2), alert(3, state="fixed"))) for r in (other, open_source, internal)]
        result = sweep.plan(reads, [], KEV)
        self.assertEqual(result.actions, ())
        self.assertEqual(
            [(e.repository, e.kind, e.open_alerts) for e in result.report],
            [("example-org/other", "unbuildable", 2), ("example-org/oss", "unbuildable", 2), ("example-org/inner", "unbuildable", 2)],
        )
        self.assertEqual(internal.buildability, Buildability.UNKNOWN)

    def test_an_unbuildable_repository_without_open_alerts_is_not_reported(self) -> None:
        other = sweep.classify_repo("example-org/other", "private", SETTINGS)
        self.assertEqual(sweep.plan([RepoRead(other, (alert(1, state="fixed"),))], [], KEV).report, ())

    def test_a_skipped_repository_is_reported_with_its_reason(self) -> None:
        [entry] = plan([], skipped="the label 'Theme' is missing").report
        self.assertEqual((entry.kind, entry.reason), ("skipped", "the label 'Theme' is missing"))

    def test_repository_names_are_compared_ignoring_case(self) -> None:
        self.assertEqual(sweep.classify_repo(DEFAULT.upper(), "private", SETTINGS).buildability, Buildability.PRIVATE_DEFAULT)
        self.assertEqual(sweep.classify_repo(PUBLIC.upper(), "public", SETTINGS).buildability, Buildability.PUBLIC_SYNCED)

    def test_a_private_synced_repository_and_a_public_default_are_not_buildable(self) -> None:
        self.assertEqual(sweep.classify_repo(PUBLIC, "private", SETTINGS).buildability, Buildability.UNBUILDABLE)
        self.assertEqual(sweep.classify_repo(DEFAULT, "public", SETTINGS).buildability, Buildability.UNBUILDABLE)


class OwnershipTest(unittest.TestCase):
    def linear_raw(self, description: str, created_by_me: bool = True) -> dict:
        return {
            "id": "i", "identifier": "ABC-1", "createdAt": "2026-10-01", "description": description, "createdByMe": created_by_me,
            "stateType": "triage", "assigned": False, "priority": 2, "comments": [], "related": [],
        }

    def test_a_linear_issue_needs_the_origin_line_and_the_app_as_creator(self) -> None:
        line = sweep.origin_line(Key(DEFAULT, "npm", "lodash"), DEFAULT)
        self.assertIsNotNone(sweep.linear_issue(self.linear_raw(line), True))
        self.assertIsNone(sweep.linear_issue(self.linear_raw(line, created_by_me=False), True))
        self.assertIsNone(sweep.linear_issue(self.linear_raw("Remediate lodash Dependabot alerts"), True))

    def test_a_github_issue_needs_the_app_as_author_and_is_not_a_pull_request(self) -> None:
        line = sweep.origin_line(Key(PUBLIC, "npm", "lodash"), PUBLIC)
        raw = {"number": 3, "state": "open", "body": line, "created_at": "x", "html_url": "u", "user": {"login": "example-app[bot]"}}
        self.assertIsNotNone(sweep.github_issue(raw, "example-app[bot]", None))
        self.assertIsNone(sweep.github_issue({**raw, "user": {"login": "someone"}}, "example-app[bot]", None))
        self.assertIsNone(sweep.github_issue({**raw, "pull_request": {}}, "example-app[bot]", None))
        self.assertIsNone(sweep.github_issue({**raw, "body": "no origin"}, "example-app[bot]", None))


class SettingsTest(unittest.TestCase):
    FACTORY = {
        "dependabot_sweep": {"team_key": "ABC", "label": "Theme"}, "github": {"login": "example-app[bot]"},
        "repositories": [
            {"full_name": "Example-Org/Example-Repo", "path": "/a", "routing": "default"},
            {"full_name": PUBLIC, "path": "/b", "routing": "synced_github"},
        ],
    }

    def test_a_valid_section_parses_with_lowercase_repositories(self) -> None:
        self.assertEqual(sweep.parse_settings(self.FACTORY), SETTINGS)

    def test_an_absent_or_empty_section_is_not_configured(self) -> None:
        self.assertEqual([sweep.configured({}), sweep.configured({"dependabot_sweep": {}}), sweep.configured(self.FACTORY)], [False, False, True])

    def test_each_malformed_field_is_a_problem(self) -> None:
        bad = {**self.FACTORY, "dependabot_sweep": {"team_key": "abc", "label": "", "extra": 1}}
        self.assertEqual(sweep.settings_problems(bad), [
            "dependabot_sweep.team_key 'abc' must be a Linear team key such as ABC",
            "dependabot_sweep.label '' must be a non-empty label name",
            "dependabot_sweep has unknown fields: extra",
        ])

    def test_the_section_needs_the_app_login_and_one_default_repository(self) -> None:
        bad = {"dependabot_sweep": {"team_key": "ABC", "label": "Theme"}, "repositories": []}
        self.assertEqual(len(sweep.settings_problems(bad)), 2)
        with self.assertRaises(sweep.SettingsError):
            sweep.parse_settings(bad)


class StateTest(unittest.TestCase):
    def test_an_advisory_line_names_the_repository_and_its_count_or_reason(self) -> None:
        state = sweep.state_document((
            sweep.ReportEntry("example-org/other", "unbuildable", 32, "private repository that is not the default repository"),
            sweep.ReportEntry("example-org/off", "skipped", 0, "Dependabot alerts are disabled or the repository is not found"),
        ), "2026-11-03T12:00:00+00:00")
        self.assertEqual(sweep.advisory_lines(state), [
            "example-org/other is not swept: 32 open Dependabot alerts; private repository that is not the default repository",
            "example-org/off was skipped: no open Dependabot alerts read; Dependabot alerts are disabled or the repository is not found",
        ])


if __name__ == "__main__":
    unittest.main()
