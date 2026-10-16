from __future__ import annotations

import subprocess
import unittest
from pathlib import PurePosixPath

from tests.support import PLUGIN

REPO = PLUGIN.parents[1]
NAME = b"GITHUB_UPLOAD_TOKEN"
CODE_SUFFIXES = {".py", ".sh", ".js", ".mjs", ".cjs", ".ts", ".json", ".yaml", ".yml", ".toml"}
VENDORED = (".agents/", ".claude/", "node_modules/")


def tracked_code_files() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True
    ).stdout.decode().split("\0")
    found = []
    for name in filter(None, listing):
        path = PurePosixPath(name)
        if name.startswith(VENDORED) or "tests" in path.parts or path.suffix == ".md":
            continue
        file = REPO / name
        if file.is_symlink() or not file.is_file():
            continue
        if path.suffix in CODE_SUFFIXES or file.read_bytes().startswith(b"#!"):
            found.append(name)
    return found


class TokenContainmentTest(unittest.TestCase):
    def test_only_the_adapter_the_upload_module_and_the_strip_list_name_the_token(self):
        naming = sorted(name for name in tracked_code_files() if NAME in (REPO / name).read_bytes())

        self.assertEqual(
            naming,
            [
                "plugins/factory-secret-names/plugin.yaml",
                "plugins/factory-uploads/adapter.py",
                "plugins/factory-uploads/upload_token.py",
            ],
        )

    def test_the_scan_sees_the_worker_scripts_it_is_meant_to_cover(self):
        scanned = tracked_code_files()

        for expected in (
            "profiles/claude-worker/scripts/claude-session",
            "profiles/claude-worker/scripts/github-upload-image",
            "profiles/claude-worker/config.yaml",
            "plugins/factory-uploads/broker.py",
        ):
            self.assertIn(expected, scanned)


if __name__ == "__main__":
    unittest.main()
