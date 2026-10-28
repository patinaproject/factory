from __future__ import annotations

import concurrent.futures
import enum
import subprocess
import unittest
from types import SimpleNamespace

from tests import load_plugin

plugin = load_plugin()

LABEL = "ai.patinaproject.factory.auto-deploy"
SKIP = {"action": "skip", "reason": "a push to the factory's default branch starts auto-deploy, not a turn"}
PUSH = {
    "ref": "refs/heads/main",
    "before": "1" * 40,
    "after": "2" * 40,
    "repository": {"full_name": "example-org/factory"},
    "pusher": {"name": "example-app", "email": "pusher@example.com"},
    "sender": {"login": "example-app[bot]"},
    "head_commit": {"message": "feat: #1 ship it"},
}


class Platform(enum.Enum):
    WEBHOOK = "webhook"
    SLACK = "slack"


def event(payload, platform=Platform.WEBHOOK):
    return SimpleNamespace(source=SimpleNamespace(platform=platform, user_id="webhook:github"), raw_message=payload)


class Inline(concurrent.futures.Executor):

    def __init__(self):
        self.calls = 0

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as failure:
            future.set_exception(failure)
        return future


class FakeLaunchctl:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(argv, result, "", "Could not find service")


def kwargs():
    return {"env": {}, "capture_output": True, "text": True, "timeout": 10.0, "check": False}


class DispatchHookTest(unittest.TestCase):
    def hook(self, start, pool=None):
        self.pool = pool or Inline()
        return plugin.make_dispatch_hook(start, self.pool)

    def test_a_push_starts_the_job_and_skips_the_turn(self):
        started = []
        self.assertEqual(self.hook(lambda: started.append(True))(event=event(PUSH)), SKIP)
        self.assertEqual(started, [True])

    def test_a_push_skips_the_turn_even_when_the_start_raises(self):
        def start():
            raise RuntimeError("launchctl vanished")

        self.assertEqual(self.hook(start)(event=event(PUSH)), SKIP)

    def test_a_push_skips_the_turn_even_when_the_pool_refuses_it(self):
        pool = concurrent.futures.ThreadPoolExecutor(1)
        pool.shutdown()
        with self.assertLogs(plugin.logger, "ERROR") as logs:
            self.assertEqual(self.hook(lambda: None, pool)(event=event(PUSH)), SKIP)
        self.assertEqual(logs.output, [f"ERROR:factory_deploy:factory-deploy: could not schedule the start of {LABEL}: RuntimeError"])

    def test_the_hook_returns_before_the_job_starts(self):
        release = concurrent.futures.Future()
        pool = concurrent.futures.ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown)
        self.addCleanup(release.set_result, None)
        self.assertEqual(self.hook(lambda: release.result(timeout=5), pool)(event=event(PUSH)), SKIP)
        self.assertFalse(release.done())

    def test_payloads_that_are_not_pushes_pass_through_untouched(self):
        started = []
        hook = self.hook(lambda: started.append(True))
        cases = {
            "pull_request with a ref key": {"action": "closed", "ref": "refs/heads/main", "before": "1", "after": "2",
                                            "pusher": {}, "pull_request": {"merged": True}},
            "check_run with a ref key": {"action": "completed", "ref": "refs/heads/main", "check_run": {}},
            "push without a pusher": {key: value for key, value in PUSH.items() if key != "pusher"},
            "Linear issue": {"type": "Issue", "action": "update", "data": {}},
            "not an object": ["refs/heads/main"],
            "no payload": None,
        }
        for name, payload in cases.items():
            with self.subTest(name):
                self.assertIsNone(hook(event=event(payload)))
        self.assertIsNone(hook(event=event(PUSH, Platform.SLACK)))
        self.assertIsNone(hook(event=None))
        self.assertIsNone(hook())
        self.assertEqual((started, self.pool.calls), ([], 0))


class StartJobTest(unittest.TestCase):
    def test_kickstarts_the_gui_domain_with_an_empty_environment(self):
        launchctl = FakeLaunchctl(0)
        with self.assertLogs(plugin.logger, "INFO") as logs:
            plugin.start_job(launchctl, uid=501)
        self.assertEqual(launchctl.calls, [(["/bin/launchctl", "kickstart", f"gui/501/{LABEL}"], kwargs())])
        self.assertEqual(logs.output, [f"INFO:factory_deploy:factory-deploy: started {LABEL} in gui/501"])

    def test_falls_back_to_the_user_domain(self):
        launchctl = FakeLaunchctl(113, 0)
        plugin.start_job(launchctl, uid=501)
        self.assertEqual(launchctl.calls, [
            (["/bin/launchctl", "kickstart", f"gui/501/{LABEL}"], kwargs()),
            (["/bin/launchctl", "kickstart", f"user/501/{LABEL}"], kwargs()),
        ])

    def test_a_job_that_starts_in_neither_domain_is_logged_and_does_not_raise(self):
        with self.assertLogs(plugin.logger, "ERROR") as logs:
            plugin.start_job(FakeLaunchctl(113, 113), uid=501)
        self.assertEqual(logs.output, [
            f"ERROR:factory_deploy:factory-deploy: could not start {LABEL}: gui/501 exited 113; user/501 exited 113"
        ])

    def test_a_launchctl_that_hangs_or_is_missing_is_logged_and_does_not_raise(self):
        for failure in (subprocess.TimeoutExpired(["/bin/launchctl"], 10.0), FileNotFoundError("/bin/launchctl")):
            with self.subTest(type(failure).__name__), self.assertLogs(plugin.logger, "ERROR") as logs:
                plugin.start_job(FakeLaunchctl(failure), uid=501)
            self.assertEqual(logs.output, [
                f"ERROR:factory_deploy:factory-deploy: could not start {LABEL}: {type(failure).__name__}"
            ])

    def test_the_default_uid_is_this_process(self):
        import os

        launchctl = FakeLaunchctl(0)
        plugin.start_job(launchctl)
        self.assertEqual(launchctl.calls[0][0][2], f"gui/{os.getuid()}/{LABEL}")


class RegisterTest(unittest.TestCase):
    def test_registers_only_the_dispatch_hook(self):
        hooks = []
        plugin.register(SimpleNamespace(register_hook=lambda name, callback: hooks.append(name)))
        self.assertEqual(hooks, ["pre_gateway_dispatch"])


if __name__ == "__main__":
    unittest.main()
