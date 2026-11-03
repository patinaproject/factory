from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "skills" / "setup-factory" / "scripts" / "linear-route"

_loader = importlib.machinery.SourceFileLoader("linear_route", str(SCRIPT))
linear_route = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(linear_route)

_render_spec = importlib.util.spec_from_file_location("render", REPO / "templates" / "render.py")
render = importlib.util.module_from_spec(_render_spec)
_render_spec.loader.exec_module(render)
_tests_spec = importlib.util.spec_from_file_location("render_tests", REPO / "templates" / "tests" / "test_render.py")
render_tests = importlib.util.module_from_spec(_tests_spec)
_tests_spec.loader.exec_module(render_tests)

SILENT = "[SILENT]"


def issue_update(issue="issue-1", created="2026-11-03T10:00:00.000Z", updated_from=None, timestamp=1):
    payload = {
        "type": "Issue", "action": "update", "createdAt": created, "webhookTimestamp": timestamp,
        "data": {"id": issue, "identifier": "ABC-1"},
    }
    if updated_from is not None:
        payload["updatedFrom"] = updated_from
    return payload


def session_created(session="session-1", created="2026-11-03T10:00:00.000Z", timestamp=1):
    return {
        "type": "AgentSessionEvent", "action": "created", "createdAt": created, "webhookTimestamp": timestamp,
        "agentSession": {"id": session, "issue": {"id": "issue-1"}},
    }


def session_prompted(session="session-1", activity="activity-1", created="2026-11-03T10:00:00.000Z", timestamp=1):
    payload = {**session_created(session, created, timestamp), "action": "prompted"}
    if activity is not None:
        payload["agentActivity"] = {"id": activity, "content": {"type": "prompt", "body": "go"}}
    return payload


def comment(comment_id="comment-1", created="2026-11-03T10:00:00.000Z", timestamp=1):
    return {
        "type": "Comment", "action": "create", "createdAt": created, "webhookTimestamp": timestamp,
        "data": {"id": comment_id, "issueId": "issue-1"},
    }


class RouteCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.home)
        (self.home / "scripts").mkdir()
        shutil.copy(SCRIPT, self.home / "scripts" / "linear-route")

    def run_script(self, stdin, home=None):
        """Run the script the way Hermes does: a fresh interpreter, the payload on stdin."""
        script = (home or self.home) / "scripts" / "linear-route"
        done = subprocess.run([sys.executable, str(script)], input=stdin, capture_output=True, text=True, cwd=script.parent)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def deliver(self, payload):
        out = self.run_script(json.dumps(payload))
        return out if out == SILENT else json.loads(out)


class RedeliveryTest(RouteCase):
    def test_a_redelivered_event_of_each_shape_is_dropped_even_after_a_restart(self):
        shapes = {
            "Issue": lambda timestamp: issue_update(updated_from={"labelIds": []}, timestamp=timestamp),
            "AgentSessionEvent created": lambda timestamp: session_created(timestamp=timestamp),
            "AgentSessionEvent prompted": lambda timestamp: session_prompted(timestamp=timestamp),
            "Comment": lambda timestamp: comment(timestamp=timestamp),
        }
        for name, make in shapes.items():
            with self.subTest(name):
                self.home = Path(tempfile.mkdtemp()).resolve()
                self.addCleanup(shutil.rmtree, self.home)
                (self.home / "scripts").mkdir()
                shutil.copy(SCRIPT, self.home / "scripts" / "linear-route")
                self.assertNotEqual(self.deliver(make(1)), SILENT)
                self.assertEqual(self.deliver(make(2)), SILENT)
                self.assertEqual(self.deliver(make(3)), SILENT)

    def test_a_different_webhook_timestamp_does_not_make_an_event_new(self):
        first = self.deliver(comment(timestamp=111))
        second = self.deliver(comment(timestamp=999999))
        self.assertEqual((first["data"]["id"], second), ("comment-1", SILENT))

    def test_distinct_events_are_all_kept(self):
        cases = {
            "two updates of one issue": (
                issue_update(created="2026-11-03T10:00:00.000Z"), issue_update(created="2026-11-03T10:00:05.000Z"),
            ),
            "two prompts of one session": (
                session_prompted(activity="activity-1"), session_prompted(activity="activity-2"),
            ),
            "two comments": (comment("comment-1"), comment("comment-2")),
            "two sessions created": (session_created("session-1"), session_created("session-2")),
        }
        for name, (first, second) in cases.items():
            with self.subTest(name):
                self.home = Path(tempfile.mkdtemp()).resolve()
                self.addCleanup(shutil.rmtree, self.home)
                (self.home / "scripts").mkdir()
                shutil.copy(SCRIPT, self.home / "scripts" / "linear-route")
                self.assertNotEqual(self.deliver(first), SILENT)
                self.assertNotEqual(self.deliver(second), SILENT)

    def test_a_prompted_event_without_an_activity_id_is_never_deduplicated(self):
        payload = session_prompted(activity=None)
        self.assertEqual(self.deliver(payload), payload)
        self.assertEqual(self.deliver(payload), payload)

    def test_an_unknown_shape_passes_through_undeduplicated(self):
        payload = {"type": "Reaction", "action": "create", "createdAt": "2026-11-03T10:00:00.000Z", "data": {"id": "r-1"}}
        self.assertEqual(self.deliver(payload), payload)
        self.assertEqual(self.deliver(payload), payload)

    def test_an_event_without_a_created_at_is_never_deduplicated(self):
        payload = comment()
        del payload["createdAt"]
        self.assertEqual(self.deliver(payload), payload)
        self.assertEqual(self.deliver(payload), payload)

    def test_the_seen_window_is_seven_hours(self):
        payload = json.dumps(comment())
        start = 1_000_000.0
        linear_route.route(payload, self.home, start)
        self.assertEqual(linear_route.route(payload, self.home, start + 7 * 3600 - 1), linear_route.SILENT)
        self.assertEqual(json.loads(linear_route.route(payload, self.home, start + 14 * 3600)), comment())


class BurstTest(RouteCase):
    def test_the_payload_carries_the_union_with_the_earliest_value_of_each_key(self):
        self.deliver(issue_update(created="2026-11-03T10:00:00.000Z", updated_from={"delegateId": "first-delegate"}))
        self.deliver(issue_update(created="2026-11-03T10:00:01.000Z", updated_from={"delegateId": "second-delegate"}))
        newest = self.deliver(issue_update(created="2026-11-03T10:00:02.000Z", updated_from={"labelIds": ["a"]}))
        self.assertEqual(newest["updatedFrom"], {"delegateId": "first-delegate", "labelIds": ["a"]})
        self.assertEqual(newest["createdAt"], "2026-11-03T10:00:02.000Z")
        self.assertEqual(newest["factoryRoute"], {"issue": "issue-1", "seq": 3})

    def test_an_update_without_updated_from_gets_the_burst_keys(self):
        self.deliver(issue_update(created="2026-11-03T10:00:00.000Z", updated_from={"stateId": "s"}))
        newest = self.deliver(issue_update(created="2026-11-03T10:00:01.000Z"))
        self.assertEqual(newest["updatedFrom"], {"stateId": "s"})

    def test_two_issues_keep_their_own_keys(self):
        self.deliver(issue_update("issue-1", updated_from={"delegateId": "d"}))
        other = self.deliver(issue_update("issue-2", updated_from={"labelIds": []}))
        self.assertEqual(other["updatedFrom"], {"labelIds": []})
        self.assertEqual(other["factoryRoute"]["issue"], "issue-2")

    def test_an_entry_older_than_fifteen_minutes_is_forgotten(self):
        start = 1_000_000.0
        linear_route.route(json.dumps(issue_update(created="a", updated_from={"delegateId": "old"})), self.home, start)
        recent = json.loads(linear_route.route(
            json.dumps(issue_update(created="b", updated_from={"labelIds": []})), self.home, start + 14 * 60,
        ))
        later = json.loads(linear_route.route(
            json.dumps(issue_update(created="c", updated_from={"stateId": "s"})), self.home, start + 16 * 60,
        ))
        self.assertEqual(recent["updatedFrom"], {"delegateId": "old", "labelIds": []})
        self.assertEqual(later["updatedFrom"], {"labelIds": [], "stateId": "s"})

    def test_a_stale_burst_on_another_issue_is_dropped_from_the_store(self):
        start = 1_000_000.0
        linear_route.route(json.dumps(issue_update(issue="issue-2", created="a", updated_from={"stateId": "s"})), self.home, start)
        linear_route.route(json.dumps(issue_update(created="b", updated_from={"labelIds": []})), self.home, start + 16 * 60)
        stored = json.loads((self.home / "linear-route" / "bursts.json").read_text())
        self.assertEqual(list(stored["issues"]), ["issue-1"])

    def test_session_events_and_comments_are_unchanged_and_unstamped(self):
        for payload in (session_created(), session_prompted(), comment()):
            with self.subTest(payload["type"], action=payload["action"]):
                self.assertEqual(self.deliver(copy.deepcopy(payload)), payload)

    def test_a_redelivered_update_does_not_add_to_the_burst(self):
        self.deliver(issue_update(created="a", updated_from={"delegateId": "d"}))
        self.assertEqual(self.deliver(issue_update(created="a", updated_from={"delegateId": "d"}, timestamp=2)), SILENT)
        newest = self.deliver(issue_update(created="b", updated_from={"labelIds": []}))
        self.assertEqual(newest["factoryRoute"]["seq"], 2)


class FailOpenTest(RouteCase):
    def test_text_that_is_not_a_json_object_passes_unchanged(self):
        for text in ("not json {", "[1, 2]", "", "null"):
            with self.subTest(text):
                self.assertEqual(self.run_script(text), text)

    def test_a_corrupt_store_passes_the_original_payload(self):
        store = self.home / "linear-route"
        store.mkdir()
        payload = issue_update(updated_from={"labelIds": []})
        for name in ("seen.json", "bursts.json"):
            with self.subTest(name):
                (store / "seen.json").write_text("{}")
                (store / "bursts.json").write_text("{}")
                (store / name).write_text("{not json")
                self.assertEqual(json.loads(self.run_script(json.dumps(payload))), payload)

    def test_a_store_that_is_not_an_object_passes_the_original_payload(self):
        store = self.home / "linear-route"
        store.mkdir()
        (store / "seen.json").write_text("[]")
        payload = comment()
        self.assertEqual(json.loads(self.run_script(json.dumps(payload))), payload)

    def test_an_unwritable_store_passes_the_original_payload(self):
        (self.home / "linear-route").write_text("a file where the store directory belongs")
        payload = issue_update(updated_from={"delegateId": "d"})
        self.assertEqual(json.loads(self.run_script(json.dumps(payload))), payload)

    def test_a_read_only_store_directory_passes_the_original_payload(self):
        store = self.home / "linear-route"
        store.mkdir()
        store.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(store.chmod, stat.S_IRWXU)
        payload = issue_update(updated_from={"delegateId": "d"})
        out = self.run_script(json.dumps(payload))
        self.assertEqual(json.loads(out), payload)


def rendered_coalesce_key() -> str:
    return render.build_routes(render_tests.SETTINGS)["linear"]["coalesce"]["key"]


def lookup(payload, dotted):
    for part in dotted.split("."):
        payload = payload.get(part) if isinstance(payload, dict) else None
    return payload


def settle(arrivals, key):
    """Hermes' coalescer: a payload without the key dispatches at once, and the newest arrival of a group dispatches alone."""
    dispatched, groups = [], {}
    for payload in arrivals:
        group = lookup(payload, key)
        if group is None:
            dispatched.append(payload)
        else:
            groups[group] = payload
    return dispatched + list(groups.values())


class CoalescedBurstTest(RouteCase):
    def settle(self, payloads):
        arrivals = [self.deliver(payload) for payload in payloads]
        return settle([a for a in arrivals if a != SILENT], rendered_coalesce_key())

    def test_three_updates_make_one_turn_with_the_newest_payload_and_the_burst_keys(self):
        turns = self.settle([
            issue_update(created="a", updated_from={"delegateId": "first"}),
            issue_update(created="b", updated_from={"stateId": "s"}),
            issue_update(created="c", updated_from={"labelIds": ["x"]}),
        ])
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["createdAt"], "c")
        self.assertEqual(turns[0]["updatedFrom"], {"delegateId": "first", "stateId": "s", "labelIds": ["x"]})

    def test_a_redelivered_earlier_event_does_not_displace_the_newest(self):
        turns = self.settle([
            issue_update(created="a", updated_from={"delegateId": "d"}),
            issue_update(created="b", updated_from={"labelIds": []}),
            issue_update(created="a", updated_from={"delegateId": "d"}, timestamp=2),
        ])
        self.assertEqual([turn["createdAt"] for turn in turns], ["b"])

    def test_session_events_dispatch_at_once_during_an_issue_burst(self):
        turns = self.settle([
            issue_update(created="a", updated_from={"delegateId": "d"}),
            session_prompted(activity="activity-1"),
            issue_update(created="b", updated_from={"labelIds": []}),
            session_created("session-2"),
            comment(),
        ])
        self.assertEqual(
            [(turn["type"], turn.get("action")) for turn in turns],
            [("AgentSessionEvent", "prompted"), ("AgentSessionEvent", "created"), ("Comment", "create"), ("Issue", "update")],
        )

    def test_updates_on_two_issues_each_make_one_turn(self):
        turns = self.settle([
            issue_update("issue-1", created="a", updated_from={"delegateId": "d1"}),
            issue_update("issue-2", created="b", updated_from={"delegateId": "d2"}),
            issue_update("issue-1", created="c", updated_from={"labelIds": []}),
            issue_update("issue-2", created="d", updated_from={"stateId": "s"}),
        ])
        self.assertEqual(
            sorted((turn["data"]["id"], turn["createdAt"], tuple(sorted(turn["updatedFrom"]))) for turn in turns),
            [("issue-1", "c", ("delegateId", "labelIds")), ("issue-2", "d", ("delegateId", "stateId"))],
        )



class HeldLockTest(RouteCase):
    def test_a_lock_held_past_the_limit_passes_the_payload_through(self):
        import fcntl
        payload = issue_update(updated_from={"labelIds": []})
        (self.home / "linear-route").mkdir()
        with open(self.home / "linear-route" / "lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            out = self.run_script(json.dumps(payload))
        self.assertEqual(json.loads(out), payload)


if __name__ == "__main__":
    unittest.main()
