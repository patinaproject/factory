from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parents[1]
_spec = importlib.util.spec_from_file_location(
    "blocker_order", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
)
order = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = order
_spec.loader.exec_module(order)
session_run = order.session_run

STUB = f"""#!{sys.executable}
import json, os, subprocess, sys, time
mode = os.environ["STUB_MODE"]
if mode == "report":
    print("starting")
    print(json.dumps({{"branch": "b", "worktree": os.getcwd(), "result": "done"}}))
    sys.exit(3)
if mode == "big":
    for _ in range(500):
        print("x" * 99)
    print(json.dumps({{"branch": "b", "worktree": os.getcwd(), "result": "r" * 5000}}))
if mode == "noise":
    print("y" * 5000)
if mode == "failed":
    print(json.dumps({{"error": "boom"}}))
if mode in ("sleep", "child"):
    if mode == "child":
        child = subprocess.Popen(["sleep", "60"])
        open(os.environ["STUB_CHILD"], "w").write(str(child.pid))
    open(os.environ["STUB_SELF"], "w").write(str(os.getpid()))
    time.sleep(60)
"""

FAKE_GH = """#!/bin/sh
case "$FAKE_GH" in
  view) echo '{"url": "https://github.com/o/r/pull/7", "state": "OPEN", "isDraft": false, "mergeStateStatus": "CLEAN", "headRefOid": "abc"}' ;;
  none) echo 'no pull requests found for branch "b"' >&2; exit 1 ;;
  *) echo 'HTTP 502' >&2; exit 1 ;;
esac
"""

ARGS = {"issue_id": "i", "issue_identifier": "ABC-1", "issue_url": "https://linear.app/x", "repo": "o/r"}
TRIAGE = {"HERMES_KANBAN_BOARD": "triage"}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SessionRunTest(unittest.TestCase):
    def setUp(self) -> None:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name).resolve()
        home = self.root / "home"
        (home / "scripts").mkdir(parents=True)
        self.executable(home / "scripts" / "claude-session", STUB)
        self.executable(self.root / "bin" / "gh", FAKE_GH)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.environ = {
            "HERMES_HOME": str(home), "HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_WORKSPACE": str(self.workspace),
        }

    def executable(self, path: Path, text: str) -> None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def host(self, mode: str, gh: str = "view", **overrides) -> session_run.Host:
        env = {
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}", "STUB_MODE": mode, "FAKE_GH": gh,
            "STUB_CHILD": str(self.root / "child"), "STUB_SELF": str(self.root / "self"),
        }
        fields = {"wrap": lambda argv: argv, "interrupted": lambda: False, "tick": 0.05, "grace": 5}
        return session_run.Host(env=env, **{**fields, **overrides})

    def run_tool(self, mode: str, environ=None, gh: str = "view", **overrides) -> dict:
        host = self.host(mode, gh, **overrides)
        return json.loads(session_run.run_tool(ARGS, environ or self.environ, host))

    def wait_dead(self, pid: int) -> None:
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(pid))

    def test_a_run_returns_its_exit_code_and_final_line(self):
        result = self.run_tool("report", self.environ | TRIAGE)

        report = {"branch": "b", "worktree": str(self.workspace), "result": "done"}
        self.assertEqual(result, {"output": f"starting\n{json.dumps(report)}", "exit_code": 3})

    def test_output_over_the_bound_keeps_the_final_line_whole_and_a_bounded_tail(self):
        result = self.run_tool("big", self.environ | TRIAGE)

        final = result["output"].splitlines()[-1]
        self.assertEqual(json.loads(final)["result"], "r" * 5000)
        self.assertEqual(len(result["output"]) - len(final), session_run.TAIL_CHARS)
        self.assertNotIn("output_cut", result)

    def test_output_with_no_json_line_is_its_last_characters(self):
        result = self.run_tool("noise")

        self.assertEqual(result["output"], ("y" * 5000 + "\n")[-session_run.TAIL_CHARS:])

    def test_a_run_past_its_limit_is_stopped_and_named(self):
        result = self.run_tool("sleep", limit=1)

        self.assertEqual(result["stopped"], "timeout")
        self.assertIn("1 seconds", result["error"])
        self.wait_dead(int((self.root / "self").read_text()))

    def test_an_interrupt_stops_the_run_and_the_processes_it_started(self):
        child = self.root / "child"
        result = self.run_tool("child", interrupted=lambda: child.exists() and child.read_text() != "")

        self.assertEqual(result["stopped"], "interrupt")
        self.assertNotIn("error", result)
        for path in (self.root / "self", child):
            self.wait_dead(int(path.read_text()))

    def test_a_successful_run_reports_its_pull_request(self):
        result = self.run_tool("report")

        self.assertEqual(result["pull_request"], {
            "url": "https://github.com/o/r/pull/7", "state": "OPEN", "isDraft": False,
            "mergeStateStatus": "CLEAN", "headRefOid": "abc",
        })

    def test_a_branch_with_no_pull_request_reports_null(self):
        result = self.run_tool("report", gh="none")

        self.assertIn("pull_request", result)
        self.assertIsNone(result["pull_request"])

    def test_another_gh_failure_reports_the_error(self):
        self.assertEqual(self.run_tool("report", gh="down")["pull_request"], {"error": "HTTP 502"})

    def test_a_failed_report_and_a_triage_card_get_no_pull_request(self):
        self.assertNotIn("pull_request", self.run_tool("failed"))
        self.assertNotIn("pull_request", self.run_tool("report", self.environ | TRIAGE))

    def test_a_missing_card_or_workspace_is_an_error_result(self):
        for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE"):
            result = self.run_tool("report", {k: v for k, v in self.environ.items() if k != name})
            self.assertIn(name, result["error"])

    def test_the_argv_names_every_given_argument(self):
        given = {**ARGS, "takeover_pr": "u", "message_file": "m", "ignore_blockers": "t_a,t_b"}

        argv = session_run.argv_for(given, self.environ)

        self.assertEqual(argv[0], f"{self.environ['HERMES_HOME']}/scripts/claude-session")
        self.assertEqual(argv[1:], [
            "--task-id", "t_1", "--issue-id", "i", "--issue-identifier", "ABC-1", "--issue-url", "https://linear.app/x",
            "--repo", "o/r", "--takeover-pr", "u", "--message-file", "m", "--ignore-blockers", "t_a,t_b",
        ])
        self.assertEqual(len(session_run.argv_for(ARGS, self.environ)), 11)

    def test_the_tool_is_registered_and_calls_no_kanban_tool(self):
        calls, registered = [], []

        class Ctx:
            def dispatch_tool(_, name, args, **kwargs):
                calls.append(name)

            def register_hook(_, name, callback):
                pass

            def register_tool(_, **kwargs):
                registered.append(kwargs)

        order.register(Ctx())

        (tool,) = registered
        self.assertEqual((tool["name"], tool["toolset"]), ("claude_session_run", "claude_session"))
        self.assertEqual(tool["schema"]["parameters"]["required"], ["issue_id", "issue_identifier", "issue_url", "repo"])
        with mock.patch.dict(os.environ, self.environ), mock.patch.object(
            session_run, "system_host", lambda environ: self.host("report")
        ):
            result = json.loads(tool["handler"](ARGS))
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(calls, [])

    def test_the_hook_watches_the_tool_and_the_log_re_read_stays_for_process_tools(self):
        self.assertIn(session_run.TOOL, order.WATCHED_TOOLS)
        self.assertNotIn(session_run.TOOL, order.PROCESS_TOOLS)
        payload = {"output": "cut", "output_cut": True}

        self.assertEqual(order.whole_output(payload, session_run.TOOL, {"session_id": "p"}, mock.Mock()), "cut")


class WorkerConfigTest(unittest.TestCase):
    CONFIG = (REPO / "profiles" / "claude-worker" / "config.yaml").read_text()

    def test_hermes_ends_a_tool_call_after_the_tool_limit_and_its_grace(self):
        match = re.search(r"^timeouts:\n  tools:\n    sequential_call: (\S+)$", self.CONFIG, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertGreater(float(match.group(1)), session_run.LIMIT_SECONDS + session_run.GRACE_SECONDS)

    def test_the_worker_has_the_toolset(self):
        match = re.search(r"^platform_toolsets:\n  cli: \[(.*)\]$", self.CONFIG, re.MULTILINE)

        self.assertIn("claude_session", [name.strip() for name in match.group(1).split(",")])

    def test_the_manifest_provides_the_tool(self):
        manifest = (PLUGIN / "plugin.yaml").read_text()

        self.assertRegex(manifest, r"(?m)^provides_tools:\n  - claude_session_run$")


if __name__ == "__main__":
    unittest.main()
