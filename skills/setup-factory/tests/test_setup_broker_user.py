from __future__ import annotations

import ast
import contextlib
import grp
import io
import importlib.machinery
import importlib.util
import json
import os
import pwd
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup-broker-user"
loader = importlib.machinery.SourceFileLoader("setup_broker_user", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
setup = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = setup
loader.exec_module(setup)

REPO = SCRIPT.parents[3]
LAYOUT = setup.Layout()
INPUT = {
    "factory_user": "factory",
    "repositories": {"example-org/example-repo": 123},
    "claude_session": {"auth_token_env": "EXAMPLE_PROXY_TOKEN"},
    "infisical": None,
}
ROOT_DIR = setup.Entry(0, 0, 0o755)
REAL_PYTHON = Path("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3")


def person(name, uid, gid=20, shell="/bin/zsh"):
    return pwd.struct_passwd((name, "*", uid, gid, name, f"/Users/{name}", shell))


def group(name, gid, members=()):
    return grp.struct_group((name, "*", gid, list(members)))


class Machine:

    def __init__(self):
        self.users = {"factory": person("factory", 501)}
        self.groups = {}
        self.entries = {Path("/Library/Application Support"): ROOT_DIR}
        for path in (setup.INTERPRETER, REAL_PYTHON, REAL_PYTHON.parent.parent / "lib"):
            self.entries.update({parent: ROOT_DIR for parent in [path, *path.parents]})
        self.entries[setup.INTERPRETER] = setup.Entry(0, 0, 0o755, link=True)
        self.loaded = set()
        self.ids = {0, 20, 499}

    def system(self, platform="darwin"):
        return setup.System(
            user=self.users.get, group=self.groups.get, used_ids=lambda: set(self.ids),
            entry=self.entries.get, loaded=lambda label: label in self.loaded, euid=501, platform=platform,
            realpath=lambda path: REAL_PYTHON if path == setup.INTERPRETER else path,
        )

    def converge(self, steps):
        for step in steps:
            if isinstance(step, setup.Run):
                argv = step.argv
                if argv[:3] == (setup.DSEDITGROUP, "-o", "create"):
                    self.groups[argv[-1]] = group(argv[-1], int(argv[6]))
                elif argv[:3] == (setup.DSEDITGROUP, "-o", "edit"):
                    self.groups[argv[-1]].gr_mem.append(argv[4])
                elif argv[:3] == (setup.DSCL, ".", "-create") and len(argv) == 4:
                    self.users[LAYOUT.user] = person(LAYOUT.user, 0)
                elif argv[:3] == (setup.DSCL, ".", "-create"):
                    old = self.users[LAYOUT.user]
                    fields = {"UniqueID": 2, "PrimaryGroupID": 3, "UserShell": 6}
                    if argv[4] in fields:
                        values = list(old)
                        values[fields[argv[4]]] = int(argv[5]) if argv[4] != "UserShell" else argv[5]
                        self.users[LAYOUT.user] = pwd.struct_passwd(values)
                elif argv[:2] == (setup.LAUNCHCTL, "bootstrap"):
                    self.loaded.add(LAYOUT.label)
            else:
                uid = 0 if step.owner == "root" else self.users[step.owner].pw_uid
                gid = 0 if step.group == "wheel" else self.groups[step.group].gr_gid
                content = step.content if isinstance(step, setup.File) else None
                self.entries[step.path] = setup.Entry(uid, gid, step.mode, content)


def lines(steps):
    return [str(step) for step in steps]


class PlanTest(unittest.TestCase):
    def test_a_fresh_machine_gets_the_user_the_group_the_files_and_the_daemon(self):
        steps = setup.plan(INPUT, Machine().system())

        self.assertEqual(lines(steps), [
            "run    /usr/sbin/dseditgroup -o create -r 'Factory broker' -i 498 _factorybroker",
            "run    /usr/bin/dscl . -create /Users/_factorybroker",
            "run    /usr/bin/dscl . -create /Users/_factorybroker UniqueID 498",
            "run    /usr/bin/dscl . -create /Users/_factorybroker PrimaryGroupID 498",
            "run    /usr/bin/dscl . -create /Users/_factorybroker UserShell /usr/bin/false",
            "run    /usr/bin/dscl . -create /Users/_factorybroker NFSHomeDirectory '/Library/Application Support/factory-broker/home'",
            "run    /usr/bin/dscl . -create /Users/_factorybroker RealName 'Factory broker'",
            "run    /usr/bin/dscl . -create /Users/_factorybroker Password '*'",
            "run    /usr/bin/dscl . -create /Users/_factorybroker IsHidden 1",
            "run    /usr/sbin/dseditgroup -o edit -a factory -t user _factorybroker",
            "dir    /Library/Application Support/factory-broker root:wheel 0755",
            "dir    /Library/Application Support/factory-broker/lib root:wheel 0755",
            "dir    /Library/Application Support/factory-broker/lib/factory_uploads root:wheel 0755",
            "dir    /Library/Application Support/factory-broker/home _factorybroker:_factorybroker 0700",
            "dir    /Library/Application Support/factory-broker/run _factorybroker:_factorybroker 0750",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/__init__.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/broker.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/credentials.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/service.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/upload_token.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/linear.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/broker.json root:wheel 0644",
            "write  /Library/LaunchDaemons/local.factory-broker.plist root:wheel 0644",
            "write  /Library/Application Support/factory-broker/home/.env _factorybroker:_factorybroker 0600",
            "run    /bin/launchctl bootstrap system /Library/LaunchDaemons/local.factory-broker.plist",
        ])

    def test_the_config_allows_only_the_factory_users_uid_and_holds_no_secret(self):
        steps = setup.plan(INPUT, Machine().system())

        [config] = [s for s in steps if isinstance(s, setup.File) and s.path == LAYOUT.config]
        self.assertEqual(json.loads(config.content), {
            "user": "_factorybroker", "group": "_factorybroker", "label": "local.factory-broker",
            "lib": "/Library/Application Support/factory-broker/lib",
            "socket": "/Library/Application Support/factory-broker/run/factory-uploads.sock",
            "home": "/Library/Application Support/factory-broker/home",
            "allowed_uids": [501],
            "repositories": {"example-org/example-repo": 123},
            "claude_session": {"auth_token_env": "EXAMPLE_PROXY_TOKEN"},
            "infisical": None,
        })

    def test_the_daemon_runs_the_root_owned_copy_as_the_broker_user(self):
        steps = setup.plan(INPUT, Machine().system())

        [plist] = [s for s in steps if isinstance(s, setup.File) and s.path == LAYOUT.plist]
        import plistlib
        loaded = plistlib.loads(plist.content)
        self.assertEqual(loaded["UserName"], "_factorybroker")
        self.assertEqual(loaded["ProgramArguments"], [
            "/Library/Developer/CommandLineTools/usr/bin/python3", "-I", "-B", "/Library/Application Support/factory-broker/lib/factory_uploads/service.py",
            "--config", "/Library/Application Support/factory-broker/broker.json",
        ])
        self.assertTrue(loaded["RunAtLoad"])
        self.assertTrue(loaded["KeepAlive"])

    def test_the_copied_code_is_the_repositorys_files(self):
        steps = setup.plan(INPUT, Machine().system())

        written = {s.path: s.content for s in steps if isinstance(s, setup.File)}
        self.assertEqual(written[LAYOUT.lib / "factory_uploads" / "broker.py"], (REPO / "plugins/factory-uploads/broker.py").read_bytes())
        self.assertEqual(written[LAYOUT.lib / "linear.py"], (REPO / "plugins/linear-agent-session/linear.py").read_bytes())

    def test_a_second_run_changes_nothing(self):
        machine = Machine()
        machine.converge(setup.plan(INPUT, machine.system()))

        self.assertEqual(setup.plan(INPUT, machine.system()), [])

    def test_changed_code_rewrites_that_file_and_restarts_the_service(self):
        machine = Machine()
        machine.converge(setup.plan(INPUT, machine.system()))
        target = LAYOUT.lib / "factory_uploads" / "broker.py"
        machine.entries[target] = setup.Entry(0, 0, 0o644, b"older code")

        self.assertEqual(lines(setup.plan(INPUT, machine.system())), [
            f"write  {target} root:wheel 0644",
            "run    /bin/launchctl kickstart -k system/local.factory-broker",
        ])

    def test_a_changed_daemon_definition_is_reloaded(self):
        machine = Machine()
        machine.converge(setup.plan(INPUT, machine.system()))
        machine.entries[LAYOUT.plist] = setup.Entry(0, 0, 0o644, b"<plist/>")

        self.assertEqual(lines(setup.plan(INPUT, machine.system()))[-2:], [
            "run    /bin/launchctl bootout system/local.factory-broker",
            "run    /bin/launchctl bootstrap system /Library/LaunchDaemons/local.factory-broker.plist",
        ])

    def test_a_home_the_planner_cannot_read_keeps_its_env_file(self):
        machine = Machine()
        machine.converge(setup.plan(INPUT, machine.system()))
        machine.entries[LAYOUT.home / ".env"] = setup.UNREADABLE_WITHOUT_ROOT

        self.assertEqual(setup.plan(INPUT, machine.system()), [])

    def test_the_plan_only_ever_changes_the_brokers_own_accounts(self):
        machine = Machine()
        machine.groups["_factorybroker"] = group("_factorybroker", 498)
        steps = setup.plan(INPUT, machine.system())

        for step in steps:
            text = str(step)
            for forbidden in ("/etc/", "sudoers", "sshd", "com.apple.access", "/Users/factory "):
                self.assertNotIn(forbidden, text)
            if isinstance(step, setup.Run):
                self.assertTrue(step.argv[0].startswith("/"), step.argv[0])
            if isinstance(step, setup.Run) and step.argv[0] == setup.DSCL:
                self.assertEqual(step.argv[3], "/Users/_factorybroker")
            if isinstance(step, setup.Run) and step.argv[0] == setup.DSEDITGROUP:
                self.assertEqual(step.argv[-1], "_factorybroker")


class RefusalTest(unittest.TestCase):
    def refuses(self, machine, data=INPUT, layout=LAYOUT, platform="darwin"):
        with self.assertRaises(setup.Refuse) as caught:
            setup.plan(data, machine.system(platform), layout)
        return str(caught.exception)

    def test_an_existing_login_account_with_the_brokers_name_is_never_edited(self):
        machine = Machine()
        machine.users["_factorybroker"] = person("_factorybroker", 502)

        self.assertIn("never edits an existing account", self.refuses(machine))

    def test_an_existing_account_with_a_shell_is_never_edited(self):
        machine = Machine()
        machine.groups["_factorybroker"] = group("_factorybroker", 480)
        machine.users["_factorybroker"] = person("_factorybroker", 480, 480, shell="/bin/sh")

        self.assertIn("never edits an existing account", self.refuses(machine))

    def test_a_protected_or_the_factory_users_name_is_refused(self):
        for name in ("admin", "com.apple.access_ssh", "factory"):
            with self.subTest(name=name):
                layout = setup.Layout(user=name, group=name)
                self.assertIn("protected name", self.refuses(Machine(), layout=layout))

    def test_a_missing_or_system_factory_user_is_refused(self):
        self.assertIn("does not exist", self.refuses(Machine(), data={**INPUT, "factory_user": "nobody-here"}))
        machine = Machine()
        machine.users["factory"] = person("factory", 70)
        self.assertIn("login account", self.refuses(machine))

    def test_a_group_with_other_members_is_refused(self):
        machine = Machine()
        machine.groups["_factorybroker"] = group("_factorybroker", 480, ["someone"])

        self.assertIn("other members", self.refuses(machine))

    def test_an_interpreter_another_user_can_change_is_refused(self):
        machine = Machine()
        machine.entries[REAL_PYTHON] = setup.Entry(501, 20, 0o755)
        self.assertIn(str(REAL_PYTHON), self.refuses(machine))

    def test_an_interpreter_under_an_admin_writable_directory_is_refused(self):
        machine = Machine()
        machine.entries[REAL_PYTHON.parent.parent / "lib"] = setup.Entry(0, 80, 0o775)

        self.assertIn(str(REAL_PYTHON.parent.parent / "lib"), self.refuses(machine))

    def test_a_base_directory_another_user_placed_is_refused(self):
        machine = Machine()
        machine.entries[LAYOUT.base] = setup.Entry(501, 20, 0o755)
        self.assertIn("another user's", self.refuses(machine))
        machine.entries[LAYOUT.base] = setup.Entry(0, 0, 0o755, link=True)
        self.assertIn("a link", self.refuses(machine))

    def test_a_writable_parent_directory_is_refused(self):
        machine = Machine()
        machine.entries[Path("/Library/Application Support")] = setup.Entry(0, 80, 0o775)

        self.assertIn("writable by a non-root user", self.refuses(machine))

    def test_only_macos_is_supported(self):
        self.assertIn("macOS", self.refuses(Machine(), platform="linux"))


class SourceTest(unittest.TestCase):
    def entries(self, owner_of_repo: int) -> dict:
        found = {}
        for path in (setup.SCRIPT, *(setup.REPO / source for source in setup.SERVICE_FILES.values())):
            for parent in [path, *path.parents]:
                inside = parent == setup.REPO or setup.REPO in parent.parents
                found[parent] = setup.Entry(owner_of_repo if inside else 0, 0, 0o755)
        return found

    def test_a_root_owned_clone_may_run_apply(self):
        setup.check_source(self.entries(0).get)

    def test_a_checkout_the_factory_user_owns_is_refused(self):
        with self.assertRaises(setup.Refuse) as caught:
            setup.check_source(self.entries(501).get)
        self.assertIn("root-owned clone", str(caught.exception))


class ConfirmTest(unittest.TestCase):
    def test_apply_installs_the_config_only_after_the_operator_types_yes(self):
        steps = setup.plan(INPUT, Machine().system())
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            setup.confirm_config(steps, LAYOUT, "factory", ask=lambda prompt: "yes")
        self.assertIn('"allowed_uids": [', shown.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(setup.Refuse):
            setup.confirm_config(steps, LAYOUT, "factory", ask=lambda prompt: "y")

    def test_a_plan_without_a_new_config_asks_nothing(self):
        setup.confirm_config([], LAYOUT, "factory", ask=lambda prompt: self.fail("asked"))


class InputTest(unittest.TestCase):
    def read(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return setup.read_input(Path(handle.name))

    def test_a_complete_input_reads_back(self):
        self.assertEqual(self.read(INPUT), INPUT)

    def test_an_incomplete_input_is_refused(self):
        for broken in (
            {**INPUT, "factory_user": None},
            {**INPUT, "repositories": {}},
            {**INPUT, "repositories": {"example-org/example-repo": "123"}},
            {**INPUT, "infisical": {"api_url": "https://example.invalid"}},
        ):
            with self.subTest(broken=broken), self.assertRaises(setup.Refuse):
                self.read(broken)


class SetSecretTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = Path(tmp.name) / ".env"

    def test_a_value_is_added_then_replaced_in_a_mode_0600_file(self):
        self.env.write_text("OTHER=kept\n")
        os.chmod(self.env, 0o600)

        setup.set_env_value(self.env, "BROKER_INFISICAL_CLIENT_ID", "first", os.getuid(), os.getgid())
        setup.set_env_value(self.env, "BROKER_INFISICAL_CLIENT_ID", "second", os.getuid(), os.getgid())

        self.assertEqual(self.env.read_text(), "OTHER=kept\nBROKER_INFISICAL_CLIENT_ID=second\n")
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)

    def test_a_bad_name_or_value_is_refused(self):
        for name, value in (("lower", "v"), ("A B", "v"), ("NAME", ""), ("NAME", "two\nlines")):
            with self.subTest(name=name), self.assertRaises(setup.Refuse):
                setup.set_env_value(self.env, name, value, os.getuid(), os.getgid())
        self.assertFalse(self.env.exists())

    def test_links_the_broker_user_plants_are_never_followed(self):
        victim = self.env.parent / "victim"
        victim.write_text("root's file\n")
        (self.env.parent / ".env.new").symlink_to(victim)

        setup.set_env_value(self.env, "NAME", "value", os.getuid(), os.getgid())

        self.assertEqual(victim.read_text(), "root's file\n")
        self.assertEqual(self.env.read_text(), "NAME=value\n")
        self.env.unlink()
        self.env.symlink_to(victim)
        with self.assertRaises(OSError):
            setup.set_env_value(self.env, "NAME", "value", os.getuid(), os.getgid())
        self.assertEqual(victim.read_text(), "root's file\n")


class ApplyTest(unittest.TestCase):
    def test_apply_runs_commands_and_writes_files_with_their_owner_and_mode(self):
        me, mine = pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name
        ran = []
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "run"
            target = directory / "broker.json"
            with contextlib.redirect_stdout(io.StringIO()):
                setup.apply([
                    setup.Run(("true",)),
                    setup.Directory(directory, me, mine, 0o750),
                    setup.File(target, b"{}\n", me, mine, 0o644),
                ], run=lambda argv, check: ran.append(argv))

            self.assertEqual(ran, [["true"]])
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o750)
            self.assertEqual(target.read_bytes(), b"{}\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertEqual(sorted(p.name for p in directory.iterdir()), ["broker.json"])


class SystemPythonTest(unittest.TestCase):
    def test_the_script_parses_as_python_3_9(self):
        ast.parse(SCRIPT.read_text(), filename=str(SCRIPT), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
