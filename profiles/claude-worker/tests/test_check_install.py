from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import SCRIPTS, git

ROUTES = {"linear": {"events": ["AgentSessionEvent"], "secret": "${LINEAR_WEBHOOK_SECRET}", "prompt": "triage"}}

RENDER = f"""import json
print(json.dumps({ROUTES!r}))
"""

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
key = sys.argv[3]
home = os.environ["HERMES_HOME"]
if key == "dashboard":
    path = os.path.join(home, "installed-dashboard.json")
elif key.startswith("platforms.webhook.extra.routes."):
    path = os.path.join(home, "installed-routes", key.rsplit(".", 1)[1] + ".json")
else:
    config = json.load(open(os.path.join(home, "installed-config.json")))
    if key not in config:
        sys.exit("Config key not set: " + key)
    print(json.dumps(config[key]))
    sys.exit()
if not os.path.exists(path):
    sys.exit("Config key not set: " + key)
print(open(path).read())
"""

FAKE_LAUNCHCTL = """#!/usr/bin/env python3
import json, os, sys
states = json.load(open(os.environ["FAKE_LAUNCHD_STATES"]))
target = sys.argv[2]
if target not in states:
    sys.exit(113)
print("ai.example = {\\n\\tactive count = 1\\n\\tstate = " + states[target] + "\\n}")
"""

SETTINGS = {"app_user_id": "u1", "repositories": []}
ROOT_CONFIG = {
    "model.provider": "claude-subscription-directsdk-experimental",
    "model.default": "opus",
    "platforms.webhook.enabled": True,
    "platforms.webhook.extra.host": "127.0.0.1",
    "platforms.webhook.extra.port": 8644,
    "plugins.entries.linear-agent-session.settings": SETTINGS,
    "kanban.max_in_progress": 4,
    "kanban.review_dispatch": False,
    "kanban.auto_decompose": False,
    "streaming.enabled": True,
    "platforms.slack.extra.native_task_cards": True,
}
WORKER_CONFIG = {key: ROOT_CONFIG[key] for key in (
    "model.provider", "model.default", "plugins.entries.linear-agent-session.settings",
)}


def cron_job(name: str, minutes: int, deliver: str = "local", **fields) -> dict:
    return {
        "id": name, "name": name, "script": name, "no_agent": True, "enabled": True, "deliver": deliver,
        "schedule": {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"},
        "schedule_display": f"every {minutes}m", "prompt": "", **fields,
    }


WORKER_JOBS = [
    cron_job("refresh-gh-app-login", 30),
    cron_job("refresh-checkouts", 60),
    cron_job("check-install", 60, "slack"),
]

SCRIPT = SCRIPTS / "check-install"
loader = importlib.machinery.SourceFileLoader("check_install", str(SCRIPT))
check_install = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(check_install)

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

        self.issuer = self.tmp / "issuer"
        self.discovery(["authorization_code_with_pkce", "refresh_tokens"])
        self.dashboard(scopes="openid profile email offline_access")

        self.hermes = self.tmp / "bin" / "fake-hermes"
        self.install(self.tmp, "bin/fake-hermes", FAKE_HERMES)
        os.chmod(self.hermes, 0o755)

        self.config(self.root, ROOT_CONFIG)
        self.config(self.worker, WORKER_CONFIG)
        self.cron(self.worker, WORKER_JOBS)
        self.launchd = Launchd(self.tmp)

    def install(self, home: Path, path: str, text: str) -> None:
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)

    def config(self, home: Path, config: dict) -> None:
        self.install(home, "installed-config.json", json.dumps(config))

    def cron(self, home: Path, jobs: list) -> None:
        self.install(home, "cron/jobs.json", json.dumps({"jobs": jobs, "updated_at": "2026-10-12T00:00:00"}))

    def discovery(self, grants: list) -> None:
        self.install(
            self.issuer, ".well-known/openid-configuration", json.dumps({"grant_types_supported": grants})
        )

    def dashboard(self, *, public_url: str = "https://factory.example", issuer: str | None = None, **self_hosted) -> None:
        self_hosted = {"issuer": self.issuer.as_uri() if issuer is None else issuer, "client_id": "cid", **self_hosted}
        config = {"public_url": public_url, "oauth": {"client_id": "", "self_hosted": self_hosted}}
        self.install(self.root, "installed-dashboard.json", json.dumps(config))

    def check(self, *args: str) -> tuple:
        env = {**os.environ, **self.launchd.env, "HERMES_HOME": str(self.worker), "HERMES_BIN": str(self.hermes)}
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

    def test_dashboard_without_offline_access_cannot_get_a_refresh_token(self) -> None:
        self.dashboard()

        self.assertEqual(
            self.check(),
            (1, f"dashboard sign-in {self.root}: dashboard.oauth.self_hosted.scopes does not request offline_access, "
                "so the issuer returns no refresh token\n"),
        )

    def test_dashboard_issuer_without_refresh_grant(self) -> None:
        self.discovery(["authorization_code_with_pkce"])

        self.assertEqual(
            self.check(),
            (1, f"dashboard sign-in {self.root}: the issuer does not offer the refresh_tokens grant\n"),
        )

    def test_dashboard_without_public_url(self) -> None:
        self.dashboard(public_url="", scopes="openid profile email offline_access")

        self.assertEqual(
            self.check(), (1, f"dashboard sign-in {self.root}: dashboard.public_url is not set\n")
        )

    def test_dashboard_without_oidc_issuer(self) -> None:
        self.dashboard(issuer="", scopes="openid profile email offline_access")

        self.assertEqual(
            self.check(), (1, f"dashboard sign-in {self.root}: dashboard.oauth.self_hosted.issuer is not set\n")
        )

    def test_dashboard_without_client_id(self) -> None:
        self.dashboard(client_id="", scopes="openid profile email offline_access")

        self.assertEqual(
            self.check(), (1, f"dashboard sign-in {self.root}: dashboard.oauth.self_hosted.client_id is not set\n")
        )

    def test_dashboard_config_that_is_not_json_does_not_hide_other_drift(self) -> None:
        self.install(self.root, "installed-dashboard.json", "not json")
        self.install(self.worker, "SOUL.md", "old soul\n")

        self.assertEqual(
            self.check(),
            (1, f"profile claude-worker: SOUL.md differs from main at {self.sha}\n"
                f"dashboard sign-in {self.root}: hermes config get dashboard did not return JSON\n"),
        )

    def test_unreadable_issuer_discovery(self) -> None:
        (self.issuer / ".well-known" / "openid-configuration").unlink()

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertTrue(out.startswith(f"dashboard sign-in {self.root}: cannot read the issuer's discovery document"))

    def test_config_key_that_differs_from_the_runbook(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "kanban.review_dispatch": True})

        self.assertEqual(self.check(), (1, f"config kanban.review_dispatch in {self.root}: is not false (part 10)\n"))

    def test_proxy_provider_from_part_3_is_accepted(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "model.provider": "custom:proxy"})
        self.config(self.worker, {**WORKER_CONFIG, "model.provider": "custom:proxy"})

        self.assertEqual(self.check(), (0, ""))

    def test_provider_the_runbook_does_not_document(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "model.provider": "openrouter"})
        self.config(self.worker, {**WORKER_CONFIG, "model.provider": "openrouter"})

        self.assertEqual(
            self.check(),
            (1, f"config model.provider in {self.root}: "
                'is not "claude-subscription-directsdk-experimental" or custom:<proxy provider> (part 4)\n'
                f"config model.provider in {self.worker}: "
                'is not "claude-subscription-directsdk-experimental" or custom:<proxy provider> (part 4)\n'),
        )

    def test_provider_that_differs_between_homes(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, "model.provider": "custom:proxy"})

        self.assertEqual(
            self.check(),
            (1, f"config model.provider: differs between {self.root} and {self.worker} (part 4)\n"),
        )

    def test_config_drift_never_prints_the_live_value(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "platforms.webhook.extra.host": "s3cr3t-value"})

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertNotIn("s3cr3t-value", out)

    def test_declared_input_that_is_not_set(self) -> None:
        config = dict(ROOT_CONFIG)
        del config["model.default"]
        self.config(self.root, {**config, "kanban.max_in_progress": ""})

        self.assertEqual(
            self.check(),
            (1, f"config model.default in {self.root}: is not set to the operator's model alias (part 4)\n"
                f"config kanban.max_in_progress in {self.root}: is not set to the operator's card limit (part 10)\n"),
        )

    def test_factory_settings_that_differ_between_homes(self) -> None:
        settings = {**SETTINGS, "app_user_id": "u2"}
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})

        self.assertEqual(
            self.check(),
            (1, f"config plugins.entries.linear-agent-session.settings: differs between {self.root} and {self.worker} "
                "(part 10)\n"),
        )

    def test_missing_and_duplicated_cron_jobs(self) -> None:
        self.cron(self.worker, [WORKER_JOBS[0], WORKER_JOBS[2], WORKER_JOBS[2]])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-checkouts in {self.worker}: is missing (part 12)\n"
                f"cron job check-install in {self.worker}: exists 2 times (part 12)\n"),
        )

    def test_cron_job_changed_by_hand(self) -> None:
        self.cron(self.worker, [
            cron_job("refresh-gh-app-login", 30, enabled=False),
            cron_job("refresh-checkouts", 15, no_agent=False),
            cron_job("check-install", 60, "local"),
        ])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-gh-app-login in {self.worker}: is disabled (part 10)\n"
                f"cron job refresh-checkouts in {self.worker}: runs every 15m, not every 60m (part 12)\n"
                f"cron job refresh-checkouts in {self.worker}: is not a script-only job that runs refresh-checkouts "
                "(part 12)\n"
                f'cron job check-install in {self.worker}: delivers to "local", not the operator\'s chat platform '
                "(part 12)\n"),
        )

    def test_cron_jobs_the_runbook_does_not_create(self) -> None:
        self.cron(self.worker, [*WORKER_JOBS, cron_job("nightly-cleanup", 1440)])
        self.cron(self.root, [cron_job("refresh-checkouts", 60)])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-checkouts in {self.root}: belongs to the worker profile (part 12)\n"
                f"cron job nightly-cleanup in {self.worker}: is not a job the setup runbook creates\n"),
        )

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
        self.launchd.plist(f"{str(home).lstrip('/')}/Library/LaunchAgents/ai.hermes.gateway.plist", "ai.hermes.gateway")
        env = {"PATH": "/usr/bin:/bin", **self.launchd.env, "HOME": str(home), "HERMES_HOME": str(self.worker)}
        proc = subprocess.run(
            ["/usr/bin/env", "python3", str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True
        )

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_worker_home_outside_profiles_is_refused(self) -> None:
        env = {**os.environ, "HERMES_HOME": str(self.root), "HERMES_BIN": str(self.hermes)}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True)

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))


class Launchd:
    """A file system root with the runbook's two plists and a fake launchctl that reports their state."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "launchd"
        self.states_file = tmp / "launchd-states.json"
        self.launchctl = tmp / "bin" / "fake-launchctl"
        self.launchctl.parent.mkdir(parents=True, exist_ok=True)
        self.launchctl.write_text(FAKE_LAUNCHCTL)
        os.chmod(self.launchctl, 0o755)
        self.home = tmp / "launchd-home"
        self.daemon = self.plist("Library/LaunchDaemons/com.example.cloudflared.plist", "com.example.cloudflared")
        self.agent = self.plist(
            f"{str(self.home).lstrip('/')}/Library/LaunchAgents/ai.hermes.gateway.plist", "ai.hermes.gateway"
        )
        uid = os.getuid()
        self.states({"system/com.example.cloudflared": "running", f"user/{uid}/ai.hermes.gateway": "running"})
        self.env = {
            "LAUNCHD_ROOT": str(self.root), "LAUNCHCTL_BIN": str(self.launchctl),
            "FAKE_LAUNCHD_STATES": str(self.states_file), "HOME": str(self.home),
        }

    def plist(self, path: str, label: str, **fields) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps({
            "Label": label, "RunAtLoad": True, "ProgramArguments": ["/bin/tool", "--token", "s3cr3t"], **fields,
        }))
        return target

    def states(self, states: dict) -> None:
        self.states_file.write_text(json.dumps(states))


class CheckServicesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        self.launchd = Launchd(tmp)
        env = mock.patch.dict(os.environ, self.launchd.env)
        env.start()
        self.addCleanup(env.stop)

    def check(self) -> list:
        return [str(d) for d in check_install.check_services(self.launchd.root, str(self.launchd.launchctl))]

    def test_installed_running_services_are_silent(self) -> None:
        self.assertEqual(self.check(), [])

    def test_agent_loaded_from_a_login_session(self) -> None:
        uid = os.getuid()
        self.launchd.states({"system/com.example.cloudflared": "running", f"gui/{uid}/ai.hermes.gateway": "running"})

        self.assertEqual(self.check(), [])

    def test_missing_and_ambiguous_plists(self) -> None:
        self.launchd.agent.unlink()
        self.launchd.plist("Library/LaunchDaemons/com.other.cloudflared.plist", "com.other.cloudflared")

        self.assertEqual(self.check(), [
            "service cloudflared tunnel: 2 plists match /Library/LaunchDaemons/*cloudflared*.plist (part 7)",
            "service Hermes gateway: no plist matches ~/Library/LaunchAgents/ai.hermes.gateway.plist (part 15)",
        ])

    def test_service_that_does_not_start_at_load_or_is_not_running(self) -> None:
        self.launchd.plist("Library/LaunchDaemons/com.example.cloudflared.plist", "com.example.cloudflared",
                           RunAtLoad=False)
        self.launchd.states({"system/com.example.cloudflared": "waiting"})

        self.assertEqual(self.check(), [
            "service cloudflared tunnel: does not run at load (part 7)",
            "service cloudflared tunnel: is waiting, not running (part 7)",
            "service Hermes gateway: is not loaded in launchd (part 15)",
        ])

    def test_program_arguments_are_never_printed(self) -> None:
        self.launchd.states({})

        self.assertNotIn("s3cr3t", "\n".join(self.check()))


if __name__ == "__main__":
    unittest.main()
