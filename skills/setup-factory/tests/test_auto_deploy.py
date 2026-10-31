from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "setup-factory" / "scripts"
AUTO_DEPLOY = SCRIPTS / "auto-deploy"
REPORT_DEPLOY = SCRIPTS / "report-deploy"
INSTALL_PLAN = REPO / "profiles" / "claude-worker" / "scripts" / "install_plan.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "auto_deploy"
FAKE_TOOLS = FIXTURES / "fake_tools.py"
# launchd runs the job on the macOS system Python, so the tests do too where it exists.
PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
SECRET = "ghp_" + "S3cretValueThatMustNeverLeak0123456789"
OPERATOR_SECRET = "operator-only-value-2b7f9c41"

loader = importlib.machinery.SourceFileLoader("auto_deploy", str(AUTO_DEPLOY))
spec = importlib.util.spec_from_loader(loader.name, loader)
auto_deploy = importlib.util.module_from_spec(spec)
loader.exec_module(auto_deploy)

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=True, capture_output=True, text=True).stdout.strip()


def tool(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FAKE_TOOLS, path)
    path.chmod(0o755)
    return path


class Machine:

    def __init__(self, tmp: str) -> None:
        self.tmp = Path(tmp)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        self.root = self.tmp / "root"
        self.worker = self.root / "profiles" / "claude-worker"
        (self.root / "scripts").mkdir(parents=True)
        self.hermes = tool(self.tmp / "bin" / "hermes")
        tool(self.worker / "scripts" / "check-install")
        self.upstream = self.tmp / "upstream"
        self.upstream.mkdir()
        git("init", "--quiet", "-b", "main", cwd=self.upstream)
        self.write_tree(AUTO_DEPLOY.read_text())
        git("add", "-A", cwd=self.upstream)
        git("commit", "--quiet", "-m", "A", cwd=self.upstream)
        self.origin = self.tmp / "origin.git"
        git("clone", "--quiet", "--bare", str(self.upstream), str(self.origin))
        self.source = self.tmp / "source"
        git("clone", "--quiet", str(self.origin), str(self.source))
        git("remote", "add", "upstream", str(self.upstream), cwd=self.origin)
        shutil.copyfile(AUTO_DEPLOY, self.root / "scripts" / "auto-deploy")
        shutil.copyfile(REPORT_DEPLOY, self.root / "scripts" / "report-deploy")
        self.hermes_state({"root": str(self.root), "key": False, "running": {"default": [[]], "triage": [[]]}, "fail": [], "on": []})
        self.drifts([])

    def write_tree(self, auto_deploy_text: str) -> None:
        scripts = self.upstream / "profiles" / "claude-worker" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(INSTALL_PLAN, scripts / "install_plan.py")
        tool(scripts / "check-install")
        setup = self.upstream / "skills" / "setup-factory" / "scripts"
        setup.mkdir(parents=True, exist_ok=True)
        (setup / "auto-deploy").write_text(auto_deploy_text)
        tool(setup / "restart-gateway")
        tool(setup / "check-settings")
        (self.upstream / "README.md").write_text("factory\n")

    def push(self, message: str, auto_deploy_text: str | None = None) -> str:
        if auto_deploy_text is not None:
            self.write_tree(auto_deploy_text)
        git("add", "-A", cwd=self.upstream)
        git("commit", "--quiet", "--allow-empty", "-m", message, cwd=self.upstream)
        git("fetch", "--quiet", "upstream", "+main:main", cwd=self.origin)
        return git("rev-parse", "HEAD", cwd=self.upstream)

    def push_command(self, message: str) -> str:
        env = " ".join(f"{k}={v}" for k, v in GIT_ENV.items())
        return (f"{env} git -C {self.upstream} commit --quiet --allow-empty -m {message} && "
                f"{env} git -C {self.origin} fetch --quiet upstream +main:main")

    @property
    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.upstream)

    def hermes_state(self, state: dict | None = None) -> dict:
        path = self.fake / "hermes.json"
        if state is not None:
            path.write_text(json.dumps(state))
        return json.loads(path.read_text())

    def update_hermes(self, **changes) -> None:
        self.hermes_state({**self.hermes_state(), **changes})

    def drifts(self, drifts: list, **extra) -> None:
        (self.fake / "check_install.json").write_text(json.dumps({"drifts": drifts, **extra}))

    def tools(self, **codes: int) -> None:
        (self.fake / "tools.json").write_text(json.dumps(codes))

    def install(self, *args: str, step: str = "PLUGINS", restarts: bool = False) -> dict:
        return {
            "kind": "plugin", "where": f"{args[-1]} in {self.root}", "detail": "differs",
            "remedy": {"type": "install", "step": step, "argv": [str(self.hermes), *args], "restarts_gateway": restarts},
        }

    @staticmethod
    def manual(where: str) -> dict:
        return {"kind": "config", "where": where, "detail": "is not declared", "remedy": {"type": "manual", "action": f"set {where} in the private record"}}

    @staticmethod
    def gating(where: str) -> dict:
        record = Machine.manual(where)
        record["remedy"]["gates_runs"] = True
        return record

    def env(self, **extra: str) -> dict:
        return {
            **os.environ, **GIT_ENV, "PATH": f"{self.hermes.parent}{os.pathsep}{os.environ['PATH']}",
            "HERMES_HOME": str(self.root), "HERMES_BIN": str(self.hermes), "FAKE_DEPLOY": str(self.fake),
            "FAKE_SOURCE": str(self.source), **extra,
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, str(self.root / "scripts" / "auto-deploy"), "--poll", "0.05", *args],
            env=self.env(**env), capture_output=True, text=True, timeout=120,
        )

    def report(self) -> str:
        proc = subprocess.run([PYTHON, str(self.root / "scripts" / "report-deploy")], env=self.env(), capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    def calls(self, tool_name: str | None = None) -> list:
        path = self.fake / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [call for call in calls if tool_name is None or call["tool"] == tool_name]

    def hermes_argv(self) -> list:
        return [call["argv"] for call in self.calls("hermes")]

    def changes(self) -> list:
        return [argv for argv in self.hermes_argv() if argv[0] != "kanban"]

    def state(self) -> dict:
        return json.loads((self.root / "auto-deploy.json").read_text())

    def outcomes(self) -> list:
        return [(event["outcome"], event["commit"]) for event in self.state()["events"]]

    def released(self) -> bool:
        return (not self.hermes_state()["key"] and not (self.root / "ESTOP").exists()
                and not (self.root / "factory-gateway.lock").exists() and self.state()["hold"] is None)


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class AutoDeployTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = Machine(self.tmp.name)

    def deploy(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        proc = self.m.run(*args, **env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_install_only_drift_deploys_at_a_break_under_the_hold_and_restarts_once(self) -> None:
        plugin = self.m.install("plugins", "install", "file://repo#plugins/blocker-order", restarts=True)
        profile = self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")
        self.m.drifts([plugin, profile])

        self.deploy()

        self.assertEqual(self.m.state()["deployed"], self.m.head)
        self.assertEqual(self.m.outcomes(), [("DEPLOYED", self.m.head)])
        installs = [call for call in self.m.calls("hermes") if call["argv"][0] in ("plugins", "profile")]
        self.assertEqual([call["argv"][0] for call in installs], ["profile", "plugins"])
        self.assertTrue(all(call["paused"] and call["held"] and call["running"] == 0 for call in installs))
        self.assertEqual(len(self.m.calls("restart-gateway")), 1)
        self.assertEqual(self.m.hermes_argv().count(["gateway", "restart"]), 1)
        self.assertTrue(self.m.released())
        self.assertEqual(stat.S_IMODE((self.m.root / "auto-deploy.json").stat().st_mode), 0o600)

    def test_no_restart_when_no_install_needs_one_and_no_pause_without_installs(self) -> None:
        self.m.drifts([self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")])
        self.deploy()
        self.assertEqual(self.m.calls("restart-gateway"), [])
        self.assertIn(["profile", "update", "claude-worker", "-y"], self.m.changes())

        self.m.drifts([])
        commit = self.m.push("B")
        self.deploy()
        self.assertEqual(self.m.state()["deployed"], commit)
        self.assertEqual([argv[0] for argv in self.m.changes()].count("pause"), 1)
        self.assertTrue(self.m.released())

    def test_a_failed_restart_stays_pending_until_a_later_run_restarts(self) -> None:
        first = self.m.head
        self.m.drifts([self.m.install("plugins", "install", "x", restarts=True)])
        self.m.tools(**{"restart-gateway": 1})
        self.deploy()
        self.assertEqual(self.m.outcomes(), [("FAILED", first)])
        self.assertEqual((self.m.state()["restart_pending"], self.m.state()["restarted"]), (first, None))

        self.m.drifts([])
        self.m.tools()
        self.deploy("--retry")
        self.assertEqual(self.m.outcomes()[-1], ("DEPLOYED", first))
        self.assertEqual(len(self.m.calls("restart-gateway")), 2)
        self.assertEqual((self.m.state()["restart_pending"], self.m.state()["restarted"]), (None, first))

        self.m.push("B")
        self.deploy()
        self.assertEqual(len(self.m.calls("restart-gateway")), 2)
        self.assertTrue(self.m.released())

    def test_a_pending_restart_is_done_by_the_next_commits_run(self) -> None:
        state = auto_deploy.fresh_state()
        state["restart_pending"] = "a" * 40
        (self.m.root / "auto-deploy.json").write_text(json.dumps(state))

        self.deploy()

        self.assertEqual(self.m.outcomes(), [("DEPLOYED", self.m.head)])
        self.assertEqual(len(self.m.calls("restart-gateway")), 1)
        self.assertEqual((self.m.state()["restart_pending"], self.m.state()["restarted"]), (None, self.m.head))
        self.assertTrue(self.m.released())

    def test_a_restart_waits_only_as_long_as_the_budget_has_left(self) -> None:
        self.m.drifts([self.m.install("config", "set", "kanban.max_spawn", "2", step="CONFIG", restarts=True)])
        self.deploy("--timeout", "30")
        argv = self.m.calls("restart-gateway")[0]["argv"]
        left = float(argv[argv.index("--timeout") + 1])
        self.assertTrue(0 < left <= 30, left)

    def test_a_dirty_diverged_source_checkout_is_reset_only_after_no_card_runs(self) -> None:
        (self.m.source / "README.md").write_text("a person's edit\n")
        git("commit", "--quiet", "-am", "local", cwd=self.m.source)
        (self.m.source / "README.md").write_text("uncommitted\n")
        diverged = git("rev-parse", "HEAD", cwd=self.m.source)
        commit = self.m.push("B")
        self.m.update_hermes(running={"default": [[{"id": "t_1", "title": "PAT-1"}]], "triage": [[]]})

        self.deploy()

        self.assertEqual(self.m.outcomes(), [("WAITING", commit)])
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.m.source), diverged)
        self.assertNotEqual(git("status", "--porcelain", "--untracked-files=no", cwd=self.m.source), "")

        self.m.update_hermes(running={"default": [[]], "triage": [[]]})
        self.deploy()

        self.assertEqual(git("rev-parse", "HEAD", cwd=self.m.source), commit)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=no", cwd=self.m.source), "")
        self.assertEqual(self.m.state()["deployed"], commit)

    def test_a_push_during_the_run_is_deployed_by_the_same_run(self) -> None:
        first = self.m.head
        self.m.drifts([self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")])
        self.m.update_hermes(on=[{"argv": ["profile", "update", "claude-worker", "-y"], "sh": self.m.push_command("B")}])

        self.deploy()

        self.assertNotEqual(self.m.head, first)
        self.assertEqual(self.m.outcomes(), [("DEPLOYED", first), ("DEPLOYED", self.m.head)])
        self.assertEqual(self.m.state()["deployed"], self.m.head)
        self.assertTrue(self.m.released())

    def test_manual_drift_never_holds_an_install_and_is_reported_once(self) -> None:
        self.m.drifts([self.m.manual("dashboard.public_url"), self.m.install("cron", "resume", "job1", step="CRON")])

        self.deploy()

        self.assertIn(["cron", "resume", "job1"], self.m.changes())
        self.assertEqual(self.m.state()["outcome"], "DEPLOYED")
        self.assertEqual(self.m.state()["manual"], [{
            "line": "config dashboard.public_url: is not declared", "action": "set dashboard.public_url in the private record",
        }])
        self.assertEqual(self.m.report().splitlines(), [
            f"auto-deploy deployed {self.m.head[:12]}",
            "auto-deploy needs a person: config dashboard.public_url: is not declared: set dashboard.public_url in the private record",
        ])
        self.assertEqual(self.m.report(), "")
        self.assertEqual(stat.S_IMODE((self.m.root / "report-deploy.json").stat().st_mode), 0o600)

    def test_a_run_gating_manual_line_installs_nothing_restarts_nothing_and_records_blocked(self) -> None:
        plugin = self.m.install("plugins", "install", "file://repo#plugins/blocker-order", restarts=True)
        self.m.drifts([self.m.gating("service gh login refresh"), plugin])

        self.deploy()

        self.assertEqual(self.m.state()["outcome"], "BLOCKED")
        self.assertIn("config service gh login refresh: is not declared", self.m.state()["detail"])
        self.assertEqual(self.m.state()["deployed"], None)
        self.assertNotIn(["plugins", "install", "file://repo#plugins/blocker-order"], self.m.changes())
        self.assertEqual(self.m.calls("restart-gateway"), [])
        self.assertTrue(self.m.released())

    def test_a_target_blocked_by_a_run_gating_line_is_not_tried_again_until_origin_moves(self) -> None:
        self.m.drifts([self.m.gating("service gh login refresh")])
        self.deploy()
        self.assertEqual(self.m.state()["outcome"], "BLOCKED")
        events = self.m.state()["events"]
        before = len(self.m.hermes_argv())

        self.deploy()

        self.assertEqual(self.m.hermes_argv()[before:], [])
        self.assertEqual(self.m.state()["events"], events)
        self.m.drifts([])
        self.m.push("B")
        self.deploy()
        self.assertEqual(self.m.state()["deployed"], self.m.head)

    def test_a_running_card_holds_nothing_installs_nothing_and_records_waiting_once(self) -> None:
        self.m.drifts([self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")])
        self.m.update_hermes(running={"default": [[{"id": "t_1", "title": "PAT-1"}]], "triage": [[]]})

        self.deploy()

        self.assertEqual(self.m.outcomes(), [("WAITING", self.m.head)])
        self.assertIn("default/t_1 PAT-1", self.m.state()["detail"])
        self.assertEqual(self.m.changes(), [])
        self.assertEqual(self.m.calls("check-install"), [])
        self.assertTrue(self.m.released())
        first = self.m.state()["detail"]

        self.m.update_hermes(running={"default": [[{"id": "t_2", "title": "PAT-2"}]], "triage": [[{"id": "t_3", "title": "PAT-3"}]]})
        self.deploy()

        self.assertEqual(self.m.outcomes(), [("WAITING", self.m.head)])
        self.assertEqual(self.m.changes(), [])
        self.assertEqual(self.m.report(), f"auto-deploy WAITING at {self.m.head[:12]}: {first}\n")
        self.assertEqual(self.m.report(), "")

    def test_with_no_card_running_the_run_holds_installs_records_deployed_and_releases(self) -> None:
        commit = self.m.push("B")
        self.m.drifts([self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")])

        self.deploy()

        self.assertEqual(self.m.outcomes(), [("DEPLOYED", commit)])
        install = next(call for call in self.m.calls("hermes") if call["argv"][0] == "profile")
        self.assertTrue(install["held"])
        changes = self.m.changes()
        self.assertEqual((changes[0], changes[-1]), (list(auto_deploy.HOLD), list(auto_deploy.RELEASE)))
        self.assertTrue(self.m.released())

    def in_process(self, sleep) -> "auto_deploy.Deploy":
        args = argparse.Namespace(root=str(self.m.root), worker=str(self.m.worker), retry=False, timeout=3600.0, poll=7.0)
        env = mock.patch.dict(os.environ, self.m.env())
        env.start()
        self.addCleanup(env.stop)
        return auto_deploy.Deploy(args, str(self.m.hermes), auto_deploy.Redactor({}), clock=lambda: 0.0, sleep=sleep)

    def test_a_card_claimed_before_the_hold_took_effect_stops_the_install(self) -> None:
        self.m.drifts([self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")])
        slept = []

        def sleep(seconds: float) -> None:
            if slept:
                raise AssertionError("the run slept twice")
            slept.append((seconds, self.m.hermes_state()["key"]))
            self.m.update_hermes(running={"default": [[{"id": "t_9", "title": "PAT-9"}]], "triage": [[]]})

        self.assertEqual(self.in_process(sleep).deploy(None, 0.0), 0)

        self.assertEqual(slept, [(7.0, True)])
        self.assertEqual(self.m.outcomes(), [("WAITING", self.m.head)])
        self.assertIn("default/t_9 PAT-9", self.m.state()["detail"])
        self.assertEqual(self.m.changes(), [list(auto_deploy.HOLD), list(auto_deploy.RELEASE)])
        self.assertEqual(self.m.calls("check-install"), [])
        self.assertTrue(self.m.released())

    def test_a_run_that_cannot_read_the_running_cards_records_failed_without_a_commit_and_the_next_run_deploys(self) -> None:
        self.m.update_hermes(fail=[{"argv": ["kanban", "boards", "list", "--json"], "stderr": "board busy"}])

        self.deploy()

        self.assertEqual(self.m.outcomes(), [("FAILED", None)])
        self.assertIsNone(self.m.state()["failed"])
        self.assertEqual(self.m.changes(), [])

        self.m.update_hermes(fail=[])
        self.deploy()

        self.assertEqual(self.m.state()["deployed"], self.m.head)

    def break_origin(self) -> str:
        url = git("remote", "get-url", "origin", cwd=self.m.source)
        git("remote", "set-url", "origin", str(self.m.tmp / "nowhere.git"), cwd=self.m.source)
        return url

    def test_a_result_repeated_by_the_next_run_is_recorded_and_reported_once(self) -> None:
        estop = self.m.root / "ESTOP"
        lock = self.m.root / "factory-gateway.lock"

        def pause():
            estop.write_text(json.dumps({"reason": "operator maintenance"}))
            return estop.unlink

        def gateway_lock():
            lock.write_text("a person deploying\n")
            return lock.unlink

        def hold_key():
            self.m.update_hermes(fail=[{"argv": ["kanban", "diagnostics", "--json"], "stderr": "config unreadable"}])
            return lambda: self.m.update_hermes(fail=[])

        def fetch():
            url = self.break_origin()
            return lambda: git("remote", "set-url", "origin", url, cwd=self.m.source)

        for name, cause in (("pause", pause), ("gateway_lock", gateway_lock), ("hold key", hold_key), ("fetch", fetch)):
            with self.subTest(name):
                before = len(self.m.state()["events"]) if (self.m.root / "auto-deploy.json").exists() else 0
                undo = cause()
                self.deploy()
                self.deploy()
                undo()

                events = self.m.state()["events"][before:]
                self.assertEqual(len(events), 1, events)
                self.assertIn(events[0]["outcome"], ("BLOCKED", "FAILED"))
                self.assertEqual(len(self.m.report().splitlines()), 1)

    def test_waiting_then_a_fetch_failure_then_waiting_again_records_the_wait_once(self) -> None:
        self.m.update_hermes(running={"default": [[{"id": "t_1", "title": "PAT-1"}]], "triage": [[]]})
        self.deploy()
        url = self.break_origin()
        self.deploy()
        git("remote", "set-url", "origin", url, cwd=self.m.source)

        self.deploy()

        self.assertEqual(self.m.outcomes(), [("WAITING", self.m.head), ("FAILED", None)])
        self.assertEqual(len(self.m.report().splitlines()), 2)

    def test_a_deployed_event_lets_the_same_result_report_again(self) -> None:
        self.break_origin()
        self.deploy()
        self.deploy()
        git("remote", "set-url", "origin", str(self.m.origin), cwd=self.m.source)
        self.deploy()
        self.m.push("B")
        self.break_origin()

        self.deploy()

        self.assertEqual([outcome for outcome, _ in self.m.outcomes()], ["FAILED", "DEPLOYED", "FAILED"])

    def test_every_failure_releases_what_the_run_engaged(self) -> None:
        self.m.drifts([self.m.install("plugins", "install", "x", restarts=True)])
        self.m.update_hermes(fail=[{"argv": ["plugins", "install", "x"], "code": 3, "stderr": "boom"}])
        self.deploy()
        self.assertEqual(self.m.state()["outcome"], "FAILED")
        self.assertIn("exited 3: boom", self.m.state()["detail"])
        self.assertEqual(self.m.calls("restart-gateway"), [])
        self.assertTrue(self.m.released())

        self.deploy()
        self.assertEqual(len(self.m.state()["events"]), 1, "a failed commit is not retried until origin moves")

        self.m.update_hermes(fail=[])
        commit = self.m.push("B")
        self.deploy()
        self.assertEqual(self.m.outcomes()[-1], ("DEPLOYED", commit))

    def test_a_check_install_crash_or_failed_check_settings_records_failed(self) -> None:
        self.m.drifts([], exit=2, stderr="check-install: HERMES_HOME is not set")
        self.deploy()
        self.assertEqual(self.m.state()["outcome"], "FAILED")
        self.assertIn("check-install exited 2", self.m.state()["detail"])
        self.assertTrue(self.m.released())

        self.m.drifts([])
        self.m.tools(**{"check-settings": 1})
        self.m.push("B")
        self.deploy()
        self.assertEqual(self.m.state()["outcome"], "FAILED")
        self.assertIn("claude-worker: claude_session.model differs", self.m.state()["detail"])

    def test_a_failed_commit_is_not_retried_after_a_later_blocked_run(self) -> None:
        self.m.drifts([], exit=2, stderr="boom")
        self.deploy()
        self.assertEqual(self.m.outcomes(), [("FAILED", self.m.head)])
        estop = self.m.root / "ESTOP"
        estop.write_text(json.dumps({"reason": "operator maintenance"}))
        self.deploy()
        estop.unlink()
        before = len(self.m.hermes_argv())

        self.deploy()

        self.assertEqual(self.m.hermes_argv()[before:], [])
        self.assertEqual([outcome for outcome, _ in self.m.outcomes()], ["FAILED", "BLOCKED"])
        self.m.drifts([])
        self.m.push("B")
        self.deploy()
        self.assertEqual(self.m.state()["deployed"], self.m.head)

    def test_retry_deploys_a_commit_that_failed_before(self) -> None:
        self.m.drifts([], exit=2, stderr="boom")
        self.deploy()
        self.m.drifts([])
        self.deploy()
        self.assertEqual(self.m.outcomes(), [("FAILED", self.m.head)])

        self.deploy("--retry")

        self.assertEqual(self.m.outcomes(), [("FAILED", self.m.head), ("DEPLOYED", self.m.head)])
        self.assertEqual((self.m.state()["deployed"], self.m.state()["failed"]), (self.m.head, None))

    def test_a_retry_that_fails_the_same_way_again_still_stops_later_runs(self) -> None:
        self.m.drifts([], exit=2, stderr="boom")
        self.deploy()
        self.deploy("--retry")

        self.assertEqual(self.m.outcomes(), [("FAILED", self.m.head)])
        self.assertEqual(self.m.state()["failed"], self.m.head)
        before = len(self.m.calls("check-install"))
        self.deploy()
        self.assertEqual(len(self.m.calls("check-install")), before)

    def test_the_plist_runs_the_installed_script_at_load_on_its_interval_and_on_demand_with_no_secret(self) -> None:
        proc = subprocess.run(
            [PYTHON, str(AUTO_DEPLOY), "--print-plist", "--root", "/h"],
            env={**self.m.env(), "HOME": "/home/op", "GITHUB_TOKEN": SECRET}, capture_output=True, timeout=60,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plistlib.loads(proc.stdout), {
            "Label": "ai.patinaproject.factory.auto-deploy",
            "ProgramArguments": ["/usr/bin/python3", "/h/scripts/auto-deploy", "--root", "/h"],
            "RunAtLoad": True,
            "StartInterval": 300,
            "EnvironmentVariables": {
                "HERMES_HOME": "/h", "PATH": "/home/op/.local/bin:/usr/local/bin:/usr/bin:/bin",
                "GH_CONFIG_DIR": "/h/profiles/claude-worker/gh",
            },
            "StandardOutPath": "/h/logs/auto-deploy.log",
            "StandardErrorPath": "/h/logs/auto-deploy.log",
        })

    def test_an_unreadable_hold_key_holds_nothing_and_the_next_delivery_retries(self) -> None:
        self.m.update_hermes(fail=[{"argv": ["kanban", "diagnostics", "--json"], "stderr": "config unreadable"}])
        self.deploy()
        self.assertEqual(self.m.outcomes(), [("FAILED", None)])
        self.assertEqual(self.m.changes(), [])

        self.m.update_hermes(fail=[])
        self.deploy()
        self.assertEqual(self.m.state()["deployed"], self.m.head)

    def test_install_drift_that_remains_records_failed(self) -> None:
        self.m.drifts([{**self.m.install("plugins", "install", "x"), "sticky": True}])
        self.deploy()
        self.assertEqual(self.m.state()["outcome"], "FAILED")
        self.assertIn("still reports 1 install", self.m.state()["detail"])

    def test_a_dispatch_profiles_key_a_person_set_blocks_and_stays(self) -> None:
        self.m.update_hermes(key=True)
        self.deploy()
        self.assertEqual(self.m.state()["outcome"], "BLOCKED")
        self.assertEqual(self.m.changes(), [])
        self.assertTrue(self.m.hermes_state()["key"])

    def test_an_operator_pause_changes_nothing(self) -> None:
        estop = self.m.root / "ESTOP"
        estop.write_text(json.dumps({"reason": "operator maintenance", "engaged_at": "then"}))
        self.deploy()
        self.assertEqual(self.m.hermes_argv(), [])
        self.assertEqual(json.loads(estop.read_text())["reason"], "operator maintenance")
        self.assertEqual(self.m.state()["outcome"], "BLOCKED")
        self.assertIsNone(self.m.state()["deployed"])

    def test_a_crashed_runs_pause_key_and_lock_are_released_before_anything_else(self) -> None:
        pid = dead_pid()
        reason = f"auto-deploy {pid}: installing abc"
        (self.m.root / "ESTOP").write_text(json.dumps({"reason": reason}))
        (self.m.root / "factory-gateway.lock").write_text(f"{pid}\n")
        (self.m.root / "auto-deploy.lock").write_text(f"{pid}\n")
        self.m.update_hermes(key=True)
        state = auto_deploy.fresh_state()
        state["hold"] = {"pid": pid, "commit": "abc", "dispatch_profiles": True, "pause": reason, "gateway_lock": True}
        (self.m.root / "auto-deploy.json").write_text(json.dumps(state))

        self.deploy()

        self.assertEqual(self.m.changes()[:2], [["resume"], list(auto_deploy.RELEASE)])
        self.assertEqual(self.m.state()["deployed"], self.m.head)
        self.assertTrue(self.m.released())
        self.assertFalse((self.m.root / "auto-deploy.lock").exists())

    def test_recovery_never_lifts_a_pause_that_replaced_the_crashed_runs(self) -> None:
        pid = dead_pid()
        estop = self.m.root / "ESTOP"
        estop.write_text(json.dumps({"reason": "operator maintenance"}))
        state = auto_deploy.fresh_state()
        state["hold"] = {"pid": pid, "commit": "abc", "dispatch_profiles": True, "pause": f"auto-deploy {pid}: installing abc", "gateway_lock": False}
        (self.m.root / "auto-deploy.json").write_text(json.dumps(state))
        self.m.update_hermes(key=True)

        self.deploy()

        self.assertEqual(self.m.hermes_argv(), [])
        self.assertTrue(estop.exists())
        self.assertTrue(self.m.hermes_state()["key"])
        self.assertEqual(self.m.state()["outcome"], "BLOCKED")

    def test_a_gateway_lock_that_is_not_a_live_runs_blocks_and_stays(self) -> None:
        lock = self.m.root / "factory-gateway.lock"
        for content in ("a person deploying\n", f"{dead_pid()}\n"):
            with self.subTest(content=content):
                lock.write_text(content)
                self.m.drifts([self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")])
                self.deploy()
                self.assertEqual(self.m.state()["outcome"], "BLOCKED")
                self.assertEqual(lock.read_text(), content)
                self.assertNotIn(["profile", "update", "claude-worker", "-y"], self.m.changes())
                self.assertFalse(self.m.hermes_state()["key"])
                self.assertIsNone(self.m.state()["hold"])

    def test_a_commit_already_deployed_changes_nothing(self) -> None:
        self.deploy()
        before = (self.m.root / "auto-deploy.json").read_bytes()
        calls = len(self.m.calls())
        self.deploy()
        self.assertEqual((self.m.root / "auto-deploy.json").read_bytes(), before)
        self.assertEqual(self.m.calls()[calls:], [])

    def test_a_live_run_keeps_its_run_lock_and_a_dead_ones_is_reclaimed(self) -> None:
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "auto-deploy"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        lock = self.m.root / "auto-deploy.lock"
        lock.write_text(f"{live.pid}\n")
        proc = self.deploy()
        self.assertIn("another run holds the run lock", proc.stdout)
        self.assertFalse((self.m.root / "auto-deploy.json").exists())
        self.assertEqual(lock.read_text(), f"{live.pid}\n")

        lock.write_text(f"{dead_pid()}\n")
        self.deploy()
        self.assertEqual(self.m.state()["deployed"], self.m.head)
        self.assertFalse(lock.exists())

    def test_a_run_lock_another_process_removed_first_is_not_an_error(self) -> None:
        deploy = auto_deploy.Deploy(argparse.Namespace(root=str(self.m.root), worker=str(self.m.worker)), str(self.m.hermes), auto_deploy.Redactor({}))
        lock = self.m.root / "auto-deploy.lock"

        def removed_by_another(read):
            def read_then_remove(path):
                pid = read(path)
                path.unlink()
                return pid
            return read_then_remove

        lock.write_text(f"{dead_pid()}\n")
        with mock.patch.object(auto_deploy, "read_pid", removed_by_another(auto_deploy.read_pid)):
            taken = deploy.take_run_lock(continuing=False)
        self.assertTrue(taken)
        with mock.patch.object(auto_deploy, "read_pid", removed_by_another(auto_deploy.read_pid)):
            deploy.drop_run_lock()
        self.assertFalse(lock.exists())

    def test_a_change_to_auto_deploy_runs_the_source_checkouts_copy(self) -> None:
        text = AUTO_DEPLOY.read_text().replace(
            'if __name__ == "__main__":', 'print("auto-deploy: the source copy runs", flush=True)\n\nif __name__ == "__main__":', 1,
        )
        commit = self.m.push("B", auto_deploy_text=text)
        self.m.drifts([self.m.install("profile", "update", "claude-worker", "-y", step="PROFILE")])

        proc = self.deploy()

        self.assertEqual(proc.stdout.count("the source copy runs"), 1)
        self.assertEqual(self.m.state()["deployed"], commit)
        self.assertIn(["profile", "update", "claude-worker", "-y"], self.m.changes())
        self.assertTrue(self.m.released())
        self.assertFalse((self.m.root / "auto-deploy.lock").exists())

    def test_no_secret_reaches_the_state_file_the_log_or_the_report(self) -> None:
        env = {"FACTORY_PROXY_TOKEN": OPERATOR_SECRET}
        self.m.drifts([], exit=2, stderr=f"check-install: leaked {SECRET} and {OPERATOR_SECRET}")
        failed = self.deploy(**env)
        self.m.drifts([self.m.install("plugins", "install", "x")])
        self.m.update_hermes(fail=[{"argv": ["plugins", "install", "x"], "stderr": f"denied {OPERATOR_SECRET}"}])
        self.m.push("B")
        failed_install = self.deploy(**env)
        report = self.m.report()

        self.assertIn("[redacted]", self.m.state()["detail"])
        for text in (failed.stdout + failed.stderr, failed_install.stdout + failed_install.stderr,
                     (self.m.root / "auto-deploy.json").read_text(), report):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(OPERATOR_SECRET, text)
        self.assertEqual(len(report.splitlines()), 2)

    def test_dry_run_prints_the_plan_and_changes_nothing(self) -> None:
        (self.m.source / "README.md").write_text("a person's edit\n")
        self.m.drifts([self.m.install("plugins", "install", "x", restarts=True), self.m.manual("dashboard.public_url")])

        proc = self.deploy("--dry-run")

        self.assertIn(f"install PLUGINS: {self.m.hermes} plugins install x", proc.stdout)
        self.assertIn("then restart the gateway", proc.stdout)
        self.assertIn("manual config dashboard.public_url: is not declared", proc.stdout)
        self.assertEqual(self.m.changes(), [])
        self.assertFalse((self.m.root / "auto-deploy.json").exists())
        self.assertEqual((self.m.source / "README.md").read_text(), "a person's edit\n")

    def test_status_prints_the_state(self) -> None:
        self.deploy()
        proc = self.deploy("--status")
        status = json.loads(proc.stdout)
        self.assertEqual(status["deployed"], self.m.head)
        self.assertIsNone(status["run"])


def hermes_python() -> tuple | None:
    root = Path.home() / ".hermes"
    try:
        entry = json.loads((root / "tools" / "facts.json").read_text())["packages"]["python"]["entry"]
        environments = [json.loads(p.read_text())["packages"]["venv"]["environment"] for p in sorted((root / "installs").glob("*/facts.json"))]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    python = root / "tools" / entry / "bin" / "python3"
    site = [path for env in environments if env for path in sorted(Path(env).glob("lib/python*/site-packages"))]
    checkout = root / "hermes-agent"
    if not python.is_file() or len(site) != 1 or not (checkout / "hermes_cli" / "kanban_db_dispatch.py").is_file():
        return None
    return python, site[0], checkout


@unittest.skipIf(hermes_python() is None, "no installed Hermes to run a dispatch tick with")
class HermesDispatchTickTest(unittest.TestCase):
    def test_hermes_claims_no_card_on_either_board_while_the_hold_is_engaged(self) -> None:
        python, site, checkout = hermes_python()
        with tempfile.TemporaryDirectory() as scratch:
            proc = subprocess.run(
                [str(python), "-I", "-B", str(FIXTURES / "dispatch_tick.py"), str(checkout), str(site),
                 json.dumps(list(auto_deploy.HOLD)), json.dumps(list(auto_deploy.RELEASE))],
                env={"PATH": "/usr/bin:/bin", "HERMES_HOME": os.path.join(scratch, "home"), "PYTHONDONTWRITEBYTECODE": "1"},
                cwd=scratch, capture_output=True, text=True, timeout=300,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(result["held_spawns"], [0, 0])
        self.assertEqual(result["held_rows"], sorted(
            [board, title, status, "claude-worker", None]
            for board in ("default", "triage")
            for title, status in (("ready before the hold", "ready"), ("ready during the hold", "ready"), ("review during the hold", "review"))
        ))
        self.assertTrue(result["held_scope"].startswith("none"))
        self.assertEqual(result["released_scope"], "any")
        self.assertEqual(result["released_spawns"], 6)


class ReportDeployTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, events: list, manual: list | None = None) -> None:
        state = {"events": [{"seq": i + 1, "outcome": o, "commit": c, "detail": d} for i, (o, c, d) in enumerate(events)], "manual": manual or []}
        (self.root / "auto-deploy.json").write_text(json.dumps(state))

    def report(self) -> list:
        proc = subprocess.run([PYTHON, str(REPORT_DEPLOY)], env={**os.environ, "HERMES_HOME": str(self.root)}, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.splitlines()

    def test_nothing_to_report_prints_nothing(self) -> None:
        self.assertEqual(self.report(), [])
        (self.root / "auto-deploy.json").write_text("{not json")
        self.assertEqual(self.report(), [])

    def test_each_outcome_is_reported_once_and_deployed_once_per_commit(self) -> None:
        a, b = "a" * 40, "b" * 40
        events = [("WAITING", a, "1 card(s) still running"), ("WAITING", a, "1 card(s) still running"), ("DEPLOYED", a, "")]
        self.write(events)
        self.assertEqual(self.report(), [
            f"auto-deploy WAITING at {a[:12]}: 1 card(s) still running",
            f"auto-deploy WAITING at {a[:12]}: 1 card(s) still running",
            f"auto-deploy deployed {a[:12]}",
        ])
        self.assertEqual(self.report(), [])

        events += [("DEPLOYED", a, ""), ("BLOCKED", b, "a person is deploying"), ("FAILED", b, "boom")]
        self.write(events)
        self.assertEqual(self.report(), [f"auto-deploy BLOCKED at {b[:12]}: a person is deploying", f"auto-deploy FAILED at {b[:12]}: boom"])

    def test_a_run_gating_blocked_outcome_is_reported_once_per_target(self) -> None:
        a = "a" * 40
        events = [("BLOCKED", a, "run-gating line"), ("BLOCKED", a, "run-gating line")]
        state = {"events": [{"seq": i + 1, "outcome": o, "commit": c, "detail": d, "gates_runs": True} for i, (o, c, d) in enumerate(events)]}
        (self.root / "auto-deploy.json").write_text(json.dumps(state))
        self.assertEqual(self.report(), [f"auto-deploy BLOCKED at {a[:12]}: run-gating line"])

    def test_a_manual_line_is_reported_once_while_it_stays_and_again_when_it_returns(self) -> None:
        line = {"line": "secret X in root: is missing", "action": "ask the operator"}
        self.write([], [line])
        self.assertEqual(self.report(), ["auto-deploy needs a person: secret X in root: is missing: ask the operator"])
        self.assertEqual(self.report(), [])
        self.write([], [])
        self.assertEqual(self.report(), [])
        self.write([], [line])
        self.assertEqual(len(self.report()), 1)


if __name__ == "__main__":
    unittest.main()
