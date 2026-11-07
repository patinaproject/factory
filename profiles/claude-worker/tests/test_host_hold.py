from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.support import SCRIPTS

sys.path.insert(0, str(SCRIPTS))
import host_hold  # noqa: E402
from host_hold import HoldList  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=10)


class Config:

    def __init__(self, root: Path, fail: bool = False, key: str | None = None) -> None:
        self.root = root
        self.fail = fail
        self.key = key
        self.calls: list = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        seen = json.loads((self.root / host_hold.HOLDS_FILE).read_text())
        if args[0] == "get":
            if self.key is None:
                return subprocess.CompletedProcess(args, 1, "", "Config key not set: kanban.dispatch_profiles")
            return subprocess.CompletedProcess(args, 0, self.key, "")
        self.calls.append((list(args), sorted(seen["holds"]), seen["applied"]))
        return subprocess.CompletedProcess(args, 1 if self.fail else 0, "", "boom" if self.fail else "")


class HostHoldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.config = Config(self.root)
        self.clock = NOW
        self.holds = HoldList(self.root, self.config, lambda: self.clock)

    def file(self) -> dict:
        return json.loads((self.root / host_hold.HOLDS_FILE).read_text())

    def test_add_sets_the_key_before_it_returns_and_records_applied(self) -> None:
        created = self.holds.add("host_load", "load 20", LATER)

        self.assertTrue(created)
        self.assertEqual(self.config.calls, [(["set", "kanban.dispatch_profiles", "[]"], ["host_load"], False)])
        self.assertEqual(self.file(), {"holds": {"host_load": {"reason": "load 20", "until": "2026-11-06T12:10:00Z"}}, "applied": True})

    def test_a_key_a_person_set_is_left_alone_and_never_unset(self) -> None:
        for key in ('["claude-worker"]', "[]"):
            with self.subTest(key=key):
                self.config.key = key
                self.config.calls.clear()

                self.holds.add("host_load", "load 20", LATER)

                self.assertEqual(self.config.calls, [])
                self.assertEqual(self.file(), {"holds": {"host_load": {"reason": "load 20", "until": "2026-11-06T12:10:00Z"}}, "applied": False})

                self.holds.remove("host_load")

                self.assertEqual(self.config.calls, [])
                self.assertEqual(self.file(), {"holds": {}, "applied": False})

    def test_a_null_key_is_absent_so_the_list_sets_it(self) -> None:
        self.config.key = "null"

        self.holds.add("host_load", "load 20", LATER)

        self.assertEqual([call[0] for call in self.config.calls], [["set", "kanban.dispatch_profiles", "[]"]])
        self.assertTrue(self.file()["applied"])

    def test_adopt_makes_the_list_own_a_key_a_caller_set_so_the_last_removal_unsets_it(self) -> None:
        self.config.key = "[]"
        self.holds.add("host_load", "load 20", LATER)

        self.holds.adopt()
        self.holds.remove("host_load")

        self.assertEqual([call[0] for call in self.config.calls], [["unset", "kanban.dispatch_profiles"]])
        self.assertEqual(self.file(), {"holds": {}, "applied": False})

    def test_removing_one_of_two_holders_leaves_the_other_and_the_key(self) -> None:
        self.holds.add("host_load", "load 20", LATER)
        self.holds.add("auto-deploy", "deploying", NOW + timedelta(hours=1))
        for leaving, staying in (("host_load", "auto-deploy"), ("auto-deploy", "host_load")):
            with self.subTest(leaving=leaving):
                self.holds.add("host_load", "load 20", LATER)
                self.holds.add("auto-deploy", "deploying", NOW + timedelta(hours=1))
                before = self.file()["holds"][staying]
                self.config.calls.clear()

                self.holds.remove(leaving)

                self.assertEqual(self.file()["holds"], {staying: before})
                self.assertTrue(self.file()["applied"])
                self.assertEqual(self.config.calls, [])
                self.holds.remove(staying)

    def test_the_last_removal_unsets_the_key(self) -> None:
        self.holds.add("host_load", "load 20", LATER)
        self.config.calls.clear()

        self.holds.remove("host_load")

        self.assertEqual(self.config.calls, [(["unset", "kanban.dispatch_profiles"], [], True)])
        self.assertEqual(self.file(), {"holds": {}, "applied": False})

    def test_a_key_the_list_did_not_set_is_never_unset(self) -> None:
        (self.root / host_hold.HOLDS_FILE).write_text(json.dumps({"holds": {}, "applied": False}))

        self.holds.apply()
        self.holds.remove("host_load")

        self.assertEqual(self.config.calls, [])

    def test_expire_removes_only_entries_past_their_until(self) -> None:
        self.holds.add("host_load", "load 20", NOW - timedelta(seconds=1))
        self.holds.add("auto-deploy", "deploying", NOW + timedelta(hours=1))

        gone = self.holds.expire()

        self.assertEqual((gone, sorted(self.file()["holds"]), self.file()["applied"]), (["host_load"], ["auto-deploy"], True))

    def test_a_failed_config_call_leaves_applied_false_so_the_next_apply_retries(self) -> None:
        self.config.fail = True
        with self.assertRaisesRegex(host_hold.HoldError, "hermes config set kanban.dispatch_profiles exited 1: boom"):
            self.holds.add("host_load", "load 20", LATER)
        self.assertEqual(self.file()["applied"], False)

        self.config.fail = False
        self.holds.apply()

        self.assertTrue(self.file()["applied"])

    def test_the_file_reads_with_only_json_and_pathlib(self) -> None:
        self.holds.add("host_load", "load 20", LATER)

        data = json.loads(Path(self.root / "dispatch-holds.json").read_text())

        self.assertEqual(data["holds"]["host_load"]["until"], "2026-11-06T12:10:00Z")
        self.assertIs(data["applied"], True)

    def test_the_log_gets_one_line_for_a_new_entry_none_for_a_refresh_and_one_for_a_removal(self) -> None:
        readings = {"one": 20.0, "five": 18.0, "threshold": 10.0}
        self.holds.add("host_load", "load 20", LATER, readings)
        self.holds.add("host_load", "load 21", LATER, readings)
        self.holds.remove("host_load", readings)
        self.holds.remove("host_load", readings)

        lines = [json.loads(line) for line in (self.root / host_hold.LOG_FILE).read_text().splitlines()]

        self.assertEqual(
            [(line["event"], line["holder"], line["time"], line["load"]) for line in lines],
            [("add", "host_load", "2026-11-06T12:00:00Z", readings), ("remove", "host_load", "2026-11-06T12:00:00Z", readings)],
        )

    def test_held_seconds_sums_closed_intervals_and_counts_an_open_one_to_now_inside_the_window(self) -> None:
        for event, at in (("add", -30 * 3600), ("remove", -23 * 3600), ("add", -2 * 3600), ("remove", -3600), ("add", -600)):
            self.clock = NOW + timedelta(seconds=at)
            with (self.root / host_hold.LOG_FILE).open("a") as log:
                log.write(json.dumps({"time": host_hold.stamp(self.clock), "event": event, "holder": "host_load"}) + "\n")

        self.assertEqual(host_hold.held_seconds(self.root, "host_load", NOW), 3600 + 3600 + 600)

    def test_threshold_defaults_to_one_and_a_half_per_cpu_and_compares_either_average(self) -> None:
        for one, five, over in ((10, 10, False), (10.1, 1, True), (1, 10.1, True)):
            with self.subTest(one=one, five=five):
                reading = host_hold.read_load(None, lambda: (one, five, 0), lambda: 8 / 1.2)

                self.assertEqual(reading.threshold, 10.0)
                self.assertEqual(reading.over, over)

    def test_an_unreadable_load_holds_nothing_and_is_logged(self) -> None:
        def broken():
            raise OSError("no load")

        self.assertIsNone(host_hold.observe(self.holds, None, broken, lambda: 8))
        self.assertIsNone(host_hold.observe(self.holds, None, lambda: (1, 1, 1), lambda: None))
        self.assertIsNone(host_hold.refresh(self.holds, None, broken, lambda: 8))

        events = [json.loads(line)["event"] for line in (self.root / host_hold.LOG_FILE).read_text().splitlines()]
        self.assertEqual(events, ["load-unreadable"] * 3)
        self.assertFalse((self.root / host_hold.HOLDS_FILE).exists())

    def test_test_workers_split_the_cpus_between_the_live_runs(self) -> None:
        self.assertEqual([host_hold.test_workers(18, runs) for runs in (1, 4, 32)], [18, 4, 1])


if __name__ == "__main__":
    unittest.main()
