from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "disable-auto-updates"
COMMERCE = "com.apple.commerce"
SOFTWARE_UPDATE = "com.apple.SoftwareUpdate"
MACOS_KEY = "AutomaticallyInstallMacOSUpdates"


@unittest.skipUnless(sys.platform == "darwin", "the update settings are macOS preferences")
class DisableAutoUpdatesTest(unittest.TestCase):

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.preferences = self.root / "Preferences"
        self.preferences.mkdir()

    def plist(self, domain: str) -> Path:
        return self.preferences / f"{domain}.plist"

    def run_script(self, env: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PREFERENCES_ROOT": str(self.root)} if env is None else env
        return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)

    def values(self) -> dict:
        return {
            domain: plistlib.loads(self.plist(domain).read_bytes()).get(key)
            for domain, key in ((COMMERCE, "AutoUpdate"), (SOFTWARE_UPDATE, MACOS_KEY))
        }

    def test_turns_both_settings_off_then_changes_nothing(self) -> None:
        self.plist(COMMERCE).write_bytes(plistlib.dumps({"AutoUpdate": True, "AutoUpdateRestartRequired": True}))

        first = self.run_script()
        after_first = {domain: self.plist(domain).read_bytes() for domain in (COMMERCE, SOFTWARE_UPDATE)}
        second = self.run_script()

        self.assertEqual((first.returncode, first.stderr), (0, ""))
        self.assertEqual(first.stdout, (
            f"{COMMERCE} AutoUpdate: was on, now off\n"
            f"{SOFTWARE_UPDATE} {MACOS_KEY}: was unset, now off\n"
        ))
        self.assertEqual(self.values(), {COMMERCE: False, SOFTWARE_UPDATE: False})
        self.assertTrue(plistlib.loads(self.plist(COMMERCE).read_bytes())["AutoUpdateRestartRequired"])
        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, "no change\n", ""))
        self.assertEqual({domain: self.plist(domain).read_bytes() for domain in after_first}, after_first)

    def test_changes_only_the_setting_that_is_on(self) -> None:
        self.plist(COMMERCE).write_bytes(plistlib.dumps({"AutoUpdate": False}))
        self.plist(SOFTWARE_UPDATE).write_bytes(plistlib.dumps({MACOS_KEY: True}))
        before = self.plist(COMMERCE).read_bytes()

        result = self.run_script()

        self.assertEqual((result.returncode, result.stdout), (0, f"{SOFTWARE_UPDATE} {MACOS_KEY}: was on, now off\n"))
        self.assertEqual(self.plist(COMMERCE).read_bytes(), before)
        self.assertEqual(self.values(), {COMMERCE: False, SOFTWARE_UPDATE: False})

    def test_refuses_without_root_when_it_would_change_the_machine(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("runs as root")
        env = {name: value for name, value in os.environ.items() if name != "PREFERENCES_ROOT"}

        result = self.run_script(env)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("sudo python3 skills/setup-factory/scripts/disable-auto-updates", result.stderr)


if __name__ == "__main__":
    unittest.main()
