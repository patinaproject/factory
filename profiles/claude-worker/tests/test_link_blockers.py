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
cards, links = state["cards"], state["links"]
argv = sys.argv[1:]
if argv[:2] == ["kanban", "list"]:
    print(json.dumps(cards))
elif argv[:2] == ["kanban", "show"]:
    task = next(c for c in cards if c["id"] == argv[2])
    print(json.dumps({"task": task,
                      "parents": [p for p, c in links if c == task["id"]],
                      "children": [c for p, c in links if p == task["id"]]}))
elif argv[:2] == ["kanban", "link"]:
    child = next(c for c in cards if c["id"] == argv[3])
    if child["status"] == "running":
        sys.exit("hermes: child is already running")
    links.append(argv[2:4])
    if child["status"] == "ready":
        child["status"] = "todo"
elif argv[:2] == ["kanban", "unlink"]:
    links.remove(argv[2:4])
else:
    sys.exit("unexpected " + repr(argv))
json.dump(state, open(path, "w"))
"""


def card(task_id: str, identifier: str, status: str = "ready", **extra) -> dict:
    return {"id": task_id, "title": f"{identifier}: work", "body": "", "status": status,
            "branch_name": f"{identifier.lower()}-work", **extra}


def issue(identifier: str, state_type: str = "unstarted") -> dict:
    return {"id": f"uuid-{identifier}", "identifier": identifier,
            "gitBranchName": f"{identifier.lower()}-work", "stateType": state_type}


class LinkBlockersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.hermes = self.dir / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)
        self.state_path = self.dir / "state.json"

    def board(self, cards: list[dict], links: list[list[str]] | None = None) -> None:
        self.state_path.write_text(json.dumps({"cards": cards, "links": links or []}))

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def run_script(self, relations: dict, *args: str) -> tuple[int, dict]:
        env = {**os.environ, "HERMES_BIN": str(self.hermes), "FAKE_HERMES_STATE": str(self.state_path)}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "link_blockers.py"), *args],
                              input=json.dumps({"blockedBy": [], "blocks": [], **relations}),
                              capture_output=True, text=True, env=env)
        return proc.returncode, json.loads(proc.stdout)

    def test_a_blocked_card_becomes_the_child_of_its_blockers_card(self) -> None:
        self.board([card("t_a", "A-1", "running"), card("t_b", "A-2")])
        code, out = self.run_script({"blockedBy": [issue("A-1")]}, "--task", "t_b")
        self.assertEqual((code, out), (0, {"linked": [{"issue": "A-1", "parent": "t_a", "child": "t_b"}],
                                           "skipped": [], "cycle": None}))
        self.assertEqual(self.state()["links"], [["t_a", "t_b"]])
        self.assertEqual(self.state()["cards"][1]["status"], "todo")

    def test_running_it_again_links_nothing_new(self) -> None:
        self.board([card("t_a", "A-1"), card("t_b", "A-2")], [["t_a", "t_b"]])
        code, out = self.run_script({"blockedBy": [issue("A-1")]}, "--task", "t_b")
        self.assertEqual((code, out["linked"]), (0, []))
        self.assertEqual(self.state()["links"], [["t_a", "t_b"]])

    def test_a_new_cards_parents_are_the_blockers_live_cards(self) -> None:
        self.board([card("t_a", "A-1"), card("t_c", "A-3", "done")])
        code, out = self.run_script({"blockedBy": [issue("A-1"), issue("A-3"), issue("A-4")]}, "--parents")
        self.assertEqual(code, 0)
        self.assertEqual(out["parents"], ["t_a"])
        self.assertEqual(out["skipped"], [{"issue": "A-3", "reason": "its card is done"},
                                          {"issue": "A-4", "reason": "no live card"}])

    def test_a_blocker_that_was_delegated_later_becomes_the_parent_of_the_waiting_card(self) -> None:
        self.board([card("t_a", "A-1"), card("t_b", "A-2")])
        code, out = self.run_script({"blocks": [issue("A-2")]}, "--task", "t_a")
        self.assertEqual((code, out["linked"]), (0, [{"issue": "A-2", "parent": "t_a", "child": "t_b"}]))
        self.assertEqual(self.state()["cards"][1]["status"], "todo")

    def test_a_card_a_worker_already_started_is_left_alone(self) -> None:
        self.board([card("t_a", "A-1"), card("t_b", "A-2", "running")])
        code, out = self.run_script({"blocks": [issue("A-2")]}, "--task", "t_a")
        self.assertEqual((code, out["linked"]), (0, []))
        self.assertEqual(out["skipped"], [{"issue": "A-2", "reason": "the child card is running"}])
        self.assertEqual(self.state()["links"], [])

    def test_a_blocker_without_a_card_or_with_a_finished_issue_links_nothing(self) -> None:
        self.board([card("t_b", "A-2"), card("t_c", "A-3")])
        code, out = self.run_script({"blockedBy": [issue("A-1"), issue("A-3", "completed")]}, "--task", "t_b")
        self.assertEqual((code, out["linked"]), (0, []))
        self.assertEqual(out["skipped"], [{"issue": "A-1", "reason": "no live card"},
                                          {"issue": "A-3", "reason": "the issue is completed"}])
        self.assertEqual(self.state()["links"], [])

    def test_a_blocker_whose_card_is_archived_has_no_card_in_the_list(self) -> None:
        self.board([card("t_b", "A-2")])
        code, out = self.run_script({"blockedBy": [issue("A-1")]}, "--task", "t_b")
        self.assertEqual((code, out["skipped"]), (0, [{"issue": "A-1", "reason": "no live card"}]))

    def test_cards_are_found_by_body_and_title_when_the_branch_differs(self) -> None:
        by_body = card("t_a", "A-1", branch_name=None, title="renamed", body="```text\nlinear_issue_id: uuid-A-1\n```")
        by_title = card("t_c", "A-3", branch_name=None)
        self.board([by_body, by_title, card("t_b", "A-2")])
        code, out = self.run_script({"blockedBy": [issue("A-1"), issue("A-3")]}, "--task", "t_b")
        self.assertEqual((code, [link["parent"] for link in out["linked"]]), (0, ["t_a", "t_c"]))

    def test_a_cycle_links_nothing_and_removes_the_link_that_closed_it(self) -> None:
        self.board([card("t_a", "A-1", "todo"), card("t_b", "A-2")], [["t_b", "t_a"]])
        code, out = self.run_script({"blockedBy": [issue("A-1")]}, "--task", "t_b")
        self.assertEqual((code, out), (0, {"linked": [], "skipped": [], "cycle": ["A-1"]}))
        self.assertEqual(self.state()["links"], [])

    def test_a_cycle_undoes_the_links_made_in_the_same_run(self) -> None:
        self.board([card("t_a", "A-1"), card("t_c", "A-3", "todo"), card("t_b", "A-2")], [["t_b", "t_c"]])
        code, out = self.run_script({"blockedBy": [issue("A-1"), issue("A-3")]}, "--task", "t_b")
        self.assertEqual((code, out["cycle"], out["linked"]), (0, ["A-3"], []))
        self.assertEqual(self.state()["links"], [])

    def test_a_longer_cycle_is_found_through_intermediate_cards(self) -> None:
        self.board([card("t_a", "A-1", "todo"), card("t_b", "A-2", "todo"), card("t_c", "A-3")],
                   [["t_c", "t_a"], ["t_a", "t_b"]])
        code, out = self.run_script({"blockedBy": [issue("A-2")]}, "--task", "t_c")
        self.assertEqual((code, out["cycle"]), (0, ["A-2"]))
        self.assertEqual(self.state()["links"], [])

    def test_a_hermes_failure_reports_the_error(self) -> None:
        self.state_path.write_text("not json")
        code, out = self.run_script({}, "--task", "t_a")
        self.assertEqual(code, 1)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
