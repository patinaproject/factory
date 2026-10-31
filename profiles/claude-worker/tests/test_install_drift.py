from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, git

sys.path.insert(0, str(SCRIPTS))
import install_drift  # noqa: E402

DEPLOYED = {
    "profiles/claude-worker/scripts/tool.py": "TOOL = 1\n",
    "profiles/claude-worker/SOUL.md": "soul\n",
    "plugins/blocker-order/result_line.py": "LINE = 1\n",
    "plugins/blocker-order/tests/test_line.py": "TEST = 1\n",
    "plugins/linear-agent-session/linear.py": "LINEAR = 1\n",
    "plugins/work/__init__.py": "WORK = 1\n",
}


def write(root: Path, files: dict) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


class DriftFixture(unittest.TestCase):
    object_format = "sha1"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main", f"--object-format={self.object_format}")
        self.commit(DEPLOYED)
        self.home = self.tmp / "home"
        write(self.home, {
            "scripts/tool.py": DEPLOYED["profiles/claude-worker/scripts/tool.py"],
            "plugins/blocker-order/result_line.py": DEPLOYED["plugins/blocker-order/result_line.py"],
            "plugins/linear-agent-session/linear.py": DEPLOYED["plugins/linear-agent-session/linear.py"],
        })

    def commit(self, files: dict) -> None:
        write(self.repo, files)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")


class InstallDriftTest(DriftFixture):
    def test_an_install_that_matches_head_has_no_skew(self) -> None:
        self.assertEqual(install_drift.skew(self.home, self.repo, None), [])

    def test_each_differing_part_is_named(self) -> None:
        (self.home / "plugins/blocker-order/result_line.py").write_text("LINE = 0\n")
        self.assertEqual(install_drift.skew(self.home, self.repo, None), ["blocker-order"])
        (self.home / "scripts/tool.py").unlink()
        (self.home / "plugins/linear-agent-session/linear.py").write_text("LINEAR = 0\n")
        self.assertEqual(install_drift.skew(self.home, self.repo, None), ["profile", "blocker-order", "linear-agent-session"])

    def test_a_commit_that_the_home_lacks_is_skew(self) -> None:
        self.commit({"plugins/linear-agent-session/broker_client.py": "BROKER = 1\n"})
        self.assertEqual(install_drift.skew(self.home, self.repo, None), ["linear-agent-session"])

    def test_tests_and_other_plugins_are_not_compared(self) -> None:
        self.commit({"plugins/blocker-order/tests/test_line.py": "TEST = 2\n", "plugins/work/__init__.py": "WORK = 2\n"})
        self.assertEqual(install_drift.skew(self.home, self.repo, None), [])

    def test_skew_compares_the_local_head_and_never_a_remote_ref(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "next")
        self.commit({"plugins/blocker-order/result_line.py": "LINE = 2\n"})
        git(self.repo, "update-ref", "refs/remotes/origin/main", "next")
        git(self.repo, "checkout", "-q", "main")
        self.assertEqual(install_drift.skew(self.home, self.repo, None), [])

    def test_a_directory_that_is_no_checkout_raises_the_modules_error(self) -> None:
        with self.assertRaises(install_drift.GitError):
            install_drift.skew(self.home, self.tmp / "home", None)

    def test_compare_files_reports_a_missing_and_a_different_file(self) -> None:
        commit = install_drift.Commit(self.repo, git(self.repo, "rev-parse", "HEAD"), "main")
        files = commit.files("plugins/blocker-order/result_line.py")
        (self.home / "plugins/blocker-order/result_line.py").write_text("LINE = 0\n")
        self.assertEqual(
            install_drift.compare_files(commit, files, "plugins/blocker-order/", self.home / "plugins/blocker-order"),
            [f"result_line.py differs from main at {commit.short}"],
        )
        self.assertEqual(
            install_drift.compare_files(commit, files, "plugins/blocker-order/", self.home / "nowhere"),
            ["result_line.py is missing"],
        )

    def test_compare_files_reads_bytes_the_way_git_does(self) -> None:
        binary = bytes(range(256)) + b"\r\n\0end"
        (self.repo / "blob.bin").write_bytes(binary)
        git(self.repo, "add", "blob.bin")
        git(self.repo, "commit", "-q", "-m", "binary")
        (self.home / "blob.bin").write_bytes(binary)
        commit = install_drift.Commit(self.repo, git(self.repo, "rev-parse", "HEAD"), "main")
        self.assertEqual(install_drift.compare_files(commit, commit.files("blob.bin"), "", self.home), [])


class Sha256InstallDriftTest(InstallDriftTest):
    object_format = "sha256"

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            probe = subprocess.run(["git", "init", "--object-format=sha256", "-q", scratch], capture_output=True)
        if probe.returncode != 0:
            raise unittest.SkipTest("this git has no sha256 repositories")


if __name__ == "__main__":
    unittest.main()


class DeployedCommitTest(DriftFixture):
    def setUp(self) -> None:
        super().setUp()
        self.deployed = git(self.repo, "rev-parse", "HEAD")
        self.commit({"plugins/blocker-order/result_line.py": "LINE = 2\n"})

    def test_a_checkout_ahead_of_the_deployed_commit_has_no_skew_against_it(self) -> None:
        self.assertEqual(install_drift.skew(self.home, self.repo, self.deployed), [])
        self.assertEqual(install_drift.skew(self.home, self.repo, None), ["blocker-order"])

    def test_an_install_that_differs_from_the_deployed_commit_is_skew(self) -> None:
        (self.home / "scripts/tool.py").write_text("TOOL = 0\n")
        self.assertEqual(install_drift.skew(self.home, self.repo, self.deployed), ["profile"])

    def test_a_deployed_commit_the_checkout_lacks_raises_and_names_it(self) -> None:
        with self.assertRaises(install_drift.MissingCommit) as raised:
            install_drift.skew(self.home, self.repo, "f" * 40)
        self.assertIn("f" * 12, str(raised.exception))

    def test_the_state_file_beside_the_profiles_directory_names_the_deployed_commit(self) -> None:
        worker = self.tmp / "root" / "profiles" / "claude-worker"
        worker.mkdir(parents=True)
        self.assertEqual(install_drift.deploy_state(worker), {})
        (self.tmp / "root" / "auto-deploy.json").write_text("{")
        self.assertEqual(install_drift.deploy_state(worker), {})
        (self.tmp / "root" / "auto-deploy.json").write_text('{"deployed": null}')
        self.assertIsNone(install_drift.deployed(install_drift.deploy_state(worker)))
        (self.tmp / "root" / "auto-deploy.json").write_text(f'{{"deployed": "{self.deployed}"}}')
        self.assertEqual(install_drift.deployed(install_drift.deploy_state(worker)), self.deployed)
