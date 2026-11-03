from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest

from tests import load_plugin

plugin = load_plugin()
opened_sessions = plugin.opened_sessions

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)


class OpenedSessionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = os.path.join(tmp.name, "opened-sessions")

    def test_a_recorded_session_is_found_and_another_is_not(self):
        opened_sessions.record(self.directory, "session-1", NOW)
        self.assertEqual(
            (opened_sessions.recorded(self.directory, "session-1"), opened_sessions.recorded(self.directory, "session-2")),
            (True, False),
        )

    def test_a_session_id_cannot_leave_the_store(self):
        opened_sessions.record(self.directory, "../../escape", NOW)
        self.assertEqual(len(os.listdir(self.directory)), 1)
        self.assertEqual(os.listdir(os.path.dirname(self.directory)), ["opened-sessions"])

    def test_no_store_records_nothing_and_finds_nothing(self):
        opened_sessions.record(None, "session-1", NOW)
        self.assertFalse(opened_sessions.recorded(None, "session-1"))

    def test_a_missing_store_finds_nothing(self):
        self.assertFalse(opened_sessions.recorded(self.directory, "session-1"))

    def test_recording_prunes_entries_older_than_a_day(self):
        opened_sessions.record(self.directory, "old", NOW)
        opened_sessions.record(self.directory, "recent", NOW)
        for session_id, age in (("old", dt.timedelta(days=1, minutes=1)), ("recent", dt.timedelta(hours=23))):
            stamp = (NOW - age).timestamp()
            os.utime(opened_sessions._path(self.directory, session_id), (stamp, stamp))
        opened_sessions.record(self.directory, "new", NOW)
        self.assertEqual(
            [opened_sessions.recorded(self.directory, s) for s in ("old", "recent", "new")], [False, True, True]
        )

    def test_the_store_lives_under_hermes_home(self):
        self.assertEqual(
            opened_sessions.store_dir({"HERMES_HOME": "/h"}), "/h/plugin-data/linear-agent-session/opened-sessions"
        )
        self.assertIsNone(opened_sessions.store_dir({}))


if __name__ == "__main__":
    unittest.main()
