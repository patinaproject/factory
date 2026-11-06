"""One test per isolation claim. Each class says whether it needs Docker; only `ContainerTest` does."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from patina_evals import isolation, loader, task
from patina_evals.manifest import Solution
from tests.support import EVALS, run_synthetic, synthetic_case

LIMITS = isolation.Limits()


def manifests():
    return {m.id: m for m in loader.load("manifest-dir", EVALS / "fixtures", EVALS / "graders")}


def run(trial, argv, **kw):
    return subprocess.run(isolation.command(trial, argv, LIMITS), cwd=trial.repo, capture_output=True, text=True, **kw)


class TrialTestCase(unittest.TestCase):
    NEEDS_DOCKER = False

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.base = Path(self.scratch.name)
        self.manifest = manifests()["triage-priority-allowed"]

    def tearDown(self):
        self.scratch.cleanup()

    def trial(self, name="a"):
        return isolation.prepare(self.base / name, self.manifest, name)


class FreshStateTest(TrialTestCase):
    """Needs Docker: no."""

    def test_no_file_session_or_service_state_crosses_trials(self):
        a, b = self.trial("a"), self.trial("b")
        run(a, ["/bin/sh", "-c", "echo leak > $HOME/marker && echo leak > marker && echo leak > $TMPDIR/marker"])
        run(a, isolation.tool_argv(a, "tracker", ["set-priority", "ISSUE-1", "1"]))
        self.assertTrue((a.home / "marker").exists())
        for path in (b.home / "marker", b.repo / "marker", b.tmp / "marker"):
            self.assertFalse(path.exists(), path)
        self.assertEqual(isolation.git(b.repo, "status", "--porcelain"), "")
        self.assertIn('"writes": []', b.tracker_state.read_text())
        self.assertNotIn('"writes": []', a.tracker_state.read_text())

    def test_parallel_trials_through_the_harness_do_not_see_each_other(self):
        grader = '''
PROBES = {{"home": {{"argv": ["/bin/sh", "-c", "ls $HOME; cat $TRACKER_STATE"]}}}}


def grade(observed):
    out = observed["probes"]["home"]["stdout"]
    return {{"checks": {{"answer_is_yes": "mine-{me}" in out and "mine-{other}" not in out}}, "forbidden": {{}}}}
'''
        for me, other in (("left", "right"), ("right", "left")):
            stream = [{"tool": "sh", "args": ["-c", f"touch $HOME/mine-{me}; sleep 1"]}]
            synthetic_case(self.base, me, reference_stream=stream, grader=grader.format(me=me, other=other))
        code, result, output = run_synthetic(self.base, "--split", "development", "--suite", "regression", "--concurrency", "2")
        self.assertEqual(code, 0, output)
        self.assertEqual([c["status"] for c in result["cases"]], ["pass", "pass"])

    def test_environment_is_an_allowlist(self):
        os.environ["PATINA_EVALS_SECRET_PROBE"] = "hidden"
        try:
            out = run(self.trial(), ["/bin/sh", "-c", "env"]).stdout
        finally:
            del os.environ["PATINA_EVALS_SECRET_PROBE"]
        self.assertNotIn("PATINA_EVALS_SECRET_PROBE", out)
        self.assertIn(f"HOME={self.base / 'a' / 'home'}", out)


class HistoryTest(TrialTestCase):
    """Needs Docker: no."""

    def test_trial_repository_has_one_commit_and_no_way_to_more_history(self):
        trial = self.trial()
        self.assertEqual(isolation.git(trial.repo, "rev-list", "--all", "--count").strip(), "1")
        self.assertEqual(isolation.git(trial.repo, "remote").strip(), "")
        self.assertFalse((trial.repo / ".git" / "objects" / "info" / "alternates").exists())
        self.assertFalse((trial.repo / ".git" / "shallow").exists())


class HiddenAnswerTest(TrialTestCase):
    """Needs Docker: no. The local sandbox hides answers by never copying them; `ContainerTest` proves the mount boundary."""

    def test_no_reference_grader_or_answer_file_enters_the_trial(self):
        trial = self.trial()
        hidden = {p.read_bytes() for p in self.manifest.grader_dir.rglob("*") if p.is_file()}
        visible = [p for p in trial.root.rglob("*") if p.is_file() and ".git" not in p.parts]
        self.assertTrue(visible)
        for path in visible:
            self.assertNotIn(path.read_bytes(), hidden, path)
            self.assertNotIn("graders", path.parts)

    def test_trial_environment_names_no_harness_path(self):
        env = isolation.env_for(self.trial())
        for value in env.values():
            self.assertNotIn(str(EVALS / "graders"), value)
            self.assertNotIn(str(EVALS / "fixtures"), value)


class WriterTest(TrialTestCase):
    """Needs Docker: no."""

    def test_second_writer_is_refused(self):
        trial = self.trial()
        lock = isolation.acquire_writer(trial, "first")
        with self.assertRaises(isolation.WriterBusy):
            isolation.acquire_writer(trial, "second")
        isolation.release_writer(lock)
        isolation.release_writer(isolation.acquire_writer(trial, "third"))

    def test_each_trial_has_its_own_worktree(self):
        a, b = self.trial("a"), self.trial("b")
        self.assertNotEqual(a.repo.resolve(), b.repo.resolve())
        isolation.acquire_writer(a, "a")
        isolation.acquire_writer(b, "b")


class ProcessTest(TrialTestCase):
    """Needs Docker: no."""

    def test_background_process_is_killed_at_teardown(self):
        trial = self.trial()
        run(trial, ["/bin/sh", "-c", "sleep 300 > /dev/null 2>&1 &"])
        leaked = isolation.teardown(trial)
        self.assertEqual(len(leaked), 1)
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.killpg(leaked[0], 0)

    def test_cpu_ceiling_is_enforced(self):
        trial = self.trial()
        tight = isolation.Limits(cpu_seconds=1)
        proc = subprocess.run(
            isolation.command(trial, ["python3", "-c", "while True: pass"], tight), cwd=trial.repo, capture_output=True, timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)


class NetworkTest(TrialTestCase):
    """Needs Docker: no. Provider and proxy variables point at a refusing address."""

    def test_external_write_cannot_leave_the_trial(self):
        trial = self.trial()
        env = isolation.env_for(trial)
        for name in isolation.BLOCKED_URL_VARS:
            self.assertEqual(env[name], isolation.BLOCKED_URL)
        code = "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://example.com/', data=b'x'), timeout=5)"
        proc = run(trial, ["python3", "-c", code])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refused", proc.stderr.lower())


class TamperTest(TrialTestCase):
    """Needs Docker: no."""

    def test_edit_to_a_protected_test_file_fails_the_trial(self):
        manifest = manifests()["token-spend-supported-usage"]
        trial = isolation.prepare(self.base / "t", manifest, "t")
        before = isolation.fingerprint(trial.repo, manifest.protected_paths)
        (trial.repo / "tests" / "test_spend.py").write_text("pass\n")
        self.assertNotEqual(isolation.fingerprint(trial.repo, manifest.protected_paths), before)
        checks = dict.fromkeys(manifest.acceptance_checks, True)
        forbidden = dict.fromkeys(manifest.forbidden_outcomes, False)
        _, flagged, passed = task.verdict_of(manifest, {"checks": checks, "forbidden": forbidden}, tampered=True)
        self.assertFalse(passed)
        self.assertTrue(flagged[task.TAMPERED])

    def test_edit_to_a_grader_is_detected(self):
        copy = self.base / "graders"
        shutil.copytree(EVALS / "graders" / "token-spend-supported-usage", copy)
        before = isolation.tree_fingerprint(copy)
        with open(copy / "grader.py", "a") as handle:
            handle.write("\n")
        self.assertNotEqual(isolation.tree_fingerprint(copy), before)

    def test_tamper_outcome_counts_only_when_targeted(self):
        solution = Solution("tamper-tests", "fail", ("grader_tampered",))
        self.assertEqual(task.outcome(solution, {"a": True}, {task.TAMPERED: True}, False), "ok")
        self.assertEqual(task.outcome(solution, {"a": False}, {task.TAMPERED: False}, False), "unexpected")


@unittest.skipUnless(shutil.which("docker") or os.environ.get("EVALS_REQUIRE_DOCKER"), "needs Docker; runs in the Evals CI job")
class ContainerTest(TrialTestCase):
    """Needs Docker: yes. A trial mounted alone in a container sees no harness files and has no network."""

    NEEDS_DOCKER = True
    IMAGE = "python:3.14-slim"

    def test_container_trial_has_no_network_and_no_graders(self):
        trial = self.trial()
        script = (
            "import os, socket, sys\n"
            "for d, subdirs, _ in os.walk('/'):\n"
            "    subdirs[:] = [s for s in subdirs if os.path.join(d, s) not in ('/proc', '/sys', '/dev')]\n"
            "    assert 'graders' not in d and 'patina_evals' not in d, d\n"
            "s = socket.socket(); s.settimeout(5)\n"
            "try:\n    s.connect(('1.1.1.1', 80))\nexcept OSError:\n    sys.exit(0)\nsys.exit('network reachable')\n"
        )
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only", "-v", f"{trial.root}:/trial", "-w", "/trial/repo",
             self.IMAGE, "python3", "-c", script],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
