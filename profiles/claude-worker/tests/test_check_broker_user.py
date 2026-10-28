from __future__ import annotations

import grp
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import pwd
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import SCRIPTS, git, with_interpreter
from tests.test_check_install import FAKE_LAUNCHCTL, check_install, classified

SETUP_SCRIPT = SCRIPTS.parents[2] / "skills" / "setup-factory" / "scripts" / "setup-broker-user"
BASE = "/Library/Application Support/factory-broker"
CONFIG = {
    "user": "_factorybroker", "group": "_factorybroker", "label": "local.factory-broker",
    "lib": f"{BASE}/lib", "socket": f"{BASE}/run/factory-uploads.sock", "home": f"{BASE}/home",
    "allowed_uids": [os.getuid()],
    "github": {"app_id": 11, "installation_id": 22, "private_key_path": f"{BASE}/home/github-app.pem"},
}
ME = pwd.getpwuid(os.getuid()).pw_name
CREDENTIALS = "plugins/factory-uploads/credentials.py"


def broker_account(shell="/usr/bin/false"):
    return pwd.struct_passwd(("_factorybroker", "*", os.getuid(), os.getgid(), "Factory broker", f"{BASE}/home", shell))


class CheckBrokerUserTest(unittest.TestCase):

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        seed = tmp / "seed"
        for source in check_install.BROKER_FILES.values():
            (seed / source).parent.mkdir(parents=True, exist_ok=True)
            (seed / source).write_text(f"code of {source}\n")
        (seed / CREDENTIALS).write_bytes((SCRIPTS.parents[2] / CREDENTIALS).read_bytes())
        git(seed.parent, "init", "-q", "-b", "main", str(seed))
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "seed")
        self.commit = check_install.Commit(seed, git(seed, "rev-parse", "HEAD").strip(), "main")

        self.launchd = tmp / "launchd"
        self.lib = self.launchd / BASE.lstrip("/") / "lib"
        (self.lib / "factory_uploads").mkdir(parents=True)
        for target, source in check_install.BROKER_FILES.items():
            (self.lib / target).write_bytes((seed / source).read_bytes())
            os.chmod(self.lib / target, 0o644)
        self.config_file = self.launchd / check_install.BROKER_CONFIG
        self.config_file.write_text(json.dumps(CONFIG))
        self.home = self.launchd / BASE.lstrip("/") / "home"
        self.home.mkdir(mode=0o700)
        self.key = self.home / "github-app.pem"
        self.key.write_text("key")
        # The tests run as one user, so mode 0000 stands in for a file only the broker user reads.
        os.chmod(self.key, 0o000)
        self.run_dir = self.launchd / BASE.lstrip("/") / "run"
        self.run_dir.mkdir()
        os.chmod(self.run_dir, 0o750)
        for path in (self.lib.parent, self.lib, self.lib / "factory_uploads"):
            os.chmod(path, 0o755)
        os.chmod(self.config_file, 0o644)
        self.plist = self.launchd / "Library/LaunchDaemons/local.factory-broker.plist"
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({"Label": "local.factory-broker", "RunAtLoad": True, "UserName": "_factorybroker"}))
        os.chmod(self.plist, 0o644)

        self.launchctl = tmp / "fake-launchctl"
        self.launchctl.write_text(with_interpreter(FAKE_LAUNCHCTL))
        os.chmod(self.launchctl, 0o755)
        self.states = tmp / "states.json"
        self.states.write_text(json.dumps({"system/local.factory-broker": "running"}))
        os.environ["FAKE_LAUNCHD_STATES"] = str(self.states)
        self.addCleanup(os.environ.pop, "FAKE_LAUNCHD_STATES", None)

        self.root = tmp / "hermes"
        self.root.mkdir()
        (self.root / "factory-uploads.sock").symlink_to(CONFIG["socket"])
        self.users = {"_factorybroker": broker_account()}
        self.groups = {"_factorybroker": grp.struct_group(("_factorybroker", "*", os.getgid(), [ME]))}

    def check(self, config=CONFIG) -> list:
        accounts = check_install.Accounts(self.users.get, self.groups.get, ME, root_uid=os.getuid())
        return classified(self, check_install.check_broker_user(
            self.commit, self.launchd, self.root, str(self.launchctl), config, accounts
        ))

    def test_a_broker_set_up_as_the_runbook_says_is_silent(self) -> None:
        self.assertEqual(self.check(), [])
        self.assertEqual(check_install.broker_user_config(self.launchd), CONFIG)

    def test_no_broker_config_means_the_gateway_runs_the_broker(self) -> None:
        self.assertIsNone(check_install.broker_user_config(self.launchd / "elsewhere"))

    def test_missing_accounts(self) -> None:
        self.users.clear()
        self.groups.clear()

        self.assertEqual(self.check(), [
            "broker user _factorybroker: does not exist; run setup-broker-user apply (part 5)",
            "broker user _factorybroker: has no group _factorybroker (part 5)",
        ])

    def test_an_account_a_person_could_sign_in_as(self) -> None:
        self.users["_factorybroker"] = broker_account(shell="/bin/zsh")

        self.assertEqual(self.check(), [
            "broker user _factorybroker: has the shell /bin/zsh, not /usr/bin/false, so a person could sign in as it (part 5)",
        ])

    def test_a_factory_user_outside_the_group_and_a_wider_allowlist(self) -> None:
        self.groups["_factorybroker"] = grp.struct_group(("_factorybroker", "*", os.getgid(), []))

        self.assertEqual(self.check({**CONFIG, "allowed_uids": [os.getuid(), 0]}), [
            f"broker user _factorybroker: group _factorybroker lacks {ME}, so no factory process reaches the socket (part 5)",
            f"broker user _factorybroker: allowed_uids is [{os.getuid()}, 0], not only {ME}'s uid (part 5)",
        ])

    def test_code_a_non_root_user_can_change_or_that_differs_from_the_commit(self) -> None:
        broker = self.lib / "factory_uploads" / "broker.py"
        os.chmod(broker, 0o664)
        (self.lib / "linear.py").write_text("tampered\n")

        self.assertEqual(self.check(), [
            f"broker user _factorybroker: {broker} is not root-owned and closed to other writers, "
            "so a factory process could change the broker (part 5)",
            f"broker user _factorybroker: {self.lib / 'linear.py'} differs from main at {self.commit.short}; "
            "run setup-broker-user apply (part 5)",
        ])

    def test_a_home_or_socket_directory_with_the_wrong_mode(self) -> None:
        os.chmod(self.home, 0o755)
        os.chmod(self.run_dir, 0o755)

        self.assertEqual(self.check(), [
            f"broker user _factorybroker: {self.home} is not _factorybroker's with mode 0700 (part 5)",
            f"broker user _factorybroker: {self.run_dir} is not _factorybroker's with mode 0750 (part 5)",
        ])

    def test_a_daemon_that_is_not_running(self) -> None:
        self.states.write_text("{}")
        self.plist.write_bytes(plistlib.dumps({"Label": "local.factory-broker", "RunAtLoad": True, "UserName": ME + "x"}))

        self.assertEqual(self.check(), [
            f"broker user _factorybroker: {self.plist} does not run at load as _factorybroker (part 5)",
            "broker user _factorybroker: local.factory-broker is not loaded, not running (part 5)",
        ])

    def test_a_missing_socket_link_and_broker_secrets_left_with_the_gateway(self) -> None:
        (self.root / "factory-uploads.sock").unlink()
        upload = "GITHUB_UPLOAD_TOKEN"
        (self.root / ".env").write_text(f"LINEAR_CLIENT_SECRET=lin-secret-FAKE\n{upload}=upload-FAKE\n")

        lines = self.check()

        self.assertEqual(lines, [
            f"broker user _factorybroker: {self.root / 'factory-uploads.sock'} is not a link to {CONFIG['socket']} (part 5)",
            f"broker user _factorybroker: {self.root}/.env holds {upload}, which only the broker user may hold (part 5)",
            f"broker user _factorybroker: {self.root}/.env holds LINEAR_CLIENT_SECRET, which only the broker user may hold (part 5)",
        ])
        self.assertNotIn("FAKE", "\n".join(lines))

    def test_a_github_app_key_the_factory_user_can_read(self) -> None:
        os.chmod(self.key, 0o400)

        self.assertEqual(self.check(), [
            f"broker user _factorybroker: {self.key} is readable by {ME}, so a factory process could read the GitHub App key (part 5)",
        ])

    def test_a_broker_without_the_github_app(self) -> None:
        config = {key: value for key, value in CONFIG.items() if key != "github"}

        self.assertEqual(self.check(config), [
            "broker user _factorybroker: broker.json names no GitHub App, so the worker's gh gets no installation token; "
            "add github to the input and run setup-broker-user apply (part 5)",
        ])

    def test_a_broker_config_without_its_paths_cannot_be_checked(self) -> None:
        self.config_file.write_text(json.dumps({"user": "_factorybroker"}))

        with self.assertRaises(check_install.Refusal):
            check_install.broker_user_config(self.launchd)

    def test_a_daemon_definition_a_non_root_user_can_change(self) -> None:
        os.chmod(self.plist, 0o666)

        self.assertEqual(self.check(), [
            f"broker user _factorybroker: {self.plist} is not root-owned and closed to other writers, "
            "so a factory process could change the broker (part 5)",
        ])


class BrokerUserConfigTableTest(unittest.TestCase):
    def test_the_gateways_platform_must_be_off_and_its_repository_list_is_not_required(self) -> None:
        rows = [entry for entry in check_install.BROKER_USER_CONFIG if entry.key.startswith("platforms.factory_uploads.")]

        self.assertEqual([entry.key for entry in rows], ["platforms.factory_uploads.enabled"])
        self.assertEqual([rows[0].want.ok(value) for value in (False, None, True)], [True, True, False])

    def test_neither_home_may_still_name_the_github_app_key(self) -> None:
        [row] = [entry for entry in check_install.BROKER_USER_CONFIG if entry.key.endswith(".github.private_key_path")]

        self.assertEqual(row.key, f"{check_install.SETTINGS_KEY}.github.private_key_path")
        self.assertEqual(row.homes, check_install.BOTH)
        self.assertEqual([row.want.ok(value) for value in (None, "", "/Users/factory/app.pem")], [True, True, False])
        self.assertEqual(len(check_install.BROKER_USER_CONFIG), len(check_install.CONFIG))


class SharedFileListTest(unittest.TestCase):
    def test_check_install_compares_exactly_the_files_the_setup_script_copies(self) -> None:
        loader = importlib.machinery.SourceFileLoader("setup_broker_user_for_check", str(SETUP_SCRIPT))
        setup = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        sys.modules[loader.name] = setup
        self.addCleanup(sys.modules.pop, loader.name, None)
        loader.exec_module(setup)

        self.assertEqual(check_install.BROKER_FILES, setup.SERVICE_FILES)


if __name__ == "__main__":
    unittest.main()
