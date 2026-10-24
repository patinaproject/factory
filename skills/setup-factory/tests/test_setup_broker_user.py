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
COMMIT = "1" * 40
NEXT_COMMIT = "2" * 40
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
            meta=self.entries.get,
        )

    def converge(self, steps):
        for step in steps:
            if isinstance(step, setup.MoveKey):
                del self.entries[step.source]
                broker = self.users[step.owner]
                self.entries[step.path] = setup.Entry(broker.pw_uid, broker.pw_gid, step.mode)
            elif isinstance(step, setup.Run):
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
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/github_app.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/service.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/upload_token.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/factory_uploads/secret-names.yaml root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/linear.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/lib/broker_client.py root:wheel 0644",
            "write  /Library/Application Support/factory-broker/setup-broker-user root:wheel 0644",
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

    def test_the_config_keeps_only_each_mcp_servers_variable_name(self):
        session = {"auth_token_env": "EXAMPLE_PROXY_TOKEN", "mcp_servers": {
            "sentry": {"url": "https://mcp.example.com/mcp", "token_env": "EXAMPLE_SENTRY_TOKEN", "headers": {"x-read-only": "true"}},
        }}
        steps = setup.plan({**INPUT, "claude_session": session}, Machine().system())

        [config] = [s for s in steps if isinstance(s, setup.File) and s.path == LAYOUT.config]
        self.assertEqual(json.loads(config.content)["claude_session"], {
            "auth_token_env": "EXAMPLE_PROXY_TOKEN", "mcp_servers": {"sentry": {"token_env": "EXAMPLE_SENTRY_TOKEN"}},
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


FACTORY_KEY = Path("/Users/factory/.config/factory/github-app.pem")
GITHUB = {"app_id": 11, "installation_id": 22, "private_key_path": str(FACTORY_KEY)}
WITH_KEY = {**INPUT, "github": GITHUB}
MOVE = f"move   {FACTORY_KEY} -> /Library/Application Support/factory-broker/home/github-app.pem _factorybroker:_factorybroker 0600"


class KeyPlanTest(unittest.TestCase):
    def machine(self, key=setup.Entry(501, 20, 0o600)):
        machine = Machine()
        if key is not None:
            machine.entries[FACTORY_KEY] = key
        return machine

    def test_the_plan_moves_the_factory_users_key_into_the_brokers_home_before_the_service_starts(self):
        steps = lines(setup.plan(WITH_KEY, self.machine().system()))

        self.assertEqual(steps[-3:], [
            "write  /Library/Application Support/factory-broker/home/.env _factorybroker:_factorybroker 0600",
            MOVE,
            "run    /bin/launchctl bootstrap system /Library/LaunchDaemons/local.factory-broker.plist",
        ])

    def test_the_config_names_the_brokers_copy_and_never_the_factory_users(self):
        steps = setup.plan(WITH_KEY, self.machine().system())

        [config] = [s for s in steps if isinstance(s, setup.File) and s.path == LAYOUT.config]
        self.assertEqual(json.loads(config.content)["github"], {
            "app_id": 11, "installation_id": 22,
            "private_key_path": "/Library/Application Support/factory-broker/home/github-app.pem",
        })

    def test_a_second_run_changes_nothing(self):
        machine = self.machine()
        machine.converge(setup.plan(WITH_KEY, machine.system()))

        self.assertNotIn(FACTORY_KEY, machine.entries)
        self.assertEqual(setup.plan(WITH_KEY, machine.system()), [])

    def test_a_planner_that_cannot_see_into_the_brokers_home_after_the_move_plans_nothing(self):
        machine = self.machine()
        machine.converge(setup.plan(WITH_KEY, machine.system()))
        machine.entries[LAYOUT.github_key] = setup.UNREADABLE_WITHOUT_ROOT

        self.assertEqual(setup.plan(WITH_KEY, machine.system()), [])

    def test_a_key_that_is_at_neither_path_is_refused(self):
        with self.assertRaises(setup.Refuse) as caught:
            setup.plan(WITH_KEY, self.machine(key=None).system())

        self.assertEqual(
            str(caught.exception),
            f"the GitHub App key is at neither {FACTORY_KEY} nor {LAYOUT.github_key}",
        )

    def test_a_key_that_is_a_link_another_users_or_open_to_others_is_refused(self):
        for key in (
            setup.Entry(501, 20, 0o600, link=True), setup.Entry(0, 0, 0o600),
            setup.Entry(501, 20, 0o640), setup.UNREADABLE_WITHOUT_ROOT,
        ):
            with self.subTest(key=key), self.assertRaises(setup.Refuse):
                setup.plan(WITH_KEY, self.machine(key=key).system())

    def test_a_key_path_inside_the_brokers_directory_is_refused(self):
        github = {**GITHUB, "private_key_path": str(LAYOUT.github_key)}

        with self.assertRaises(setup.Refuse):
            setup.plan({**INPUT, "github": github}, self.machine().system())

    def test_the_confirmation_gives_the_rollback_and_never_the_key(self):
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), self.assertRaises(setup.Refuse):
            setup.install(WITH_KEY, self.machine().system(), LAYOUT, COMMIT, ask=lambda prompt: "no")

        self.assertIn(
            "To put the key back: /usr/bin/sudo /usr/bin/install -o factory -m 0600 "
            f"'{LAYOUT.github_key}' {FACTORY_KEY}",
            shown.getvalue(),
        )


class KeyInputTest(unittest.TestCase):
    def test_a_github_object_needs_numeric_ids_and_an_absolute_key_path(self):
        for github in (
            {**GITHUB, "app_id": "x11"}, {**GITHUB, "installation_id": True},
            {**GITHUB, "private_key_path": "github-app.pem"}, {"app_id": 11}, "github",
        ):
            with self.subTest(github=github), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "input.json"
                path.write_text(json.dumps({**INPUT, "github": github}))
                with self.assertRaises(setup.Refuse):
                    setup.read_input(path)


PEM = b"-----BEGIN RSA PRIVATE KEY-----\nFAKEKEYBODY\n-----END RSA PRIVATE KEY-----\n"


class MoveKeyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.factory = Path(tmp.name) / "factory"
        self.broker = Path(tmp.name) / "broker"
        self.factory.mkdir()
        self.broker.mkdir(mode=0o700)
        self.source = self.factory / "github-app.pem"
        self.target = self.broker / "github-app.pem"
        self.write(self.source, PEM)
        self.step = setup.MoveKey(self.source, self.target, "broker", "broker", os.getuid())

    @staticmethod
    def write(path, content, mode=0o600):
        path.write_bytes(content)
        os.chmod(path, mode)

    def move(self):
        setup.move_key(self.step, os.getuid(), os.getgid())

    def test_the_key_lands_in_the_brokers_home_with_mode_0600_and_leaves_the_factory_users(self):
        self.move()

        self.assertEqual(self.target.read_bytes(), PEM)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertFalse(self.source.exists())

    def test_the_same_key_already_in_place_only_removes_the_factory_users_copy(self):
        self.write(self.target, PEM)

        self.move()

        self.assertEqual(self.target.read_bytes(), PEM)
        self.assertFalse(self.source.exists())

    def test_a_different_key_in_place_keeps_both(self):
        other = PEM.replace(b"FAKEKEYBODY", b"OTHERKEY")
        self.write(self.target, other)

        with self.assertRaises(setup.Refuse):
            self.move()

        self.assertEqual((self.source.read_bytes(), self.target.read_bytes()), (PEM, other))

    def test_a_source_that_is_not_a_private_key_is_kept(self):
        self.write(self.source, b"not a key\n")

        with self.assertRaises(setup.Refuse):
            self.move()

        self.assertTrue(self.source.exists())
        self.assertFalse(self.target.exists())

    def test_a_linked_or_open_source_is_refused_and_kept(self):
        elsewhere = self.factory / "elsewhere.pem"
        self.source.rename(elsewhere)
        self.source.symlink_to(elsewhere)
        with self.assertRaises(setup.Refuse):
            self.move()
        self.source.unlink()

        os.link(elsewhere, self.source)
        with self.assertRaises(setup.Refuse):
            self.move()
        elsewhere.unlink()

        os.chmod(self.source, 0o640)
        with self.assertRaises(setup.Refuse):
            self.move()

        self.assertEqual(self.source.read_bytes(), PEM)
        self.assertFalse(self.target.exists())

    def test_apply_moves_the_key_and_prints_no_key_material(self):
        me, mine = pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name
        step = setup.MoveKey(self.source, self.target, me, mine, os.getuid())

        with contextlib.redirect_stdout(io.StringIO()) as shown:
            setup.apply([step])

        self.assertEqual(self.target.read_bytes(), PEM)
        self.assertFalse(self.source.exists())
        self.assertNotIn("FAKEKEYBODY", shown.getvalue())


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
        for path in setup.source_paths():
            for parent in [path, *path.parents]:
                inside = parent == setup.REPO or setup.REPO in parent.parents
                found[parent] = setup.Entry(owner_of_repo if inside else 0, 0, 0o755)
        return found

    def test_a_root_owned_clone_may_run_apply(self):
        setup.check_source(self.entries(0).get)

    def test_the_commit_is_read_from_a_detached_checkout_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            head = Path(tmp) / ".git" / "HEAD"
            head.parent.mkdir()
            head.write_text(f"{COMMIT}\n")
            self.assertEqual(setup.source_commit(Path(tmp)), COMMIT)
            head.write_text("ref: refs/heads/main\n")
            with self.assertRaises(setup.Refuse):
                setup.source_commit(Path(tmp))

    def test_a_checkout_the_factory_user_owns_is_refused(self):
        with self.assertRaises(setup.Refuse) as caught:
            setup.check_source(self.entries(501).get)
        self.assertIn("root-owned clone", str(caught.exception))


class InstallTest(unittest.TestCase):
    def installed(self):
        machine = Machine()
        machine.converge(setup.plan(INPUT, machine.system(), commit=COMMIT))
        return machine

    def install(self, machine, answer, commit=NEXT_COMMIT, data=INPUT):
        asked, written = [], []

        def ask(prompt):
            asked.append(prompt)
            return answer

        refusal = None
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            try:
                setup.install(data, machine.system(), LAYOUT, commit, ask=ask, write=written.extend)
            except setup.Refuse as caught:
                refusal = str(caught)
        return asked, lines(written), shown.getvalue(), refusal

    def test_the_first_install_records_its_commit(self):
        machine = Machine()
        _, written, _, refusal = self.install(machine, "yes", commit=COMMIT)

        self.assertIsNone(refusal)
        self.assertIn(f"write  {LAYOUT.commit_file} root:wheel 0644", written)
        machine.converge(setup.plan(INPUT, machine.system(), commit=COMMIT))
        self.assertEqual(machine.entries[LAYOUT.commit_file].content, f"{COMMIT}\n".encode())

    def test_a_code_only_change_shows_its_diff_and_installs_nothing_without_yes(self):
        machine = self.installed()
        target = LAYOUT.lib / "factory_uploads" / "broker.py"
        machine.entries[target] = setup.Entry(0, 0, 0o644, b"older code\n")

        asked, written, shown, refusal = self.install(machine, "y")

        self.assertEqual(len(asked), 1)
        self.assertEqual(written, [])
        self.assertIn("did not confirm", refusal)
        self.assertIn(f"--- {target} (installed)", shown)
        self.assertIn("-older code", shown)
        self.assertIn(
            f"git diff {COMMIT}..{NEXT_COMMIT} -- plugins/factory-uploads plugins/factory-secret-names/plugin.yaml plugins/linear-agent-session/linear.py "
            "plugins/linear-agent-session/broker_client.py skills/setup-factory/scripts/setup-broker-user",
            shown,
        )

    def test_a_confirmed_code_change_installs_it_restarts_the_service_and_records_the_commit(self):
        machine = self.installed()
        target = LAYOUT.lib / "factory_uploads" / "broker.py"
        machine.entries[target] = setup.Entry(0, 0, 0o644, b"older code\n")

        _, written, _, refusal = self.install(machine, "yes")

        self.assertIsNone(refusal)
        self.assertEqual(written, [
            f"write  {target} root:wheel 0644",
            f"write  {LAYOUT.commit_file} root:wheel 0644",
            "run    /bin/launchctl kickstart -k system/local.factory-broker",
        ])

    def test_a_changed_setup_script_needs_yes(self):
        machine = self.installed()
        machine.entries[LAYOUT.script_copy] = setup.Entry(0, 0, 0o644, b"older script\n")

        asked, written, shown, refusal = self.install(machine, "no")

        self.assertEqual((len(asked), written), (1, []))
        self.assertIn("-older script", shown)
        self.assertIsNotNone(refusal)

    def test_no_change_asks_nothing_and_writes_nothing(self):
        asked, written, _, refusal = self.install(self.installed(), "yes")

        self.assertEqual((asked, written, refusal), ([], [], None))

    def test_the_confirmation_names_the_factory_user_and_the_infisical_host(self):
        data = {**INPUT, "infisical": {**INFISICAL, "api_url": "https://secrets.example.com", "self_hosted_host": "secrets.example.com"}}

        _, _, shown, _ = self.install(Machine(), "no", data=data)

        self.assertIn("factory joins _factorybroker", shown)
        self.assertIn("self-hosted Infisical at https://secrets.example.com", shown)

    def test_the_brokers_env_file_is_never_shown(self):
        machine = self.installed()
        machine.entries.pop(LAYOUT.home / ".env")
        machine.entries[LAYOUT.lib / "linear.py"] = setup.Entry(0, 0, 0o644, b"older\n")

        _, _, shown, _ = self.install(machine, "no")

        self.assertNotIn(f"--- {LAYOUT.home / '.env'}", shown)

    def test_control_characters_in_a_diff_are_shown_escaped(self):
        machine = self.installed()
        target = LAYOUT.lib / "factory_uploads" / "broker.py"
        machine.entries[target] = setup.Entry(0, 0, 0o644, b"keep\tthis\n\x1b[2A\x1b[2Kold\r\x1bc\x7f\xc2\x85\xff\n")

        _, _, shown, _ = self.install(machine, "no")

        self.assertEqual([c for c in shown if (c < " " and c not in "\n\t") or "\x7f" <= c <= "\x9f"], [])
        self.assertIn("-keep\tthis\n", shown)
        for escaped in ("\\x1b[2A", "\\x1b[2Kold", "\\x0d", "\\x1bc", "\\x7f", "\\x85", "\\xff"):
            self.assertIn(escaped, shown)

    def test_control_characters_in_the_factory_user_are_shown_escaped(self):
        name = "fac\x1b[2Jtory"
        machine = Machine()
        machine.users = {name: person(name, 501)}

        _, _, shown, _ = self.install(machine, "no", data={**INPUT, "factory_user": name})

        self.assertNotIn("\x1b", shown)
        self.assertIn("fac\\x1b[2Jtory joins _factorybroker", shown)
        self.assertIn("-a 'fac\\x1b[2Jtory'", shown)

    def test_a_refusal_shows_control_characters_escaped(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = setup.main(["plan", "--input", "/missing\x1b[2J.json"])

        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", stderr.getvalue())
        self.assertIn("/missing\\x1b[2J.json", stderr.getvalue())


INFISICAL = {"api_url": "https://app.infisical.com", "project_id": "p", "environment": "e", "paths": ["/f"]}


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
            {**INPUT, "claude_session": {"mcp_servers": {"sentry": "EXAMPLE_SENTRY_TOKEN"}}},
            {**INPUT, "claude_session": {"auth_token_env": 7}},
            {**INPUT, "infisical": {"api_url": "https://example.invalid"}},
        ):
            with self.subTest(broken=broken), self.assertRaises(setup.Refuse):
                self.read(broken)

    def test_infisical_cloud_hosts_are_accepted(self):
        for url in ("https://app.infisical.com", "https://eu.infisical.com/"):
            with self.subTest(url=url):
                data = {**INPUT, "infisical": {**INFISICAL, "api_url": url}}
                self.assertEqual(self.read(data), data)

    def test_an_infisical_url_that_is_not_https_to_a_known_host_is_refused(self):
        for url in (
            "http://app.infisical.com",
            "https://attacker.example",
            "https://app.infisical.com.attacker.example",
            "https://user:pass@app.infisical.com",
            "https://app.infisical.com/elsewhere",
            "https://app.infisical.com?x=1",
        ):
            with self.subTest(url=url), self.assertRaises(setup.Refuse):
                self.read({**INPUT, "infisical": {**INFISICAL, "api_url": url}})

    def test_a_self_hosted_infisical_host_needs_its_name_confirmed(self):
        url = "https://secrets.example.com"
        with self.assertRaises(setup.Refuse) as caught:
            self.read({**INPUT, "infisical": {**INFISICAL, "api_url": url}})
        self.assertIn("self_hosted_host", str(caught.exception))
        data = {**INPUT, "infisical": {**INFISICAL, "api_url": url, "self_hosted_host": "secrets.example.com"}}
        self.assertEqual(self.read(data), data)
        with self.assertRaises(setup.Refuse):
            self.read({**INPUT, "infisical": {**INFISICAL, "api_url": "http://secrets.example.com", "self_hosted_host": "secrets.example.com"}})

    def test_an_infisical_url_with_a_control_character_is_refused_even_for_a_confirmed_host(self):
        for url in ("https://app.infisical.com\x1bc", "https://app.infisical.com\n", "https://app.infisical.com:\x1bc"):
            for host in (None, url.split("//")[1].split(":")[0]):
                with self.subTest(url=url, host=host), self.assertRaises(setup.Refuse):
                    self.read({**INPUT, "infisical": {**INFISICAL, "api_url": url, "self_hosted_host": host}})

    def test_an_infisical_url_must_use_the_https_port(self):
        data = {**INPUT, "infisical": {**INFISICAL, "api_url": "https://app.infisical.com:443"}}
        self.assertEqual(self.read(data), data)
        for url in ("https://app.infisical.com:8443", "https://app.infisical.com:80", "https://app.infisical.com:x"):
            with self.subTest(url=url), self.assertRaises(setup.Refuse):
                self.read({**INPUT, "infisical": {**INFISICAL, "api_url": url}})

    def test_a_malformed_infisical_url_is_refused_cleanly(self):
        for url in ("https://[app.infisical.com", "https://[::1", "https://app.infisical.com]"):
            with self.subTest(url=url), self.assertRaises(setup.Refuse):
                self.read({**INPUT, "infisical": {**INFISICAL, "api_url": url}})


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

    def test_a_hard_link_the_broker_user_plants_is_refused(self):
        victim = self.env.parent / "victim"
        victim.write_text("ROOT_ONLY=secret\n")
        os.link(victim, self.env)

        with self.assertRaises(setup.Refuse):
            setup.set_env_value(self.env, "NAME", "value", os.getuid(), os.getgid())
        self.assertEqual(victim.read_text(), "ROOT_ONLY=secret\n")
        self.assertEqual(self.env.read_text(), "ROOT_ONLY=secret\n")

    def test_an_env_file_another_user_owns_is_refused(self):
        self.env.write_text("OTHER=kept\n")

        with self.assertRaises(setup.Refuse):
            setup.set_env_value(self.env, "NAME", "value", os.getuid() + 1, os.getgid())
        self.assertEqual(self.env.read_text(), "OTHER=kept\n")


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
