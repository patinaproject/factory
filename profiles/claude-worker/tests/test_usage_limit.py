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
import usage_limit  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)
BACKOFF = usage_limit.DEFAULT_BACKOFF
# Each process makes one change through the module, with a config call that sets the key and finds it unset.
WRITER = """
import subprocess, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, sys.argv[1])
import host_hold, usage_limit
from pathlib import Path

def config(*args):
    return subprocess.CompletedProcess(args, 1 if args[0] == "get" else 0, "", "Config key not set")

holds = host_hold.HoldList(Path(sys.argv[2]), config)
if sys.argv[3] == "hit":
    usage_limit.record_hit(holds, None, usage_limit.DEFAULT_BACKOFF)
else:
    holds.add(sys.argv[3], "test", datetime.now(timezone.utc) + timedelta(hours=1))
"""


def config(*args: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, 1 if args[0] == "get" else 0, "", "Config key not set")


class UsageLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.clock = NOW
        self.holds = host_hold.HoldList(self.root, config, lambda: self.clock)

    def until(self) -> str:
        return json.loads((self.root / host_hold.HOLDS_FILE).read_text())["holds"]["usage_limit"]["until"]

    def episode(self) -> dict:
        return json.loads((self.root / usage_limit.EPISODE_FILE).read_text())

    def release(self) -> None:
        """Move the clock past the entry's end and expire it, as the next dispatch-triage tick does."""
        self.clock = host_hold.parse(self.until()) + timedelta(seconds=1)
        self.holds.expire()

    def test_a_reset_time_holds_until_it_plus_two_minutes_and_one_past_five_hours_holds_five_hours(self) -> None:
        for reset, expected in ((NOW + timedelta(hours=1), "2026-11-06T13:02:00Z"), (NOW + timedelta(hours=7), "2026-11-06T17:00:00Z")):
            with self.subTest(reset=reset):
                (self.root / host_hold.HOLDS_FILE).unlink(missing_ok=True)
                (self.root / usage_limit.EPISODE_FILE).unlink(missing_ok=True)

                self.assertEqual(host_hold.stamp(usage_limit.record_hit(self.holds, reset, BACKOFF)), expected)
                self.assertEqual(self.until(), expected)

    def test_hits_back_off_15_then_30_minutes_never_past_5_hours_and_a_clean_run_starts_over(self) -> None:
        usage_limit.record_hit(self.holds, None, BACKOFF)
        self.assertEqual(self.until(), "2026-11-06T12:15:00Z")
        self.assertEqual(usage_limit.next_notice(self.holds)[0], "start")

        self.clock += timedelta(minutes=5)
        usage_limit.record_hit(self.holds, None, BACKOFF)
        self.assertEqual((self.until(), self.episode()["hits"]), ("2026-11-06T12:15:00Z", 1))

        self.release()
        released = self.clock
        usage_limit.record_hit(self.holds, None, BACKOFF)
        self.assertEqual(host_hold.parse(self.until()) - released, timedelta(minutes=30))
        self.assertIsNone(usage_limit.next_notice(self.holds))

        lengths = []
        for _ in range(6):
            self.release()
            start = self.clock
            usage_limit.record_hit(self.holds, None, BACKOFF)
            lengths.append(host_hold.parse(self.until()) - start)
        self.assertEqual(lengths[:3], [timedelta(hours=1), timedelta(hours=2), timedelta(hours=4)])
        self.assertEqual(lengths[3:], [timedelta(hours=5)] * 3)

        self.release()
        clean = {"outcome": "completed", "started_at": int(self.clock.timestamp()), "ended_at": int(self.clock.timestamp()) + 60}
        self.episode_cursor(clean["ended_at"] - 120)
        self.clock += timedelta(minutes=2)
        usage_limit.read_board(self.holds, lambda since: [clean], BACKOFF)
        self.assertEqual(self.episode()["hits"], 0)

        start = self.clock
        usage_limit.record_hit(self.holds, None, BACKOFF)
        self.assertEqual(host_hold.parse(self.until()) - start, timedelta(minutes=15))

    def episode_cursor(self, cursor: int) -> None:
        path = self.root / usage_limit.EPISODE_FILE
        path.write_text(json.dumps({**json.loads(path.read_text()), "cursor": cursor}))

    def test_a_clean_run_that_started_before_the_release_keeps_the_episode(self) -> None:
        usage_limit.record_hit(self.holds, None, BACKOFF)
        self.release()
        early = {"outcome": "completed", "started_at": int(NOW.timestamp()) + 60, "ended_at": int(self.clock.timestamp())}
        self.episode_cursor(early["ended_at"] - 1)

        usage_limit.read_board(self.holds, lambda since: [early], BACKOFF)

        self.assertEqual(self.episode()["hits"], 1)

    def test_with_no_cursor_the_board_sets_it_to_now_and_reads_no_run(self) -> None:
        def ended_since(since: int) -> list:
            raise AssertionError("read the board before a cursor existed")

        self.assertIsNone(usage_limit.read_board(self.holds, ended_since, BACKOFF))

        self.assertEqual(self.episode()["cursor"], int(NOW.timestamp()))
        self.assertFalse((self.root / host_hold.HOLDS_FILE).exists())

    def test_one_episode_has_one_start_and_one_release_notice(self) -> None:
        self.assertIsNone(usage_limit.next_notice(self.holds))
        usage_limit.record_hit(self.holds, None, BACKOFF)

        self.assertEqual(usage_limit.next_notice(self.holds), ("start", NOW + timedelta(minutes=15)))
        self.assertIsNone(usage_limit.next_notice(self.holds))
        self.release()
        self.assertIsNone(usage_limit.next_notice(self.holds))
        clean = {"outcome": "completed", "started_at": int(self.clock.timestamp()), "ended_at": int(self.clock.timestamp()) + 60}
        self.episode_cursor(clean["ended_at"] - 120)
        usage_limit.read_board(self.holds, lambda since: [clean], BACKOFF)
        self.assertEqual(usage_limit.next_notice(self.holds), ("release", NOW + timedelta(minutes=15)))
        self.assertIsNone(usage_limit.next_notice(self.holds))

    def test_a_hit_after_the_entry_ends_holds_again_with_no_release_notice_between(self) -> None:
        usage_limit.record_hit(self.holds, None, BACKOFF)
        self.assertEqual(usage_limit.next_notice(self.holds)[0], "start")
        self.release()
        self.assertIsNone(usage_limit.next_notice(self.holds))

        usage_limit.record_hit(self.holds, None, BACKOFF)

        self.assertIsNone(usage_limit.next_notice(self.holds))
        self.assertEqual(self.episode()["hits"], 2)

    def test_a_reset_time_that_is_not_a_date_gives_none(self) -> None:
        for text in ("limit resets at 2026-13-45T00:00Z", "resets in 99999999999999999 hours", "retry after 99999999999999999999 seconds"):
            with self.subTest(text=text):
                self.assertIsNone(usage_limit.reset_time(text, NOW))

    def test_a_usage_limit_result_needs_is_error_and_a_rate_limit_text(self) -> None:
        for result, expected in (
            ({"is_error": True, "result": 'API Error: 429 {"type": "error", "error": {"type": "rate_limit_error"}}'}, True),
            ({"is_error": True, "result": "All credentials are cooling down via provider claude. Please try again later."}, True),
            ({"is_error": False, "result": "Fixed the 429 rate_limit_error handling."}, False),
            ({"is_error": True, "result": "API Error: 500 internal server error"}, False),
            ({"result": "API Error: 429 rate_limit_error"}, False),
        ):
            with self.subTest(result=result):
                self.assertIs(usage_limit.is_limit(result), expected)

    def test_reset_times_come_from_resets_at_resets_in_and_retry_after(self) -> None:
        for text, expected in (
            ("rate_limit_error; resets at 2026-11-06T14:30:00Z", datetime(2026, 11, 6, 14, 30, tzinfo=timezone.utc)),
            ("rate_limit_error; resets in 1h 30m", NOW + timedelta(minutes=90)),
            ("Claude AI usage limit reached|1793973600", datetime.fromtimestamp(1793973600, timezone.utc)),
            ("429 rate_limit_error, retry after 120 seconds", NOW + timedelta(seconds=120)),
            ("cooling down, try again later", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(usage_limit.reset_time(text, NOW), expected)

    def test_an_episode_file_that_does_not_parse_raises_and_adds_no_entry(self) -> None:
        (self.root / usage_limit.EPISODE_FILE).write_text("{not json")

        with self.assertRaises(host_hold.HoldError):
            usage_limit.record_hit(self.holds, None, BACKOFF)
        self.assertFalse((self.root / host_hold.HOLDS_FILE).exists())

    def run_writers(self, *changes: str) -> None:
        procs = [
            subprocess.Popen([sys.executable, "-c", WRITER, str(SCRIPTS), str(self.root), change]) for change in changes
        ]
        self.assertEqual([proc.wait(timeout=60) for proc in procs], [0] * len(changes))

    def test_ten_simultaneous_hits_from_separate_processes_make_one_entry_one_hit_and_one_start_notice(self) -> None:
        self.run_writers(*["hit"] * 10)

        holds = host_hold.HoldList(self.root, config)
        self.assertEqual(sorted(holds.holders()), ["usage_limit"])
        self.assertEqual(self.episode()["hits"], 1)
        self.assertEqual(usage_limit.next_notice(holds)[0], "start")
        self.assertIsNone(usage_limit.next_notice(holds))

    def test_concurrent_writers_that_each_add_their_own_entry_keep_every_entry(self) -> None:
        names = [f"holder_{index}" for index in range(8)]
        self.run_writers(*names, "hit")

        self.assertEqual(sorted(host_hold.HoldList(self.root, config).holders()), sorted([*names, "usage_limit"]))

    def test_the_hold_list_reads_with_only_the_standard_library_and_no_hermes(self) -> None:
        usage_limit.record_hit(self.holds, None, BACKOFF)
        reader = (
            "import json, sys; from datetime import datetime; "
            "entry = json.load(open(sys.argv[1]))['holds']['usage_limit']; "
            "print(entry['reason'], datetime.fromisoformat(entry['until'].replace('Z', '+00:00')).isoformat()); "
            "assert not any(name.startswith(('hermes', 'host_hold', 'usage_limit')) for name in sys.modules)"
        )

        proc = subprocess.run(
            [sys.executable, "-I", "-c", reader, str(self.root / host_hold.HOLDS_FILE)], capture_output=True, text=True,
        )

        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(proc.stdout, "the Claude usage limit was reached 2026-11-06T12:15:00+00:00\n")


if __name__ == "__main__":
    unittest.main()
