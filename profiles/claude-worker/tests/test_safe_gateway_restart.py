from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, with_interpreter

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_HERMES_STATE"]
state = json.load(open(path))
argv = sys.argv[1:]
if argv == ["kanban", "list", "--status", "running", "--json"]:
    if state.get("list_fails"):
        sys.exit("hermes: database is locked")
    reads = state["reads"]
    state["calls"].append("list")
    print(json.dumps([{"id": card, "status": "running"} for card in (reads.pop(0) if len(reads) > 1 else reads[0])]))
elif argv == ["gateway", "restart"]:
    state["calls"].append("restart")
    json.dump(state, open(path, "w"))
    sys.exit(state.get("restart_exit", 0))
else:
    sys.exit("unexpected " + repr(argv))
json.dump(state, open(path, "w"))
"""


class SafeGatewayRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.hermes = self.dir / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)
        self.state_path = self.dir / "state.json"

    def run_script(self, reads: list, timeout: float = 5, **state) -> tuple[int, dict, list]:
        self.state_path.write_text(json.dumps({"reads": reads, "calls": [], **state}))
        env = {**os.environ, "FAKE_HERMES_STATE": str(self.state_path)}
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "safe-gateway-restart"), "--hermes", str(self.hermes),
             "--timeout", str(timeout), "--interval", "0.05", "--settle", "0.01"],
            capture_output=True, text=True, env=env,
        )
        return proc.returncode, json.loads(proc.stdout), json.loads(self.state_path.read_text())["calls"]

    def test_an_idle_board_restarts_the_gateway_once(self) -> None:
        code, line, calls = self.run_script([[]])
        self.assertEqual((code, line), (0, {"restarted": True, "exit": 0}))
        self.assertEqual(calls, ["list", "list", "restart"])

    def test_it_waits_for_a_running_card_before_it_restarts(self) -> None:
        code, line, calls = self.run_script([["t_1"], ["t_1"], []])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["list", "list", "list", "list", "restart"])

    def test_a_card_that_starts_between_the_two_empty_reads_still_holds_the_restart(self) -> None:
        code, line, calls = self.run_script([[], ["t_2"], []])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["list", "list", "list", "list", "restart"])

    def test_a_card_still_running_at_the_deadline_refuses_the_restart_and_names_it(self) -> None:
        code, line, calls = self.run_script([["t_1", "t_2"]], timeout=0.2)
        self.assertEqual(code, 1)
        self.assertEqual(line, {"restarted": False, "error": "cards still running", "running": ["t_1", "t_2"]})
        self.assertNotIn("restart", calls)

    def test_an_unreadable_board_refuses_the_restart(self) -> None:
        code, line, calls = self.run_script([[]], list_fails=True)
        self.assertEqual(code, 1)
        self.assertFalse(line["restarted"])
        self.assertIn("kanban list failed", line["error"])
        self.assertNotIn("restart", calls)

    def test_a_failed_gateway_restart_exits_with_its_code(self) -> None:
        code, line, calls = self.run_script([[]], restart_exit=3)
        self.assertEqual((code, line), (3, {"restarted": False, "exit": 3}))


if __name__ == "__main__":
    unittest.main()
