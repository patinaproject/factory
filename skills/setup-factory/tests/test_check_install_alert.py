from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-install-alert"

FAKE_CHECK_INSTALL = """\
import os, sys
print("home=" + os.environ["HERMES_HOME"])
print("args=" + ",".join(sys.argv[1:]))
sys.exit(1)
"""


class CheckInstallAlertTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.worker = self.root / "profiles" / "claude-worker"

    def run_alert(self, *args: str, home: Path | None = None) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items() if key != "HERMES_HOME"}
        if home is not None:
            env["HERMES_HOME"] = str(home)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)

    def test_runs_the_workers_check_install_in_the_worker_home(self) -> None:
        (self.worker / "scripts").mkdir(parents=True)
        (self.worker / "scripts" / "check-install").write_text(FAKE_CHECK_INSTALL)

        proc = self.run_alert("--no-fetch", home=self.root)

        self.assertEqual(
            (proc.returncode, proc.stdout), (1, f"home={self.worker}\nargs=--no-fetch\n")
        )

    def test_refuses_without_the_workers_script(self) -> None:
        proc = self.run_alert(home=self.root)

        self.assertEqual(
            (proc.returncode, proc.stderr),
            (1, f"check-install-alert: {self.worker}/scripts/check-install does not exist\n"),
        )

    def test_refuses_without_a_home(self) -> None:
        proc = self.run_alert()

        self.assertEqual(
            (proc.returncode, proc.stderr), (1, "check-install-alert: HERMES_HOME is not set\n")
        )


if __name__ == "__main__":
    unittest.main()
