from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
REPORT = REPO / "skills" / "setup-factory" / "scripts" / "report-usage-limit"
WORKER_SCRIPTS = REPO / "profiles" / "claude-worker" / "scripts"
# Prints the factory settings for `config get`, and records every other config call.
FAKE_HERMES = """#!{python}
import json, os, sys
if sys.argv[1:3] == ["config", "get"] and sys.argv[3] != "kanban.dispatch_profiles":
    print(json.dumps(json.load(open(os.environ["FAKE_SETTINGS"]))))
elif sys.argv[1:3] == ["config", "get"]:
    sys.exit("Config key not set: kanban.dispatch_profiles")
"""
HIT = """
import subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import host_hold, usage_limit

def config(*args):
    return subprocess.CompletedProcess(args, 1 if args[0] == "get" else 0, "", "Config key not set")

usage_limit.record_hit(host_hold.HoldList(Path(sys.argv[2]), config), None, usage_limit.DEFAULT_BACKOFF)
"""
REGINA = timezone(timedelta(hours=-6))


class ReportUsageLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        (self.root / "profiles" / "claude-worker").mkdir(parents=True)
        (self.root / "profiles" / "claude-worker" / "scripts").symlink_to(WORKER_SCRIPTS)
        self.hermes = Path(tmp.name) / "hermes"
        self.hermes.write_text(FAKE_HERMES.format(python=sys.executable))
        self.hermes.chmod(0o755)
        self.settings = Path(tmp.name) / "settings.json"
        self.settings.write_text(json.dumps({"dispatch_hold": {"time_zone": "America/Regina"}}))

    def report(self) -> list[str]:
        env = {**os.environ, "HERMES_HOME": str(self.root), "HERMES_BIN": str(self.hermes), "FAKE_SETTINGS": str(self.settings)}
        proc = subprocess.run([sys.executable, str(REPORT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.splitlines()

    def hits(self, count: int) -> None:
        procs = [subprocess.Popen([sys.executable, "-c", HIT, str(WORKER_SCRIPTS), str(self.root)]) for _ in range(count)]
        self.assertEqual([proc.wait(timeout=60) for proc in procs], [0] * count)

    def until(self) -> datetime:
        entry = json.loads((self.root / "dispatch-holds.json").read_text())["holds"]["usage_limit"]
        return datetime.strptime(entry["until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def end_entry(self) -> str:
        """Expire the entry and end the episode with a clean run, as dispatch-triage does, with the episode's release
        time moved into the past."""
        ended = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(second=0, microsecond=0)
        holds = self.root / "dispatch-holds.json"
        holds.write_text(json.dumps({"holds": {}, "applied": False}))
        path = self.root / "dispatch-holds.usage-limit.json"
        episode = json.loads(path.read_text())
        path.write_text(json.dumps({**episode, "hits": 0, "released_at": ended.strftime("%Y-%m-%dT%H:%M:%SZ")}))
        return ended.astimezone(REGINA).strftime("%Y-%m-%d %H:%M CST")

    def test_one_episode_prints_one_start_line_and_one_release_line_however_many_cards_hit(self) -> None:
        self.hits(10)
        local = self.until().astimezone(REGINA).strftime("%Y-%m-%d %H:%M CST")
        self.assertEqual(json.loads((self.root / "dispatch-holds.usage-limit.json").read_text())["hits"], 1)

        self.assertEqual(self.report(), [
            f"The factory holds new worker and triage runs for the Claude usage limit until {local}. Running cards finish.",
        ])
        self.hits(3)
        self.assertEqual(self.report(), [])

        ended = self.end_entry()
        self.assertEqual(self.report(), [f"The usage-limit hold ended at {ended}. New worker and triage runs start again."])
        self.assertEqual(self.report(), [])

        self.hits(1)
        self.assertEqual(len(self.report()), 1)

    def test_a_quiet_factory_prints_nothing(self) -> None:
        self.assertEqual(self.report(), [])


if __name__ == "__main__":
    unittest.main()
