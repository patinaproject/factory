"""Run a test module's cases with the shadow router off and then on, and require both runs to observe the same thing.

`webhook-retry`'s tests import this file by path, so it imports nothing from this package.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import unittest
from typing import Any, Callable, ContextManager
from unittest import mock

Mode = Callable[[unittest.TestCase, bool], ContextManager[list]]
TEMPORARY = re.compile(
    "(?:" + "|".join(re.escape(root) for root in {os.path.realpath(tempfile.gettempdir()), tempfile.gettempdir()}) + r")/[^/'\" ]+"
)


def normalized(observed: list) -> str:
    """Each run makes its own temporary directories, so their names are not part of what a run observed."""
    return TEMPORARY.sub("<tmp>", repr(observed))


class BothModes(unittest.TestCase):
    def __init__(self, test: unittest.TestCase, mode: Mode) -> None:
        super().__init__("compare")
        self.test = test
        self.mode = mode

    def id(self) -> str:
        return self.test.id()

    def __str__(self) -> str:
        return f"{self.test} with the router off and on"

    def shortDescription(self):
        return None

    def compare(self) -> None:
        observed = {}
        for on in (False, True):
            case = type(self.test)(self.test._testMethodName)
            result = unittest.TestResult()
            with self.mode(case, on) as seen:
                case.run(result)
            if result.skipped:
                self.skipTest(result.skipped[0][1])
            for _, trace in result.errors + result.failures:
                self.fail(f"with the router {'on' if on else 'off'}:\n{trace}")
            observed[on] = normalized(seen)
        self.assertEqual(observed[False], observed[True], "the router changed what the hook or the retry did")


def cases(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


def load_tests_in_both_modes(mode: Mode) -> Callable[..., unittest.TestSuite]:
    def load_tests(loader, tests, pattern):
        return unittest.TestSuite(BothModes(test, mode) for test in cases(tests))

    return load_tests


class RecordingProxy:
    """Delegates to the real object and logs each method call, so a test's own assertions on it still hold."""

    def __init__(self, target: Any, log: list, name: str) -> None:
        self._target = target
        self._log = log
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        value = getattr(self._target, attribute)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            self._log.append((self._name, attribute, args, kwargs))
            return value(*args, **kwargs)

        return call


@contextlib.contextmanager
def recorded_subprocess(log: list):
    real = subprocess.run

    def run(args, *rest, **kwargs):
        log.append(("subprocess.run", args))
        return real(args, *rest, **kwargs)

    with mock.patch.object(subprocess, "run", run):
        yield


def dispatch_hook_mode(plugin: Any) -> Mode:
    """Builds each dispatch hook with `router_shadow` fixed, logs its returns and its client and subprocess calls, and
    with the router on waits for each decision to be written before the test goes on."""

    @contextlib.contextmanager
    def mode(case: unittest.TestCase, on: bool):
        log: list = []
        real_make = plugin.make_dispatch_hook

        def make(get_client, *args, **kwargs):
            kwargs["router_shadow"] = lambda: on
            hook = real_make(lambda: RecordingProxy(get_client(), log, "client"), *args, **kwargs)

            async def observed(*hook_args, **hook_kwargs):
                result = await hook(*hook_args, **hook_kwargs)
                plugin.router.drain()
                log.append(("return", result))
                return result

            return observed

        with mock.patch.object(plugin, "make_dispatch_hook", make), recorded_subprocess(log):
            yield log

    return mode
