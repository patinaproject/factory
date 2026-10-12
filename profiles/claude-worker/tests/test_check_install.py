from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, git

ROUTES = {"linear": {"events": ["AgentSessionEvent"], "secret": "${LINEAR_WEBHOOK_SECRET}", "prompt": "triage"}}

RENDER = f"""import json
print(json.dumps({ROUTES!r}))
"""

FAKE_HERMES = """#!/usr/bin/env python3
import os, sys
name = sys.argv[3].rsplit(".", 1)[1]
path = os.path.join(os.environ["HERMES_HOME"], "installed-routes", name + ".json")
if not os.path.exists(path):
    sys.exit("Config key not set: " + sys.argv[3])
print(open(path).read())
"""

REPO_FILES = {
    "profiles/claude-worker/SOUL.md": "soul\n",
    "profiles/claude-worker/config.yaml": "model: repo\n",
    "profiles/claude-worker/scripts/x": "#!/bin/sh\n",
    "plugins/p1/__init__.py": "plugin\n",
    "plugins/p1/tests/test_a.py": "test\n",
    "templates/render.py": RENDER,
}


class CheckInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        seed = self.tmp / "seed"
        for path, text in REPO_FILES.items():
            (seed / path).parent.mkdir(parents=True, exist_ok=True)
            (seed / path).write_text(text)
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "seed")
        self.sha = git(seed, "rev-parse", "--short=7", "HEAD")
        self.repo = self.tmp / "factory"
        git(self.tmp, "clone", "-q", str(seed), str(self.repo))

        self.root = self.tmp / "hermes"
        self.worker = self.root / "profiles" / "claude-worker"
        for home in (self.root, self.worker):
            self.install(home, "plugins/p1/__init__.py", "plugin\n")
        self.install(self.worker, "SOUL.md", "soul\n")
        self.install(self.worker, "scripts/x", "#!/bin/sh\n")
        self.install(self.worker, "config.yaml", "model: local\n")
        self.install(
            self.worker, "distribution.yaml", f"name: claude-worker\nsource: {self.repo}/profiles/claude-worker\n"
        )
        installed = {**ROUTES["linear"], "secret": "abc...1234"}
        self.install(self.root, "installed-routes/linear.json", json.dumps(installed))

        self.hermes = self.tmp / "bin" / "fake-hermes"
        self.install(self.tmp, "bin/fake-hermes", FAKE_HERMES)
        os.chmod(self.hermes, 0o755)

    def install(self, home: Path, path: str, text: str) -> None:
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)

    def check(self, *args: str) -> tuple:
        env = {**os.environ, "HERMES_HOME": str(self.worker), "HERMES_BIN": str(self.hermes)}
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "check-install"), *args], env=env, capture_output=True, text=True
        )
        return proc.returncode, proc.stdout

    def test_matching_install_is_silent(self) -> None:
        self.assertEqual(self.check(), (0, ""))

    def test_plugin_difference_names_the_home(self) -> None:
        self.install(self.worker, "plugins/p1/__init__.py", "old\n")

        self.assertEqual(
            self.check(),
            (1, f"plugin p1 in {self.worker}: __init__.py differs from main at {self.sha}\n"),
        )

    def test_plugin_missing_from_root_home(self) -> None:
        shutil.rmtree(self.root / "plugins" / "p1")

        self.assertEqual(self.check(), (1, f"plugin p1 in {self.root}: not installed\n"))

    def test_plugin_tests_are_ignored(self) -> None:
        self.install(self.worker, "plugins/p1/tests/test_a.py", "different\n")

        self.assertEqual(self.check(), (0, ""))

    def test_profile_differences(self) -> None:
        self.install(self.worker, "SOUL.md", "old soul\n")
        (self.worker / "scripts" / "x").unlink()

        self.assertEqual(
            self.check(),
            (
                1,
                f"profile claude-worker: SOUL.md differs from main at {self.sha}\n"
                "profile claude-worker: scripts/x is missing\n",
            ),
        )

    def test_route_differences(self) -> None:
        stale = {**ROUTES["linear"], "prompt": "old triage", "secret": "abc...1234"}
        self.install(self.root, "installed-routes/linear.json", json.dumps(stale))

        self.assertEqual(
            self.check(), (1, f"webhook route linear: differs from the route rendered at {self.sha}\n")
        )

    def test_missing_route(self) -> None:
        (self.root / "installed-routes" / "linear.json").unlink()

        self.assertEqual(self.check(), (1, f"webhook route linear: not installed in {self.root}\n"))

    def test_a_merge_after_the_last_fetch_is_seen(self) -> None:
        seed = self.tmp / "seed"
        (seed / "plugins" / "p1" / "__init__.py").write_text("merged\n")
        git(seed, "commit", "-qam", "merge")
        merged = git(seed, "rev-parse", "--short=7", "HEAD")

        self.assertEqual(self.check("--no-fetch"), (0, ""))
        self.assertEqual(
            self.check(),
            (1, f"plugin p1 in {self.root}: __init__.py differs from main at {merged}\n"
                f"plugin p1 in {self.worker}: __init__.py differs from main at {merged}\n"),
        )

    def test_checkout_state_does_not_matter(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "plugins" / "p1" / "__init__.py").write_text("dirty\n")
        (self.repo / "profiles" / "claude-worker" / "SOUL.md").write_text("dirty\n")

        self.assertEqual(self.check(), (0, ""))

    def test_hermes_is_found_in_the_installer_bin_without_path(self) -> None:
        home = self.tmp / "home"
        self.install(home, ".local/bin/hermes", FAKE_HERMES)
        os.chmod(home / ".local" / "bin" / "hermes", 0o755)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "HERMES_HOME": str(self.worker)}
        proc = subprocess.run(
            ["/usr/bin/env", "python3", str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True
        )

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_worker_home_outside_profiles_is_refused(self) -> None:
        env = {**os.environ, "HERMES_HOME": str(self.root), "HERMES_BIN": str(self.hermes)}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True)

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))


if __name__ == "__main__":
    unittest.main()
