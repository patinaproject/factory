from __future__ import annotations

import importlib.util
import itertools
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "blocker_order", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
)
order = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = order
_spec.loader.exec_module(order)
result_line = order.result_line

sys.path.insert(0, str(PLUGIN.parents[1] / "profiles" / "claude-worker" / "scripts"))
import blockers  # noqa: E402

FINISHED = ("done", "archived")
# A card waits and re-runs at most once per blocker in the chain, so a loop past this is a bug.
MAX_RUNS_PER_CARD = 10


class FakeBoard:
    """Hermes' link, block, and completion rules, as `hermes_cli/kanban_db.py` and `tools/kanban_tools.py` apply them."""

    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.links: set[tuple[str, str]] = set()
        self.runs: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, str] = {}
        self.titles: dict[str, str] = {}
        self.logs: dict[str, str] = {}

    def add(self, task: str, status: str = "ready", title: str = "") -> None:
        self.status[task] = status
        self.titles[task] = title

    def parents(self, task: str) -> list[str]:
        return [p for p, c in self.links if c == task]

    def would_cycle(self, parent: str, child: str) -> bool:
        stack, seen = [child], set()
        while stack:
            node = stack.pop()
            if node == parent:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(c for p, c in self.links if p == node)
        return False

    def start(self, task: str) -> None:
        self.status[task] = "running"
        self.runs[task] = self.runs.get(task, 0) + 1

    def complete(self, task: str) -> None:
        self.status[task] = "done"
        for parent, child in self.links:
            if parent == task and self.status[child] == "todo" and all(
                self.status[p] in FINISHED for p in self.parents(child)
            ):
                self.status[child] = "ready"

    def dispatch_tool(self, name: str, args: dict, **_: object) -> str:
        self.calls.append((name, args))
        if name in self.fail:
            return json.dumps({"error": self.fail[name]})
        worker = os.environ["HERMES_KANBAN_TASK"]
        if name in order.PROCESS_TOOLS:
            assert args["action"] == "log", args
            if args["session_id"] not in self.logs:
                return json.dumps({"error": f"Process {args['session_id']} not found"})
            return json.dumps({"session_id": args["session_id"], "output": self.logs[args["session_id"]]})
        if name == "kanban_link":
            parent, child = args["parent_id"], args["child_id"]
            if parent not in self.status or child not in self.status:
                return json.dumps({"error": f"kanban_link: unknown task(s): {parent}"})
            if self.status[child] == "running" and child != worker:
                return json.dumps({"error": f"kanban_link: cannot link {parent} -> {child}: child is already running"})
            if self.would_cycle(parent, child):
                return json.dumps({"error": f"kanban_link: linking {parent} -> {child} would create a cycle"})
            self.links.add((parent, child))
            return json.dumps({"ok": True, "parent_id": parent, "child_id": child, "gated": False})
        if name == "kanban_block":
            if self.status[worker] != "running":
                return json.dumps({"error": f"could not block {worker} (unknown id or not in running/ready)"})
            open_parent = any(self.status[p] not in FINISHED for p in self.parents(worker))
            kind = args["kind"] if open_parent else "needs_input"
            self.status[worker] = "todo" if kind == "dependency" else "blocked"
            return json.dumps({"ok": True, "task_id": worker, "status": self.status[worker], "block_kind": kind})
        raise AssertionError(f"unexpected tool {name}")


def process_result(*lines: str) -> str:
    return json.dumps({"status": "exited", "exit_code": 0, "output": "".join(f"{line}\n" for line in lines)})


def waiting(*cards: str) -> str:
    return result_line.line(result_line.Waiting(cards))


class Hooked(unittest.TestCase):
    def setUp(self) -> None:
        order._handled.clear()
        self.board = FakeBoard()
        self.env = mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(order.STOP_NUDGE, None)
        self.hook = None

    def call(self, result: str, tool_name: str = "process_manage", task: str = "t_1"):
        hooks = []

        board = self.board

        class Ctx:
            def dispatch_tool(_, name, args, **kwargs):
                return board.dispatch_tool(name, args, **kwargs)

            def register_hook(_, name, callback):
                hooks.append((name, callback))

        order.register(Ctx())
        self.assertEqual([name for name, _ in hooks], ["transform_tool_result"])
        os.environ["HERMES_KANBAN_TASK"] = task
        return hooks[0][1](tool_name=tool_name, args={"action": "wait", "session_id": "proc_1"}, result=result, task_id="s", status="ok")


class HookTest(Hooked):
    def setUp(self) -> None:
        super().setUp()
        for task in ("t_1", "t_a", "t_b"):
            self.board.add(task, "todo")
        self.board.start("t_1")

    def outcome(self, rewritten: str) -> dict:
        result = json.loads(rewritten)
        self.assertEqual((result["status"], result["exit_code"]), ("exited", 0))
        self.assertTrue(result["output"].endswith(f"{waiting('t_a', 't_b')}\n"))
        return result["blocker_order"]

    def test_links_each_card_blocks_this_card_and_tells_the_worker_to_stop(self):
        outcome = self.outcome(self.call(process_result("starting", waiting("t_a", "t_b"))))

        self.assertEqual(outcome, {
            "linked": ["t_a", "t_b"], "not_linked": {}, "blocked": True, "ignore": [],
            "instruction": (
                "This card now waits in todo for its blocker cards t_a, t_b: the blocker-order hook linked them "
                "as its parents and blocked it with kind dependency. Hermes starts it again once they are done. "
                "Stop now: call no other tool and do not run claude-session again."
            ),
        })
        self.assertEqual(self.board.links, {("t_a", "t_1"), ("t_b", "t_1")})
        self.assertEqual(self.board.status["t_1"], "todo")
        self.assertEqual(self.board.calls[-1], ("kanban_block", {"reason": "waits for its blocker cards t_a, t_b", "kind": "dependency"}))
        self.assertEqual(os.environ.get(order.STOP_NUDGE), "off")

    def test_a_link_that_would_close_a_cycle_links_nothing_and_starts_claude(self):
        self.board.links.add(("t_1", "t_a"))
        self.board.links.add(("t_a", "t_b"))

        outcome = self.outcome(self.call(process_result(waiting("t_a", "t_b"))))

        self.assertEqual(outcome["linked"], [])
        self.assertEqual(outcome["blocked"], False)
        self.assertEqual(outcome["ignore"], ["t_a", "t_b"])
        self.assertEqual(sorted(outcome["not_linked"]), ["t_a", "t_b"])
        self.assertIn("would create a cycle", outcome["not_linked"]["t_a"])
        self.assertEqual(outcome["instruction"], (
            "This card does not wait for blocker cards t_a, t_b, because Hermes refused to link them: linking "
            "t_a, t_b would close a cycle of blockers. Post one thought on the Linear agent session that names "
            "the issue of each of those cards (kanban_show gives a card's title, which starts with its issue "
            "identifier) and says why this card starts without waiting for them. Then run claude-session again "
            "as before, adding `--ignore-blockers t_a,t_b` to it and to every later run of this card."
        ))
        self.assertEqual(self.board.links, {("t_1", "t_a"), ("t_a", "t_b")})
        self.assertEqual(self.board.status["t_1"], "running")
        self.assertNotIn("kanban_block", [name for name, _ in self.board.calls])
        self.assertNotIn(order.STOP_NUDGE, os.environ)

    def test_a_card_that_links_still_waits_when_another_closes_a_cycle(self):
        self.board.links.add(("t_1", "t_a"))

        outcome = self.outcome(self.call(process_result(waiting("t_a", "t_b"))))

        self.assertEqual((outcome["linked"], outcome["blocked"], outcome["ignore"]), (["t_b"], True, []))
        self.assertEqual(self.board.links, {("t_1", "t_a"), ("t_b", "t_1")})
        self.assertEqual(self.board.status["t_1"], "todo")

    def test_a_refused_block_leaves_the_block_to_the_worker(self):
        self.board.fail["kanban_block"] = "kanban_block: could not block t_1"

        outcome = self.outcome(self.call(process_result(waiting("t_a", "t_b"))))

        self.assertEqual((outcome["linked"], outcome["blocked"]), (["t_a", "t_b"], False))
        self.assertEqual(outcome["instruction"], (
            "The blocker-order hook linked this card to its blocker cards t_a, t_b but could not block it: "
            "kanban_block: could not block t_1. Call kanban_block with kind dependency and the reason "
            "`waits for its blocker cards t_a, t_b`, then stop."
        ))
        self.assertNotIn(order.STOP_NUDGE, os.environ)

    def test_a_block_that_hermes_records_as_another_kind_is_reported(self):
        self.board.status.update(t_a="done", t_b="done")

        outcome = self.outcome(self.call(process_result(waiting("t_a", "t_b"))))

        self.assertEqual(outcome["blocked"], True)
        self.assertEqual(outcome["instruction"], (
            "Hermes recorded this card's block as needs_input, not dependency, because its blocker cards "
            "t_a, t_b finished meanwhile. Post one error activity on the Linear agent session saying a person "
            "must unblock the card, then stop."
        ))

    def test_an_unexpected_failure_lets_claude_start(self):
        def broken(name, args, **_):
            raise RuntimeError("registry gone")

        self.board.dispatch_tool = broken

        outcome = self.outcome(self.call(process_result(waiting("t_a", "t_b"))))

        self.assertEqual((outcome["linked"], outcome["blocked"], outcome["ignore"]), ([], False, ["t_a", "t_b"]))
        self.assertEqual(outcome["not_linked"], {"t_a": "RuntimeError: registry gone", "t_b": "RuntimeError: registry gone"})
        self.assertIn("refused to link them: linking t_a failed: RuntimeError: registry gone; linking t_b failed: "
                      "RuntimeError: registry gone. Post one thought", outcome["instruction"])

    def test_the_same_result_read_twice_changes_the_board_once(self):
        first = self.call(process_result(waiting("t_a", "t_b")))
        calls = len(self.board.calls)

        self.assertEqual(self.call(process_result(waiting("t_a", "t_b")), tool_name="process_manage"), first)
        self.assertEqual(len(self.board.calls), calls)

    def test_an_older_hermes_passes_the_process_tool_by_that_name(self):
        outcome = json.loads(self.call(process_result(waiting("t_a", "t_b")), tool_name="process"))["blocker_order"]

        self.assertEqual(outcome["blocked"], True)

    def test_the_terminal_tool_result_is_read_too(self):
        outcome = json.loads(self.call(process_result(waiting("t_a", "t_b")), tool_name="terminal"))["blocker_order"]

        self.assertEqual(outcome["blocked"], True)

    def test_every_other_result_is_left_alone(self):
        cases = {
            "another tool": ("kanban_show", process_result(waiting("t_a"))),
            "a run report": ("process_manage", process_result(json.dumps({"session_id": "s", "result": "done"}))),
            "an earlier line": ("process_manage", process_result(waiting("t_a"), json.dumps({"session_id": "s"}))),
            "extra keys": ("process_manage", process_result(json.dumps({"waiting_on": ["t_a"], "x": 1}))),
            "no cards": ("process_manage", process_result(json.dumps({"waiting_on": []}))),
            "not cards": ("process_manage", process_result(json.dumps({"waiting_on": [1]}))),
            "not JSON": ("process", "plain text"),
            "no output": ("process", json.dumps({"status": "timeout"})),
        }
        for name, (tool, result) in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(self.call(result, tool_name=tool))
        self.assertEqual(self.board.calls, [])

    def test_a_process_that_is_not_a_kanban_worker_is_left_alone(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            hooks = []

            class Ctx:
                dispatch_tool = self.board.dispatch_tool

                def register_hook(_, name, callback):
                    hooks.append(callback)

            order.register(Ctx())
            self.assertIsNone(hooks[0](tool_name="process_manage", args={}, result=process_result(waiting("t_a"))))
        self.assertEqual(self.board.calls, [])


SESSION = result_line.Session(
    session_id="s", resumed=True, reason="resumed", worktree="/w", branch="abc-1-fix", progress=True,
    adopted=None, superseded=None, issue_links=None, triage_documents=None, session_title=None,
)


def outcome(result: str) -> result_line.Outcome:
    return result_line.Outcome(subtype="success", num_turns=3, result=result, usage=None, first_context_tokens=None)


def report(result: str) -> str:
    return result_line.line(result_line.Done(SESSION, outcome(result)))


class ResultLineTest(unittest.TestCase):
    def test_every_report_reads_back_as_written(self):
        reports = {
            "waiting": result_line.Waiting(("t_a", "t_b")),
            "done": result_line.Done(SESSION, outcome("Opened the pull request."), blocker_error="Linear said no"),
            "needs input": result_line.Done(SESSION, outcome("Needs input: which one?")),
            "triage without an activity": result_line.Done(SESSION, outcome("Triage outcome: done"), activity=None),
            "triage": result_line.Done(SESSION, outcome("Triage outcome: wontfix"), activity="response"),
            "failed without output": result_line.Failed("claude exited 1 without JSON output: boom", SESSION),
            "failed": result_line.Failed("claude run ended with error_during_execution (exit 0)", SESSION, outcome(None)),
            "refused": result_line.Refused("another claude-session holds /w"),
            "refused, transient": result_line.Refused("Linear token: the broker is busy", transient=True),
        }
        for name, written in reports.items():
            with self.subTest(name):
                self.assertEqual(result_line.last(f"log line\n{result_line.line(written)}\n"), written)

    def test_only_a_run_whose_result_asks_for_input_needs_input(self):
        self.assertEqual(result_line.needs_input(result_line.Done(SESSION, outcome("Needs input: a?"))), "Needs input: a?")
        self.assertEqual(result_line.needs_input(result_line.Failed("x", SESSION, outcome("Needs input: a?"))), "Needs input: a?")
        self.assertIsNone(result_line.needs_input(result_line.Done(SESSION, outcome("Needs input later"))))
        self.assertIsNone(result_line.needs_input(result_line.Refused("Needs input: a?")))
        self.assertIsNone(result_line.needs_input(None))


class NeedsInputTest(Hooked):
    def setUp(self) -> None:
        super().setUp()
        self.board.add("t_1", "todo")
        self.board.start("t_1")
        self.reason = "Needs input: which of the two migrations should win?\n\n1. keep a\n2. keep b"

    def test_a_resumed_run_that_ends_with_needs_input_blocks_the_card_once(self):
        self.assertIsNone(self.call(process_result(report("Pushed a fix; checks pending."))))
        self.assertEqual(self.board.status["t_1"], "running")

        outcome = json.loads(self.call(process_result(report(self.reason))))["needs_input_block"]

        self.assertEqual(self.board.calls, [("kanban_block", {"reason": self.reason, "kind": "needs_input"})])
        self.assertEqual(self.board.status["t_1"], "blocked")
        self.assertEqual(outcome, {"blocked": True, "instruction": (
            "Claude needs input, so the blocker-order hook blocked this card with kind needs_input and the whole "
            "result as the reason. Stop now: call no other tool and do not run claude-session again."
        )})
        self.assertEqual(os.environ[order.STOP_NUDGE], "off")

    def test_the_same_report_read_twice_blocks_once(self):
        first = self.call(process_result(report(self.reason)))

        self.assertEqual(self.call(process_result(report(self.reason))), first)
        self.assertEqual(len(self.board.calls), 1)

    def test_a_block_hermes_refuses_tells_the_worker_to_block_itself(self):
        self.board.fail["kanban_block"] = "could not block t_1"

        outcome = json.loads(self.call(process_result(report(self.reason)), tool_name="terminal"))["needs_input_block"]

        self.assertEqual(outcome["blocked"], False)
        self.assertEqual(outcome["instruction"], (
            "The blocker-order hook could not block this card: could not block t_1. Call kanban_block with kind "
            "needs_input and the whole result as the reason, then stop. Do not run claude-session again."
        ))
        self.assertNotIn(order.STOP_NUDGE, os.environ)

    def cut(self, reason: str, session: str = "proc_1") -> str:
        """What `process wait` returns for a long report: the last 2000 characters and how many it dropped."""
        output = "starting\n" + report(reason) + "\n"
        self.board.logs[session] = output
        return json.dumps({"status": "exited", "exit_code": 0, "output": output[-2000:], "output_cut": len(output) - 2000})

    def test_a_report_longer_than_the_cut_the_tool_keeps_is_read_from_the_log(self):
        reason = "Needs input: " + "Which option should the factory take for the retry policy? " * 50
        cut = self.cut(reason)
        self.assertGreater(len(reason), 2000)
        self.assertIsNone(order.needs_input(json.loads(cut)["output"]))

        rewritten = json.loads(self.call(cut))

        self.assertEqual(self.board.calls, [
            ("process_manage", {"action": "log", "session_id": "proc_1"}),
            ("kanban_block", {"reason": reason, "kind": "needs_input"}),
        ])
        self.assertEqual(self.board.status["t_1"], "blocked")
        self.assertEqual(rewritten["needs_input_block"]["blocked"], True)
        self.assertEqual(rewritten["output"], json.loads(cut)["output"])
        self.assertEqual(rewritten["output_cut"], json.loads(cut)["output_cut"])

    def test_a_cut_report_that_the_log_no_longer_has_is_left_alone(self):
        cut = self.cut("Needs input: " + "why? " * 600)
        del self.board.logs["proc_1"]

        self.assertIsNone(self.call(cut))
        self.assertEqual([name for name, _ in self.board.calls], ["process_manage"])

    def test_a_result_that_was_not_cut_never_reads_the_log(self):
        self.assertIsNone(self.call(process_result(report("Pushed a fix."))))
        self.call(process_result(report(self.reason)))

        self.assertEqual([name for name, _ in self.board.calls], ["kanban_block"])

    def test_other_reports_are_left_alone(self):
        cases = {
            "not first": report("Done.\nNeeds input: later"),
            "no session": json.dumps({"result": self.reason}),
            "an error line": json.dumps({"error": "boom"}),
            "an earlier line": report(self.reason) + "\n" + report("done"),
        }
        for name, line in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(self.call(process_result(*line.split("\n"))))
        self.assertEqual(self.board.calls, [])


def blocker(identifier: str, state: str = "started") -> dict:
    return {"id": f"uuid-{identifier}", "identifier": identifier, "gitBranchName": f"{identifier.lower()}-work",
            "stateType": state, "delegate": "app-user", "delegatedToSelf": True}


class Factory(Hooked):
    """Triage creates one card per delegated issue, the dispatcher runs ready cards, and each run first asks
    claude-session's blocker check, then the hook, the way a worker does. A card's run ends with its merge."""

    def setUp(self) -> None:
        super().setUp()
        self.blocked_by: dict[str, list[str]] = {}
        self.state: dict[str, str] = {}
        self.card: dict[str, str] = {}
        self.pending: list[str] = []
        self.started: set[str] = set()
        self.merged: list[str] = []
        self.notices: list[str] = []
        self.now = 0.0

    def issues(self, **blocked_by: str) -> None:
        for issue, blockers_of in blocked_by.items():
            self.blocked_by[issue] = list(blockers_of)
            self.state[issue] = "unstarted"

    def create(self, issue: str) -> str:
        task = f"t_{issue.lower()}"
        self.card[issue] = task
        self.board.add(task, "ready", f"{issue}: work")
        return task

    def board_list(self) -> list[dict]:
        return [
            {"id": task, "title": self.board.titles[task], "body": "", "branch_name": f"{issue.lower()}-work",
             "status": self.board.status[task]}
            for issue, task in self.card.items() if self.board.status[task] != "archived"
        ]

    def triage_meanwhile(self, seconds: float) -> None:
        self.now += seconds
        if self.pending:
            self.create(self.pending.pop(0))

    def run_card(self, issue: str) -> None:
        task = self.card[issue]
        self.board.start(task)
        ignore: list[str] = []
        for _ in range(MAX_RUNS_PER_CARD):
            cards = blockers.waiting_on(
                lambda: [blocker(b, self.state[b]) for b in self.blocked_by[issue]], self.board_list, ignore,
                sleep=self.triage_meanwhile, clock=lambda: self.now,
            )
            if not cards:
                break
            outcome = json.loads(self.call(process_result(waiting(*cards)), task=task))["blocker_order"]
            if outcome["blocked"]:
                return
            self.notices.append(issue)
            ignore = outcome["ignore"]
        else:
            self.fail(f"{issue} was still waiting after {MAX_RUNS_PER_CARD} runs")
        self.started.add(issue)
        self.merged.append(issue)
        self.state[issue] = "completed"
        self.board.complete(task)

    def deliver(self, delegation_order) -> None:
        self.pending = list(delegation_order)
        while True:
            ready = [issue for issue, task in self.card.items() if self.board.status[task] == "ready"]
            if ready:
                self.run_card(ready[0])
            elif self.pending:
                self.create(self.pending.pop(0))
            else:
                return


class ChainTest(Factory):
    def test_a_three_issue_chain_merges_in_order_whatever_order_it_is_delegated_in(self):
        for delegation_order in itertools.permutations("ABC"):
            with self.subTest(delegation_order=delegation_order):
                self.setUp()
                self.issues(A="", B="A", C="B")

                self.deliver(delegation_order)

                self.assertEqual(self.merged, ["A", "B", "C"])
                self.assertEqual(self.notices, [])
                self.assertTrue(all(status == "done" for status in self.board.status.values()))

    def test_a_mutual_block_runs_both_cards_and_posts_one_notice(self):
        self.issues(A="B", B="A")

        self.deliver("AB")

        self.assertEqual(self.merged, ["B", "A"])
        self.assertEqual(self.notices, ["B"])
        self.assertEqual(self.board.links, {("t_b", "t_a")})

    def test_a_mistaken_closing_relation_keeps_the_correct_chains_links(self):
        self.issues(A="", B="A", C="B")
        for issue in "ABC":
            self.create(issue)
        self.board.links |= {("t_a", "t_b"), ("t_b", "t_c")}
        self.board.status.update(t_b="todo", t_c="todo")
        self.blocked_by["A"] = ["C"]

        self.deliver([])

        self.assertEqual(self.merged, ["A", "B", "C"])
        self.assertEqual(self.notices, ["A"])
        self.assertEqual(self.board.links, {("t_a", "t_b"), ("t_b", "t_c")})


if __name__ == "__main__":
    unittest.main()
