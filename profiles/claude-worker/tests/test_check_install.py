from __future__ import annotations

import importlib.machinery
import importlib.util
import atexit
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.support import REPO, SCRIPTS, git, with_interpreter

ROUTE_KEY = "platforms.webhook.extra.routes.linear"
RATE_LIMIT_KEY = "platforms.webhook.extra.rate_limit"
LINEAR_ID = "00000000-0000-4000-8000-0000000000c1"
ROUTE = {
    "events": ["AgentSessionEvent"],
    "secret": "${LINEAR_WEBHOOK_SECRET}",
    "prompt": "triage",
    "filters": [{"field": "data.id", "in_file": "owned-refs.json"}],
}

RENDER = f"""import json
print(json.dumps({{{RATE_LIMIT_KEY!r}: 600, {ROUTE_KEY!r}: {ROUTE!r}}}))
"""

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:4] == ["kanban", "boards", "list"]:
    boards = os.path.join(os.environ["HERMES_HOME"], "installed-boards.json")
    print(open(boards).read() if os.path.exists(boards) else "[]")
    sys.exit()
key = sys.argv[3]
config = os.path.join(os.environ["HERMES_HOME"], "installed-config.json")
if os.path.exists(config) and key in json.load(open(config)):
    print(json.dumps(json.load(open(config))[key]))
    sys.exit()
if key == "dashboard":
    path = os.path.join(os.environ["HERMES_HOME"], "installed-dashboard.json")
else:
    path = os.path.join(os.environ["HERMES_HOME"], "installed-config", key + ".json")
if not os.path.exists(path):
    sys.exit("Config key not set: " + sys.argv[3])
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

TEAM_SKILLS = Path(tempfile.mkdtemp()).resolve()
atexit.register(shutil.rmtree, TEAM_SKILLS, ignore_errors=True)
(TEAM_SKILLS / "example-skill").mkdir()
(TEAM_SKILLS / "example-skill" / "SKILL.md").write_text("---\nname: example-skill\n---\n")
TEAM_KEY = "plugins.entries.team.settings.skills_dir"
TEAM_ALLOW_KEY = "plugins.entries.team.settings.allow"

SETTINGS = {"app_user_id": "u1", "repositories": [], "triage": {"max_running": 2}}
MAX_RUNNING_KEY = "plugins.entries.linear-agent-session.settings.triage.max_running"
BOARDS = [{"slug": "default", "default_workdir": None}, {"slug": "triage", "default_workdir": None}]
ROOT_CONFIG = {
    "model.provider": "claude-subscription-directsdk-experimental",
    "model.default": "opus",
    "platforms.webhook.enabled": True,
    "platforms.webhook.extra.host": "127.0.0.1",
    "platforms.webhook.extra.port": 8644,
    "plugins.entries.linear-agent-session.settings": SETTINGS,
    MAX_RUNNING_KEY: 2,
    "kanban.max_in_progress": 4,
    "kanban.review_dispatch": False,
    "kanban.auto_decompose": False,
    "streaming.enabled": True,
    "platforms.slack.extra.native_task_cards": True,
    "platforms.factory_uploads.enabled": True,
    "platforms.factory_uploads.extra.repositories": ["example-org/example-repo"],
    TEAM_KEY: str(TEAM_SKILLS),
}
WORKER_CONFIG = {
    **{key: ROOT_CONFIG[key] for key in (
        "model.provider", "model.default", "plugins.entries.linear-agent-session.settings", MAX_RUNNING_KEY,
    )},
    "terminal.timeout": 400,
}


def cron_job(name: str, minutes: int, deliver: str = "local", **fields) -> dict:
    return {
        "id": name, "name": name, "script": name, "no_agent": True, "enabled": True, "deliver": deliver,
        "schedule": {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"},
        "schedule_display": f"every {minutes}m", "prompt": "", **fields,
    }


WORKER_JOBS = [
    cron_job("refresh-gh-app-login", 30),
    cron_job("refresh-checkouts", 60),
    cron_job("refresh-owned-refs", 1),
    cron_job("reopen-conflicts", 5),
    cron_job("dispatch-triage", 1),
]
ALERT_SCRIPT = "check-install-alert"
RETRY_SCRIPT = "retry-failed-webhooks"
ROOT_JOBS = [{**cron_job("check-install", 60, "slack"), "script": ALERT_SCRIPT}, cron_job(RETRY_SCRIPT, 10, "slack")]
ALERT_SOURCE = f"skills/setup-factory/scripts/{ALERT_SCRIPT}"
RETRY_SOURCE = f"skills/setup-factory/scripts/{RETRY_SCRIPT}"

SCRIPT = SCRIPTS / "check-install"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
loader = importlib.machinery.SourceFileLoader("check_install", str(SCRIPT))
check_install = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(check_install)

SECRET_NAMES = "plugin.yaml"
SECRET_NAMES_MANIFEST = "kind: platform\noptional_env:\n  - name: LINEAR_CLIENT_SECRET\n  - name: CLAUDE_PROXY_TOKEN\n  - name: SENTRY_ACCESS_TOKEN\n"
TRIAGE_PLAYBOOK = ".agents/skills/patina-mode/playbooks/triage.md"
REPO_FILES = {
    f"plugins/factory-secret-names/{SECRET_NAMES}": SECRET_NAMES_MANIFEST,
    TRIAGE_PLAYBOOK: "playbook\n",
    "profiles/claude-worker/SOUL.md": "soul\n",
    "profiles/claude-worker/config.yaml": "model: repo\n",
    "profiles/claude-worker/scripts/x": "#!/bin/sh\n",
    "plugins/p1/__init__.py": "plugin\n",
    "plugins/p1/tests/test_a.py": "test\n",
    "plugins/factory-uploads/__init__.py": "uploads\n",
    "plugins/team/__init__.py": "team\n",
    "plugins/webhook-retry/__init__.py": "retry\n",
    "templates/render.py": RENDER,
    ALERT_SOURCE: "#!/usr/bin/env python3\nalert\n",
    RETRY_SOURCE: "#!/usr/bin/env python3\nretry\n",
}


class FakeBroker:
    """A Unix socket that answers each request with `reply` and records what it was sent."""

    def __init__(self, path: Path, reply: bytes):
        self.requests: list = []
        self.reply = reply
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                request = b""
                while not request.endswith(b"\n"):
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                self.requests.append(request)
                client.sendall(self.reply)

    def close(self) -> None:
        self.server.close()


STATUS = {
    "ok": True, "token_present": True, "linear_ready": True, "proxy_token_present": True, "github_app_ready": True,
    "last_state": None,
}
OK_STATUS = json.dumps(STATUS).encode() + b"\n"


class CheckInstallTest(unittest.TestCase):
    def broker(self, reply: bytes = OK_STATUS) -> FakeBroker:
        broker = FakeBroker(self.root / "factory-uploads.sock", reply)
        self.addCleanup(broker.close)
        return broker

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
            self.install(home, f"plugins/factory-secret-names/{SECRET_NAMES}", SECRET_NAMES_MANIFEST)
        self.install(self.root, "plugins/factory-uploads/__init__.py", "uploads\n")
        self.install(self.root, "plugins/team/__init__.py", "team\n")
        self.install(self.root, "plugins/webhook-retry/__init__.py", "retry\n")
        self.install(self.worker, "SOUL.md", "soul\n")
        self.install(self.worker, "scripts/x", "#!/bin/sh\n")
        self.install(self.worker, "config.yaml", "model: local\n")
        self.install(
            self.worker, "distribution.yaml", f"name: claude-worker\nsource: {self.repo}/profiles/claude-worker\n"
        )
        self.install(self.root, f"installed-config/{ROUTE_KEY}.json", json.dumps({**ROUTE, "secret": "abc...1234"}))
        self.install(self.root, f"installed-config/{RATE_LIMIT_KEY}.json", "600")
        self.install(self.root, "logs/agent.log", "")
        self.install(self.root, f"scripts/{ALERT_SCRIPT}", REPO_FILES[ALERT_SOURCE])
        self.install(self.root, f"scripts/{RETRY_SCRIPT}", REPO_FILES[RETRY_SOURCE])
        self.install(self.root, ".env", "SLACK_HOME_CHANNEL=C0123\n")
        self.owned_refs = self.root / "owned-refs.json"
        self.install(self.root, "owned-refs.json", f'["goodyear/abc-1-fix", "{LINEAR_ID}"]\n')
        self.install(self.root, "triage-refs.json", "[]\n")
        self.watch_state({"checked_at": time.time(), "state": "healthy", "restarts": []})
        self.install(self.root, "gateway-heartbeat-url", "https://monitor.example/ping/id\n")
        os.chmod(self.root / "gateway-heartbeat-url", 0o600)
        self.boards(BOARDS)

        self.issuer = self.tmp / "issuer"
        self.discovery(["authorization_code_with_pkce", "refresh_tokens"])
        self.dashboard(scopes="openid profile email offline_access")

        self.hermes = self.tmp / "bin" / "fake-hermes"
        self.install(self.tmp, "bin/fake-hermes", with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)

        self.config(self.root, ROOT_CONFIG)
        self.config(self.worker, WORKER_CONFIG)
        self.cron(self.worker, WORKER_JOBS)
        self.cron(self.root, ROOT_JOBS)
        self.launchd = Launchd(self.tmp)
        self.uploads = self.broker()

    def install(self, home: Path, path: str, text: str) -> None:
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)

    def watch_state(self, state: dict) -> None:
        self.install(self.root, "gateway-watch.json", json.dumps(state))

    def boards(self, boards: list) -> None:
        self.install(self.root, "installed-boards.json", json.dumps(boards))

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

    def test_a_stale_or_missing_watch_state_is_reported(self) -> None:
        self.watch_state({"checked_at": time.time() - 3600, "state": "healthy", "restarts": []})
        self.assertEqual(self.check(), (1, (
            f"gateway watch {self.root}/gateway-watch.json: was last written 60 minutes ago, "
            "but watch-gateway runs every 5 (part 12)\n"
        )))

        (self.root / "gateway-watch.json").unlink()
        self.assertEqual(self.check(), (1, (
            f"gateway watch {self.root}/gateway-watch.json: is missing or unreadable, so watch-gateway has not run (part 12)\n"
        )))

    def test_a_spent_restart_budget_is_reported(self) -> None:
        now = time.time()
        self.watch_state({"checked_at": now, "state": "unresolved", "restarts": [now - 60, now - 600, now - 1200]})

        self.assertEqual(self.check(), (1, (
            f"gateway watch {self.root}/gateway-watch.json: restarted the gateway 3 times in the last hour and its "
            "secrets stay unresolved: read the factory-secrets log (part 14)\n"
        )))

    def test_restarts_that_recovered_or_aged_out_are_not_reported(self) -> None:
        now = time.time()
        self.watch_state({"checked_at": now, "state": "healthy", "restarts": [now - 60, now - 600, now - 1200]})
        self.assertEqual(self.check(), (0, ""))

        self.watch_state({"checked_at": now, "state": "unresolved", "restarts": [now - 60, now - 4000, now - 5000]})
        self.assertEqual(self.check(), (0, ""))

    def test_a_missing_or_open_heartbeat_file_is_reported_without_its_url(self) -> None:
        beat = self.root / "gateway-heartbeat-url"
        os.chmod(beat, 0o644)
        code, out = self.check()
        self.assertEqual((code, out), (1, f"gateway watch {beat}: has mode 644; it holds a URL, so use 600 (part 12)\n"))
        self.assertNotIn("monitor.example", out)

        beat.unlink()
        self.assertEqual(self.check(), (1, (
            f"gateway watch {beat}: is missing, so nothing alerts a person when the gateway, Slack, or the machine "
            "is down (part 12)\n"
        )))

    def test_the_watch_scripts_are_compared_with_the_repository(self) -> None:
        for name in ("watch-gateway", "restart-gateway"):
            source = f"skills/setup-factory/scripts/{name}"
            git(self.repo, "checkout", "-q", "-b", f"with-{name}")
            self.install(self.repo, source, f"#!/usr/bin/env python3\n{name}\n")
            git(self.repo, "add", ".")
            git(self.repo, "commit", "-q", "-m", name)
            self.install(self.root, f"scripts/{name}", f"#!/usr/bin/env python3\n{name}\n")
        git(self.repo, "checkout", "-q", "-b", "both")
        for name in ("watch-gateway", "restart-gateway"):
            git(self.repo, "merge", "-q", "--no-edit", f"with-{name}")
        sha = git(self.repo, "rev-parse", "--short=7", "HEAD")
        self.assertEqual(self.check("--ref", "both", "--no-fetch"), (0, ""))

        (self.root / "scripts" / "watch-gateway").unlink()
        self.install(self.root, "scripts/restart-gateway", "stale\n")
        self.assertEqual(self.check("--ref", "both", "--no-fetch"), (1, (
            f"script {self.root}/scripts/watch-gateway: is missing; install it from "
            "skills/setup-factory/scripts/watch-gateway (part 12)\n"
            f"script {self.root}/scripts/restart-gateway: differs from both at {sha} (part 12)\n"
        )))

    def test_the_constants_equal_the_watch_script(self) -> None:
        loader = importlib.machinery.SourceFileLoader(
            "watch_gateway", str(SCRIPTS.parents[2] / "skills" / "setup-factory" / "scripts" / "watch-gateway")
        )
        watch = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(watch)

        self.assertEqual(
            (check_install.WATCH_STATE, check_install.HEARTBEAT_FILE, check_install.WATCH_INTERVAL, check_install.RESTARTS_PER_HOUR),
            (watch.STATE_FILE, watch.HEARTBEAT_FILE, watch.INTERVAL_SECONDS, watch.RESTARTS_PER_HOUR),
        )
        launchd_name = Path(check_install.SERVICES[-1].plist).name
        self.assertEqual(launchd_name, f"{watch.LABEL}.plist")

    def test_plugin_difference_names_the_home(self) -> None:
        self.install(self.worker, "plugins/p1/__init__.py", "old\n")

        self.assertEqual(
            self.check(),
            (1, f"plugin p1 in {self.worker}: __init__.py differs from main at {self.sha}\n"),
        )

    def test_plugin_missing_from_root_home(self) -> None:
        shutil.rmtree(self.root / "plugins" / "p1")

        self.assertEqual(self.check(), (1, f"plugin p1 in {self.root}: not installed\n"))

    def test_a_root_only_plugin_is_not_required_in_the_worker_home(self) -> None:
        self.assertFalse((self.worker / "plugins" / "factory-uploads").exists())
        self.assertEqual(self.check(), (0, ""))

    def test_a_root_only_plugin_is_required_in_the_root_home_and_compared_there(self) -> None:
        self.install(self.root, "plugins/factory-uploads/__init__.py", "old\n")
        self.assertEqual(
            self.check(),
            (1, f"plugin factory-uploads in {self.root}: __init__.py differs from main at {self.sha}\n"),
        )
        shutil.rmtree(self.root / "plugins" / "factory-uploads")
        self.assertEqual(self.check(), (1, f"plugin factory-uploads in {self.root}: not installed\n"))

    def test_the_team_plugin_is_root_only(self) -> None:
        shutil.rmtree(self.root / "plugins" / "team")
        self.assertEqual(self.check(), (1, f"plugin team in {self.root}: not installed\n"))

        self.install(self.root, "plugins/team/__init__.py", "team\n")
        self.install(self.worker, "plugins/team/__init__.py", "team\n")
        self.assertEqual(
            self.check(), (1, f"plugin team in {self.worker}: installed, but it runs only in the root home\n")
        )

    def test_a_root_only_plugin_installed_in_the_worker_home_is_reported(self) -> None:
        self.install(self.worker, "plugins/factory-uploads/__init__.py", "uploads\n")

        self.assertEqual(
            self.check(),
            (1, f"plugin factory-uploads in {self.worker}: installed, but it runs only in the root home\n"),
        )

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
        stale = {**ROUTE, "prompt": "old triage", "secret": "abc...1234"}
        self.install(self.root, f"installed-config/{ROUTE_KEY}.json", json.dumps(stale))

        self.assertEqual(
            self.check(), (1, f"webhook config {ROUTE_KEY}: differs from the value rendered at {self.sha}\n")
        )

    def test_missing_route(self) -> None:
        (self.root / "installed-config" / f"{ROUTE_KEY}.json").unlink()

        self.assertEqual(self.check(), (1, f"webhook config {ROUTE_KEY}: not installed in {self.root}\n"))

    def test_missing_rate_limit(self) -> None:
        (self.root / "installed-config" / f"{RATE_LIMIT_KEY}.json").unlink()

        self.assertEqual(self.check(), (1, f"webhook config {RATE_LIMIT_KEY}: not installed in {self.root}\n"))

    def access_line(self, minutes_ago: int, route: str, status: int) -> str:
        stamp = (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f'{stamp},430 INFO aiohttp.access: 127.0.0.1 [12/Oct/2026:16:35:23 +0700] '
            f'"POST /webhooks/{route} HTTP/1.1" {status} 206 "-" "GitHub-Hookshot/7c39df5"\n'
        )

    def test_deliveries_rejected_in_the_last_hour_are_counted_per_route(self) -> None:
        self.install(self.root, "logs/agent.log.1", self.access_line(50, "github", 429))
        self.install(self.root, "logs/agent.log", "".join([
            self.access_line(90, "github", 429),
            self.access_line(30, "github", 200),
            self.access_line(20, "github", 429),
            self.access_line(10, "linear", 429),
            "2026-10-12 16:35:23,431 INFO gateway: status 429 mentioned outside an access line\n",
        ]))

        self.assertEqual(self.check(), (1, (
            "webhook route github: 2 rejected with HTTP 429 in the last hour; check platforms.webhook.extra.rate_limit\n"
            "webhook route linear: 1 rejected with HTTP 429 in the last hour; check platforms.webhook.extra.rate_limit\n"
        )))

    def test_rejections_older_than_an_hour_and_other_statuses_are_silent(self) -> None:
        self.install(self.root, "logs/agent.log", self.access_line(61, "github", 429) + self.access_line(5, "github", 200))

        self.assertEqual(self.check(), (0, ""))

    def test_a_missing_gateway_log_is_reported(self) -> None:
        (self.root / "logs" / "agent.log").unlink()

        self.assertEqual(
            self.check(), (1, f"webhook deliveries {self.root}: cannot read {self.root}/logs/agent.log to count 429 responses\n")
        )

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

    def test_default_assignee_that_would_route_an_unassigned_ready_card(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "kanban.default_assignee": "claude-worker"})

        self.assertEqual(self.check(), (1, f"config kanban.default_assignee in {self.root}: is not unset (part 10)\n"))

    def test_empty_default_assignee_is_unset(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "kanban.default_assignee": ""})

        self.assertEqual(self.check(), (0, ""))

    def test_upload_platform_that_is_not_enabled(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "platforms.factory_uploads.enabled": False})

        self.assertEqual(
            self.check(), (1, f"config platforms.factory_uploads.enabled in {self.root}: is not true (part 5)\n")
        )

    def test_upload_platform_with_no_repositories_refuses_every_upload(self) -> None:
        for repositories in ([], None, "example-org/example-repo"):
            with self.subTest(repositories=repositories):
                self.config(self.root, {**ROOT_CONFIG, "platforms.factory_uploads.extra.repositories": repositories})

                code, out = self.check()

                self.assertEqual(code, 1)
                self.assertEqual(
                    out,
                    f"config platforms.factory_uploads.extra.repositories in {self.root}: "
                    "is not a non-empty list of <owner>/<repo> names (part 5)\n",
                )

    def test_upload_platform_is_not_required_in_the_worker_home(self) -> None:
        self.assertNotIn("platforms.factory_uploads.enabled", WORKER_CONFIG)
        self.assertEqual(self.check(), (0, ""))

    def test_healthy_broker_gets_only_a_status_request(self) -> None:
        self.assertEqual(self.check(), (0, ""))

        self.assertEqual(self.uploads.requests, [b'{"op":"status"}\n'])

    def test_broker_that_is_not_listening(self) -> None:
        self.uploads.close()
        (self.root / "factory-uploads.sock").unlink()

        self.assertEqual(
            self.check(),
            (1, f"upload broker {self.root}/factory-uploads.sock: is unreachable: no broker is listening on it (part 5)\n"),
        )

    def test_broker_without_a_token(self) -> None:
        self.uploads.reply = json.dumps({**STATUS, "token_present": False}).encode() + b"\n"

        self.assertEqual(
            self.check(),
            (
                1,
                f"upload broker {self.root}/factory-uploads.sock: "
                "reports token_present false: the broker holds no upload token (part 5)\n",
            ),
        )

    def test_broker_without_linear_credentials_leaves_every_worker_without_linear(self) -> None:
        self.uploads.reply = json.dumps({**STATUS, "linear_ready": False}).encode() + b"\n"

        self.assertEqual(
            self.check(),
            (
                1,
                f"upload broker {self.root}/factory-uploads.sock: reports linear_ready false: the broker holds no "
                "Linear app credentials, so no worker reaches Linear (part 5)\n",
            ),
        )

    def test_broker_without_the_github_app_leaves_the_workers_gh_to_expire(self) -> None:
        self.uploads.reply = json.dumps({**STATUS, "github_app_ready": False}).encode() + b"\n"

        self.assertEqual(
            self.check(),
            (
                1,
                f"upload broker {self.root}/factory-uploads.sock: reports github_app_ready false: the broker cannot "
                "mint the GitHub App installation token, so the worker's gh login expires within the hour (part 5)\n",
            ),
        )

    def test_broker_without_the_proxy_token_matters_only_when_one_is_configured(self) -> None:
        self.uploads.reply = json.dumps({**STATUS, "proxy_token_present": False}).encode() + b"\n"
        self.assertEqual(self.check(), (0, ""))

        self.proxy("CLAUDE_PROXY_TOKEN", self.helper())

        self.assertEqual(
            self.check(),
            (
                1,
                f"upload broker {self.root}/factory-uploads.sock: reports proxy_token_present false: the broker "
                "holds no CLAUDE_PROXY_TOKEN, so no worker reaches the proxy (part 5)\n",
            ),
        )

    def test_broker_without_an_mcp_token_matters_only_for_a_configured_server(self) -> None:
        self.uploads.reply = json.dumps({**STATUS, "mcp_tokens_present": {"sentry": True, "posthog": False}}).encode() + b"\n"
        self.proxy("CLAUDE_PROXY_TOKEN", self.helper(), {"sentry": "SENTRY_ACCESS_TOKEN"})
        self.assertEqual(self.check(), (0, ""))

        self.proxy("CLAUDE_PROXY_TOKEN", self.helper(), {"sentry": "SENTRY_ACCESS_TOKEN", "posthog": "POSTHOG_MCP_KEY", "figma": "FIGMA_MCP_TOKEN"})

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertIn(
            f"upload broker {self.root}/factory-uploads.sock: holds no token for the figma MCP server: the broker "
            "holds no FIGMA_MCP_TOKEN, so every worker run is refused (part 5)\n"
            f"upload broker {self.root}/factory-uploads.sock: holds no token for the posthog MCP server: the broker "
            "holds no POSTHOG_MCP_KEY, so every worker run is refused (part 5)\n",
            out,
        )

    def test_broker_that_does_not_answer_a_status_request_properly(self) -> None:
        for reply in (b"not json\n", b'{"ok": false}\n', b"[]\n", b""):
            with self.subTest(reply=reply):
                self.uploads.reply = reply

                code, out = self.check()

                self.assertEqual(code, 1)
                self.assertIn("upload broker", out)

    def test_worker_wait_limit_that_differs_from_the_runbook(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, "terminal.timeout": 1200})

        self.assertEqual(self.check(), (1, f"config terminal.timeout in {self.worker}: is not 400 (part 5)\n"))

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

    def proxy(self, token_env: str, worker_command: dict | None = None, mcp_envs: dict | None = None) -> None:
        servers = {name: {"url": f"https://mcp.example/{name}", "token_env": env} for name, env in (mcp_envs or {}).items()}
        settings = {**SETTINGS, "claude_session": {"auth_token_env": token_env, "mcp_servers": servers}}
        self.config(self.root, {**ROOT_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        worker = {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings}
        self.config(self.worker, {**worker, **({"secrets.command": worker_command} if worker_command else {})})

    def helper(self) -> dict:
        return {"enabled": True, "command": f"python3 {self.worker}/scripts/worker-secrets"}

    def test_worker_that_loads_its_proxy_token_through_the_helper_is_silent(self) -> None:
        self.proxy("CLAUDE_PROXY_TOKEN", self.helper())

        self.assertEqual(self.check(), (0, ""))

    def test_worker_with_a_proxy_token_and_another_secret_source(self) -> None:
        drift = (
            f"config secrets.command in {self.worker}: "
            f"is not the worker helper python3 {self.worker}/scripts/worker-secrets (part 5)\n"
        )
        for worker in (
            {"enabled": True, "command": f"{self.root}/bin/factory-secrets"},
            {"enabled": True, "command": "env TOKEN=s3cr3t-value sh -c helper"},
            {**self.helper(), "enabled": False},
            None,
        ):
            with self.subTest(worker=worker):
                self.proxy("CLAUDE_PROXY_TOKEN", worker)

                code, out = self.check()

                self.assertEqual((code, out), (1, drift))
                self.assertNotIn("s3cr3t-value", out)

    def test_worker_without_a_proxy_token_loads_no_secret(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, "secrets.command": {"enabled": True, "command": "s3cr3t-value"}})

        code, out = self.check()

        self.assertEqual(
            (code, out),
            (1, f"config secrets.command in {self.worker}: is set, but a worker without a proxy token loads no secret (part 5)\n"),
        )
        self.assertNotIn("s3cr3t-value", out)

    def test_worker_secret_source_disabled_without_a_proxy_token_is_silent(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, "secrets.command": {"enabled": False, "command": "y"}})

        self.assertEqual(self.check(), (0, ""))

    def test_secrets_only_the_gateway_holds_are_reported_in_the_worker_env_by_name_only(self) -> None:
        self.proxy("CLAUDE_PROXY_TOKEN", self.helper())
        self.install(self.worker, ".env", "\n".join([
            "GH_CONFIG_DIR=/gh", "LINEAR_CLIENT_ID=s3cr3t-1", "export LINEAR_CLIENT_SECRET='s3cr3t-2'",
            "FACTORY_INFISICAL_CLIENT_ID=s3cr3t-3", "FACTORY_INFISICAL_CLIENT_SECRET=s3cr3t-4",
            "CLAUDE_PROXY_TOKEN=s3cr3t-5", "",
        ]))

        code, out = self.check()

        held = "is set, but the worker holds no factory secret; remove it"
        self.assertEqual((code, out), (1, "".join([
            f"secret LINEAR_CLIENT_ID in {self.worker}/.env: {held} (part 8)\n",
            f"secret LINEAR_CLIENT_SECRET in {self.worker}/.env: {held} (part 8)\n",
            f"secret FACTORY_INFISICAL_CLIENT_ID in {self.worker}/.env: {held} (part 14)\n",
            f"secret FACTORY_INFISICAL_CLIENT_SECRET in {self.worker}/.env: {held} (part 14)\n",
            f"secret CLAUDE_PROXY_TOKEN in {self.worker}/.env: {held} (part 3)\n",
        ])))
        self.assertNotIn("s3cr3t", out)

    def test_an_mcp_token_in_the_worker_env_is_reported_by_name_only(self) -> None:
        self.uploads.reply = json.dumps({**STATUS, "mcp_tokens_present": {"sentry": True}}).encode() + b"\n"
        self.proxy("CLAUDE_PROXY_TOKEN", self.helper(), {"sentry": "SENTRY_ACCESS_TOKEN"})
        self.install(self.worker, ".env", "SENTRY_ACCESS_TOKEN=s3cr3t-6\n")

        code, out = self.check()

        self.assertEqual((code, out), (
            1,
            f"secret SENTRY_ACCESS_TOKEN in {self.worker}/.env: is set, but the worker holds no factory secret; "
            "remove it (part 3)\n",
        ))
        self.assertNotIn("s3cr3t", out)

    def test_an_mcp_token_variable_hermes_would_pass_to_every_process(self) -> None:
        self.uploads.reply = json.dumps({**STATUS, "mcp_tokens_present": {"sentry": True}}).encode() + b"\n"
        self.proxy("CLAUDE_PROXY_TOKEN", self.helper(), {"sentry": "OTHER_SENTRY_TOKEN"})

        self.assertEqual(
            self.check(),
            (1, f"config claude_session.mcp_servers.sentry.token_env in {self.worker}: names OTHER_SENTRY_TOKEN, which "
                "plugins/factory-secret-names/plugin.yaml does not declare, so every process a home starts "
                "inherits it (part 3)\n"),
        )

    def test_a_proxy_token_variable_hermes_would_pass_to_every_process(self) -> None:
        self.proxy("OTHER_PROXY_TOKEN", self.helper())

        self.assertEqual(
            self.check(),
            (1, f"config claude_session.auth_token_env in {self.worker}: names OTHER_PROXY_TOKEN, which "
                "plugins/factory-secret-names/plugin.yaml does not declare, so every process a home starts "
                "inherits it (part 3)\n"),
        )

    def test_worker_that_passes_variables_to_its_commands(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, "terminal.env_passthrough": ["LINEAR_CLIENT_SECRET"]})

        self.assertEqual(
            self.check(), (1, f"config terminal.env_passthrough in {self.worker}: is not empty (part 5)\n")
        )

    def test_missing_and_duplicated_cron_jobs(self) -> None:
        self.cron(self.worker, [job for job in WORKER_JOBS if job["name"] != "refresh-checkouts"])
        self.cron(self.root, [*ROOT_JOBS, *ROOT_JOBS])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-checkouts in {self.worker}: is missing (part 12)\n"
                f"cron job check-install in {self.root}: exists 2 times (part 12)\n"
                f"cron job {RETRY_SCRIPT} in {self.root}: exists 2 times (part 12)\n"),
        )

    def test_cron_job_changed_by_hand(self) -> None:
        self.cron(self.worker, [
            cron_job("refresh-gh-app-login", 30, enabled=False),
            cron_job("refresh-checkouts", 15, no_agent=False),
            cron_job("refresh-owned-refs", 1),
            cron_job("reopen-conflicts", 5),
            cron_job("dispatch-triage", 1),
        ])
        self.cron(self.root, [{**cron_job("check-install", 60, "local"), "script": ALERT_SCRIPT}, ROOT_JOBS[1]])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-gh-app-login in {self.worker}: is disabled (part 10)\n"
                f"cron job refresh-checkouts in {self.worker}: runs every 15m, not every 60m (part 12)\n"
                f"cron job refresh-checkouts in {self.worker}: is not a script-only job that runs refresh-checkouts "
                "(part 12)\n"
                f'cron job check-install in {self.root}: delivers to "local", not the operator\'s chat platform '
                "(part 12)\n"),
        )

    def test_cron_jobs_the_runbook_does_not_create(self) -> None:
        self.cron(self.worker, [*WORKER_JOBS, cron_job("nightly-cleanup", 1440)])
        self.cron(self.root, [*ROOT_JOBS, cron_job("refresh-checkouts", 60)])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-checkouts in {self.root}: belongs to the worker profile (part 12)\n"
                f"cron job nightly-cleanup in {self.worker}: is not a job the setup runbook creates\n"),
        )

    def test_failed_last_run_names_its_error_until_a_run_succeeds(self) -> None:
        error = (
            "Script exited with code 1\nstderr:\nrefresh-owned-refs: hermes kanban --board triage list failed: "
            "kanban: board 'triage' does not exist.\nsecond line"
        )
        failed = cron_job("refresh-owned-refs", 1, last_status="error", last_error=error)
        self.cron(self.worker, [failed if job["name"] == failed["name"] else job for job in WORKER_JOBS])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-owned-refs in {self.worker}: last run failed: Script exited with code 1: "
                "refresh-owned-refs: hermes kanban --board triage list failed: kanban: board 'triage' does not "
                "exist. (part 12)\n"),
        )

        recovered = {**failed, "last_status": "ok", "last_error": None,
                     "last_failure": {"at": "2026-10-17T22:31:26+07:00", "detail": error}}
        self.cron(self.worker, [recovered if job["name"] == failed["name"] else job for job in WORKER_JOBS])

        self.assertEqual(self.check(), (0, ""))

    def test_failed_last_run_masks_secrets_in_its_error(self) -> None:
        self.install(self.root, ".env", "SLACK_HOME_CHANNEL=C1\nSLACK_BOT_TOKEN=xoxb-short\n")
        error = "Script exited with code 1\nstdout:\nrejected xoxb-short and ghs_16C7e42F292c6912E7710c838347Ae178B4a"
        failed = cron_job("reopen-conflicts", 5, last_status="error", last_error=error)
        self.cron(self.worker, [failed if job["name"] == failed["name"] else job for job in WORKER_JOBS])

        code, out = self.check()

        self.assertEqual(
            (code, out),
            (1, f"cron job reopen-conflicts in {self.worker}: last run failed: Script exited with code 1: "
                "rejected [redacted] and [redacted] (part 12)\n"),
        )

    def test_failed_check_install_run_does_not_fail_a_clean_machine(self) -> None:
        error = (
            "Script exited with code 1\nstderr:\nprofile claude-worker: scripts/claude-session differs from main "
            "at c02dda9 (part 12)"
        )
        self.cron(self.root, [{**ROOT_JOBS[0], "last_status": "error", "last_error": error}, *ROOT_JOBS[1:]])

        self.assertEqual(self.check(), (0, ""))

        failed = cron_job("refresh-checkouts", 60, last_status="error", last_error="Script exited with code 1")
        self.cron(self.worker, [failed if job["name"] == failed["name"] else job for job in WORKER_JOBS])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-checkouts in {self.worker}: last run failed: Script exited with code 1 "
                "(part 12)\n"),
        )

    def test_undelivered_last_run_names_its_delivery_error(self) -> None:
        undelivered = {**ROOT_JOBS[0], "last_status": "delivery_failed", "last_error": None,
                       "last_delivery_error": "slack: channel_not_found\nmore"}
        self.cron(self.root, [undelivered, *ROOT_JOBS[1:]])

        self.assertEqual(
            self.check(),
            (1, f"cron job check-install in {self.root}: last run's output was not delivered: "
                "slack: channel_not_found (part 12)\n"),
        )

    def test_check_install_job_left_in_the_worker_profile(self) -> None:
        self.cron(self.worker, [*WORKER_JOBS, cron_job("check-install", 60, "slack")])

        self.assertEqual(
            self.check(),
            (1, f"cron job check-install in {self.worker}: belongs to the root profile (part 12)\n"),
        )

    def test_delivery_without_a_home_channel_is_reported(self) -> None:
        (self.root / ".env").unlink()

        self.assertEqual(
            self.check(),
            (1, f"cron job check-install in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"
                f"cron job {RETRY_SCRIPT} in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"),
        )

    def test_empty_home_channel_is_not_a_target(self) -> None:
        self.install(self.root, ".env", "SLACK_HOME_CHANNEL=\nSLACK_BOT_TOKEN=x\n")

        self.assertEqual(
            self.check(),
            (1, f"cron job check-install in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"
                f"cron job {RETRY_SCRIPT} in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"),
        )

    def test_home_channel_from_config_resolves_delivery(self) -> None:
        (self.root / ".env").unlink()
        self.config(self.root, {**ROOT_CONFIG, "platforms.slack.home_channel": {"chat_id": "C0123"}})

        self.assertEqual(self.check(), (0, ""))

    def test_explicit_and_local_delivery_targets_need_no_home_channel(self) -> None:
        (self.root / ".env").unlink()
        self.cron(self.root, [{**cron_job("check-install", 60, "slack:C0123"), "script": ALERT_SCRIPT}, cron_job(RETRY_SCRIPT, 10, "slack:C0123")])

        self.assertEqual(self.check(), (0, ""))

    def test_root_alert_script_missing_or_changed(self) -> None:
        script = self.root / "scripts" / ALERT_SCRIPT
        script.write_text("old\n")
        changed = self.check()
        script.unlink()
        missing = self.check()

        self.assertEqual(
            (changed, missing),
            ((1, f"script {script}: differs from main at {self.sha} (part 12)\n"),
             (1, f"script {script}: is missing; install it from {ALERT_SOURCE} (part 12)\n")),
        )

    def test_retry_cron_job_missing_or_changed(self) -> None:
        self.cron(self.root, ROOT_JOBS[:1])
        missing = self.check()
        self.cron(self.root, [ROOT_JOBS[0], cron_job(RETRY_SCRIPT, 30, "slack", enabled=False)])
        changed = self.check()

        self.assertEqual(
            (missing, changed),
            ((1, f"cron job {RETRY_SCRIPT} in {self.root}: is missing (part 12)\n"),
             (1, f"cron job {RETRY_SCRIPT} in {self.root}: is disabled (part 12)\n"
                 f"cron job {RETRY_SCRIPT} in {self.root}: runs every 30m, not every 10m (part 12)\n")),
        )

    def test_retry_wrapper_missing_or_changed(self) -> None:
        script = self.root / "scripts" / RETRY_SCRIPT
        script.write_text("old\n")
        changed = self.check()
        script.unlink()
        missing = self.check()

        self.assertEqual(
            (changed, missing),
            ((1, f"script {script}: differs from main at {self.sha} (part 12)\n"),
             (1, f"script {script}: is missing; install it from {RETRY_SOURCE} (part 12)\n")),
        )

    def test_webhook_retry_installed_in_the_worker_home_is_reported(self) -> None:
        self.install(self.worker, "plugins/webhook-retry/__init__.py", "retry\n")

        self.assertEqual(
            self.check(),
            (1, f"plugin webhook-retry in {self.worker}: installed, but it runs only in the root home\n"),
        )

    def test_missing_owned_refs_file(self) -> None:
        self.owned_refs.unlink()

        self.assertEqual(
            self.check(),
            (1, f"owned refs {self.owned_refs}: is missing; create or run the refresh-owned-refs cron job (part 12)\n"),
        )

    def test_route_filtering_on_linear_ids_with_no_ids_in_the_file(self) -> None:
        self.owned_refs.write_text('["goodyear/abc-1-fix"]\n')

        self.assertEqual(
            self.check(),
            (
                1,
                f"webhook config {ROUTE_KEY}: filters on Linear issue ids, but {self.owned_refs} holds none; "
                "deploy the profile and run refresh-owned-refs once before the route (part 11)\n",
            ),
        )

    def test_route_filtering_on_linear_ids_with_an_empty_board(self) -> None:
        self.owned_refs.write_text("[]\n")

        self.assertEqual(self.check(), (0, ""))

    def test_missing_or_stale_triage_refs_file(self) -> None:
        triage_refs = self.root / "triage-refs.json"
        stale = time.time() - 600
        os.utime(triage_refs, (stale, stale))

        self.assertEqual(
            self.check(),
            (1, f"owned refs {triage_refs}: is 10 minutes old; the refresh-owned-refs cron job is not running (part 12)\n"),
        )
        triage_refs.unlink()

        self.assertEqual(
            self.check(),
            (1, f"owned refs {triage_refs}: is missing; create or run the refresh-owned-refs cron job (part 12)\n"),
        )

    def test_missing_triage_board(self) -> None:
        self.boards(BOARDS[:1])

        self.assertEqual(self.check(), (1, f"kanban board triage: is missing in {self.root} (part 10)\n"))

    def kanban_database(self, path: Path, columns: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as board:
            board.execute(f"CREATE TABLE tasks ({columns})")
        board.close()
        return path

    def test_board_databases_that_hold_the_columns_the_blocker_check_reads_are_silent(self) -> None:
        columns = "id, title, body, branch_name, status, assignee"
        self.kanban_database(self.root / "kanban.db", columns)
        self.kanban_database(self.root / "kanban" / "boards" / "triage" / "kanban.db", columns)

        self.assertEqual(self.check(), (0, ""))

    def test_a_board_database_missing_a_column_the_blocker_check_reads(self) -> None:
        triage = self.kanban_database(self.root / "kanban" / "boards" / "triage" / "kanban.db", "id, title, body, state")

        self.assertEqual(self.check(), (1, (
            f"kanban board database {triage}: has no tasks.branch_name, tasks.status, so a card cannot wait for "
            "its blockers (part 15, check 29)\n"
        )))

    def test_a_board_database_that_cannot_be_read(self) -> None:
        board = self.root / "kanban.db"
        board.write_text("not a database")

        code, output = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(
            output, f"kanban board database {board}: cannot be read for blockers: file is not a database "
            "(part 15, check 29)\n",
        )

    def default_repository(self) -> Path:
        checkout = self.tmp / "app"
        git(self.tmp, "init", "-q", str(checkout))
        git(checkout, "commit", "-q", "--allow-empty", "-m", "init")
        git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(checkout, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        settings = {**SETTINGS, "repositories": [{"full_name": "org/app", "path": str(checkout), "routing": "default"}]}
        self.config(self.root, {**ROOT_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        return checkout

    def test_triage_board_must_anchor_worktrees_at_the_default_repository(self) -> None:
        checkout = self.default_repository()

        self.assertEqual(self.check(), (1, f"kanban board triage: default_workdir is not {checkout} (part 10)\n"))
        self.boards([BOARDS[0], {"slug": "triage", "default_workdir": str(checkout)}])

        self.assertEqual(self.check(), (0, ""))

    def test_factory_checkout_without_the_triage_playbook(self) -> None:
        (self.repo / TRIAGE_PLAYBOOK).unlink()

        self.assertEqual(self.check("--no-fetch"), (
            1,
            f"triage playbook {self.repo}: has no {TRIAGE_PLAYBOOK}, so triage cards cannot follow the triage "
            "playbook; update the checkout to a commit that vendors it (part 10)\n",
        ))

    def test_triage_cap_that_is_unset_or_not_positive(self) -> None:
        for value in (None, 0, -1, "2", True):
            with self.subTest(value=value):
                root = {k: v for k, v in ROOT_CONFIG.items() if k != MAX_RUNNING_KEY}
                worker = {k: v for k, v in WORKER_CONFIG.items() if k != MAX_RUNNING_KEY}
                if value is not None:
                    root[MAX_RUNNING_KEY] = worker[MAX_RUNNING_KEY] = value
                self.config(self.root, root)
                self.config(self.worker, worker)

                self.assertEqual(self.check(), (1, "".join(
                    f"config {MAX_RUNNING_KEY} in {home}: is not a positive limit on running triage cards (part 10)\n"
                    for home in (self.root, self.worker)
                )))

    def test_triage_cap_that_differs_between_homes(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, MAX_RUNNING_KEY: 3})

        self.assertEqual(self.check(), (1, (
            f"config {MAX_RUNNING_KEY}: differs between {self.root} and {self.worker} (part 10)\n"
        )))

    def test_stale_owned_refs_file(self) -> None:
        stale = time.time() - 600
        os.utime(self.owned_refs, (stale, stale))

        self.assertEqual(
            self.check(),
            (1, f"owned refs {self.owned_refs}: is 10 minutes old; the refresh-owned-refs cron job is not running (part 12)\n"),
        )

    def repositories(self, *paths: Path) -> None:
        settings = {**SETTINGS, "repositories": [{"full_name": f"org/{p.name}", "path": str(p)} for p in paths]}
        self.config(self.root, {**ROOT_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})

    def claude_session(self, **fields) -> None:
        settings = {**SETTINGS, "claude_session": fields}
        self.config(self.root, {**ROOT_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})

    def test_transport_model_without_a_sonnet_tier_is_reported(self) -> None:
        self.claude_session(model="transport-model", tier_models={"haiku": "haiku-model"})

        self.assertEqual(self.check(), (
            1,
            f"model tier claude_session in {self.worker}: has a model but no tier_models.sonnet, "
            "so every subagent runs on that model (part 3)\n",
        ))

    def test_transport_model_with_a_sonnet_tier_is_silent(self) -> None:
        self.claude_session(model="transport-model", tier_models={"sonnet": "sonnet-model"})

        self.assertEqual(self.check(), (0, ""))

    def test_malformed_tier_models_are_reported_not_fatal(self) -> None:
        self.claude_session(model="transport-model", tier_models=["sonnet"])

        status, output = self.check()

        self.assertEqual(status, 1)
        self.assertIn("has a model but no tier_models.sonnet", output)

    def test_no_transport_model_needs_no_tiers(self) -> None:
        self.claude_session(model="")

        self.assertEqual(self.check(), (0, ""))

    def checkout(self) -> Path:
        path = self.tmp / "checkout"
        git(self.tmp, "clone", "-q", str(self.tmp / "seed"), str(path))
        return path

    def test_checkout_with_origin_head_is_silent(self) -> None:
        self.repositories(self.checkout())

        self.assertEqual(self.check(), (0, ""))

    def test_checkout_without_origin_head(self) -> None:
        checkout = self.checkout()
        git(checkout, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        self.repositories(checkout)

        self.assertEqual(
            self.check(),
            (1, f"checkout {checkout}: origin/HEAD is not set; run git -C {checkout} remote set-head origin -a "
                "(part 10)\n"),
        )

    def test_checkout_that_does_not_exist(self) -> None:
        missing = self.tmp / "missing"
        self.repositories(self.checkout(), missing)

        self.assertEqual(self.check(), (1, f"checkout {missing}: does not exist (part 10)\n"))

    def test_team_skills_directory_that_does_not_exist(self) -> None:
        missing = self.tmp / "missing"
        self.config(self.root, {**ROOT_CONFIG, TEAM_KEY: str(missing)})

        self.assertEqual(self.check(), (1, f"team skills {missing}: does not exist (part 5)\n"))

    def test_team_skills_directory_with_no_skill(self) -> None:
        (self.tmp / "skills" / "not-a-skill").mkdir(parents=True)
        self.config(self.root, {**ROOT_CONFIG, TEAM_KEY: str(self.tmp / "skills")})

        self.assertEqual(self.check(), (1, f"team skills {self.tmp}/skills: holds no <name>/SKILL.md (part 5)\n"))

    def test_team_skills_allow_naming_a_skill_the_directory_lacks(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, TEAM_ALLOW_KEY: ["example-skill", "ghost", "alpha"]})

        self.assertEqual(
            self.check(),
            (1, f"team skills {TEAM_SKILLS}: lacks the allowed skill alpha (part 5)\n"
                f"team skills {TEAM_SKILLS}: lacks the allowed skill ghost (part 5)\n"),
        )

    def test_team_skills_allow_that_is_not_a_list(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, TEAM_ALLOW_KEY: "example-skill"})

        self.assertEqual(
            self.check(),
            (1, "team skills plugins.entries.team.settings.allow: is not a list of skill names (part 5)\n"),
        )

    def test_team_skills_directory_holding_only_an_invalid_name(self) -> None:
        skills = self.tmp / "skills"
        (skills / "bad.name").mkdir(parents=True)
        (skills / "bad.name" / "SKILL.md").write_text("---\nname: bad.name\n---\n")
        self.config(self.root, {**ROOT_CONFIG, TEAM_KEY: str(skills)})

        self.assertEqual(self.check(), (1, f"team skills {skills}: holds no <name>/SKILL.md (part 5)\n"))

    def test_team_skills_with_a_matching_allow_is_silent(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, TEAM_ALLOW_KEY: ["example-skill"]})

        self.assertEqual(self.check(), (0, ""))

    def test_unset_team_skills_directory_is_reported_once_by_the_config_row(self) -> None:
        config = dict(ROOT_CONFIG)
        del config[TEAM_KEY]
        self.config(self.root, config)

        self.assertEqual(
            self.check(),
            (1, f"config {TEAM_KEY} in {self.root}: is not set to the team skills directory (part 5)\n"),
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
                f"plugin p1 in {self.worker}: __init__.py differs from main at {merged}\n"
                f"source checkout {self.repo}: is behind main at {merged}. Fast-forward only this checkout "
                f"with git -C {self.repo} merge --ff-only {git(self.repo, 'rev-parse', 'origin/main')}. "
                "Leave the worker-owned checkouts to refresh-checkouts.\n"),
        )

    def advance_origin(self) -> str:
        seed = self.tmp / "seed"
        (seed / "plugins" / "p1" / "__init__.py").write_text("merged\n")
        git(seed, "commit", "-qam", "merge")
        return git(seed, "rev-parse", "HEAD")

    def commit_locally(self) -> None:
        (self.repo / "local.txt").write_text("local\n")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-qm", "local")

    def source_lines(self) -> list:
        return [line for line in self.check()[1].splitlines() if line.startswith("source checkout")]

    def assert_fast_forward_line(self, sha: str) -> None:
        (line,) = self.source_lines()
        self.assertIn(str(self.repo), line)
        self.assertIn(f"git -C {self.repo} merge --ff-only {sha}", line)

    def assert_manual_line(self, reason: str) -> None:
        (line,) = self.source_lines()
        self.assertIn(str(self.repo), line)
        self.assertIn(reason, line)
        self.assertNotIn("merge --ff-only", line)

    def test_clean_source_checkout_on_main_that_is_behind_gets_the_fast_forward_command(self) -> None:
        sha = self.advance_origin()

        self.assert_fast_forward_line(sha)

    def test_source_checkout_ahead_of_origin_gets_no_merge_command(self) -> None:
        self.install(self.worker, "SOUL.md", "stale\n")
        self.commit_locally()

        self.assert_manual_line("is ahead of main")

    def test_diverged_source_checkout_gets_no_merge_command(self) -> None:
        self.advance_origin()
        self.commit_locally()

        self.assert_manual_line("has diverged from main")

    def test_source_checkout_on_another_branch_gets_no_merge_command(self) -> None:
        self.advance_origin()
        git(self.repo, "checkout", "-q", "-b", "side")

        self.assert_manual_line("is on side, not main")

    def test_dirty_source_checkout_gets_no_merge_command(self) -> None:
        self.advance_origin()
        (self.repo / "plugins" / "p1" / "__init__.py").write_text("dirty\n")

        self.assert_manual_line("has uncommitted changes")

    def test_print_source_prints_the_checkout_the_distribution_names(self) -> None:
        self.assertEqual(self.check("--print-source"), (0, f"{self.repo}\n"))

    def test_source_checkout_at_origin_head_gets_no_fast_forward_line(self) -> None:
        self.install(self.worker, "SOUL.md", "stale\n")

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertNotIn("source checkout", out)

    def test_an_explicit_ref_gets_no_source_checkout_line(self) -> None:
        merged = self.advance_origin()[:7]

        self.assertEqual(
            self.check("--ref", "origin/main"),
            (1, f"plugin p1 in {self.root}: __init__.py differs from origin/main at {merged}\n"
                f"plugin p1 in {self.worker}: __init__.py differs from origin/main at {merged}\n"),
        )

    def test_behind_source_checkout_without_drift_is_silent(self) -> None:
        seed = self.tmp / "seed"
        (seed / "README.md").write_text("docs only\n")
        git(seed, "add", "README.md")
        git(seed, "commit", "-qm", "docs")

        self.assertEqual(self.check(), (0, ""))

    def test_checkout_state_does_not_matter(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "plugins" / "p1" / "__init__.py").write_text("dirty\n")
        (self.repo / "profiles" / "claude-worker" / "SOUL.md").write_text("dirty\n")

        self.assertEqual(self.check(), (0, ""))

    def test_hermes_is_found_in_the_installer_bin_without_path(self) -> None:
        home = self.tmp / "home"
        self.install(home, ".local/bin/hermes", with_interpreter(FAKE_HERMES))
        os.chmod(home / ".local" / "bin" / "hermes", 0o755)
        agents = f"{str(home).lstrip('/')}/Library/LaunchAgents"
        self.launchd.plist(f"{agents}/ai.hermes.gateway.plist", "ai.hermes.gateway")
        self.launchd.plist(
            f"{agents}/ai.patinaproject.factory.gateway-watch.plist", "ai.patinaproject.factory.gateway-watch"
        )
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
        self.launchctl.write_text(with_interpreter(FAKE_LAUNCHCTL))
        os.chmod(self.launchctl, 0o755)
        self.home = tmp / "launchd-home"
        self.daemon = self.plist("Library/LaunchDaemons/com.example.cloudflared.plist", "com.example.cloudflared")
        self.agent = self.plist(
            f"{str(self.home).lstrip('/')}/Library/LaunchAgents/ai.hermes.gateway.plist", "ai.hermes.gateway"
        )
        self.watch = self.plist(
            f"{str(self.home).lstrip('/')}/Library/LaunchAgents/ai.patinaproject.factory.gateway-watch.plist",
            "ai.patinaproject.factory.gateway-watch",
        )
        uid = os.getuid()
        self.states({
            "system/com.example.cloudflared": "running", f"user/{uid}/ai.hermes.gateway": "running",
            f"user/{uid}/ai.patinaproject.factory.gateway-watch": "not running",
        })
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


FAKE_SESSION = """import json, os, sys
args = sys.argv[1:]
reports = json.load(open(os.path.join(os.path.dirname(__file__), "reports.json")))
assert args[:2] == ["--mcp-status", "--repo"] and os.environ["HERMES_HOME"] == os.path.dirname(os.path.dirname(__file__))
code, report = reports[args[2]]
print(json.dumps(report))
sys.exit(code)
"""


class CheckMcpServersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.worker = self.tmp / "worker"
        (self.worker / "scripts").mkdir(parents=True)
        (self.worker / "scripts" / "claude-session").write_text(FAKE_SESSION)
        self.settings = {"repositories": [
            {"full_name": "example-org/app", "path": str(self.tmp)},
            {"full_name": "example-org/site", "path": str(self.tmp)},
            {"full_name": "example-org/gone", "path": str(self.tmp / "missing")},
        ]}

    def check(self, reports: dict) -> list:
        (self.worker / "scripts" / "reports.json").write_text(json.dumps(reports))
        return [str(d) for d in check_install.check_mcp_servers(self.settings, self.worker)]

    def test_reports_each_server_neither_connected_nor_disabled(self) -> None:
        app = {"servers": [
            {"name": "linear", "status": "connected"}, {"name": "figma", "status": "needs-auth"},
            {"name": "cms", "status": "failed"}, {"name": "vercel", "status": "needs-auth"},
        ], "disabled": ["vercel"]}

        self.assertEqual(self.check({
            "example-org/app": [0, app],
            "example-org/site": [2, {"error": "MCP tokens: the gateway's broker holds no token for sentry"}],
        }), [
            "mcp figma in example-org/app: is needs-auth, not connected or disabled (part 10)",
            "mcp cms in example-org/app: is failed, not connected or disabled (part 10)",
            "mcp example-org/site: cannot read the MCP server status: MCP tokens: the gateway's broker holds no "
            "token for sentry (part 10)",
        ])

    def test_every_server_connected_or_disabled_is_silent(self) -> None:
        report = {"servers": [{"name": "linear", "status": "connected"}, {"name": "figma", "status": "needs-auth"}], "disabled": ["figma"]}

        self.assertEqual(self.check({"example-org/app": [0, report], "example-org/site": [0, report]}), [])

    def test_a_probe_without_a_report_is_named(self) -> None:
        (self.worker / "scripts" / "claude-session").write_text("print('not json')\n")

        self.assertEqual(self.check({}), [
            "mcp example-org/app: cannot read the MCP server status: exited 0 without a report (part 10)",
            "mcp example-org/site: cannot read the MCP server status: exited 0 without a report (part 10)",
        ])

    def test_a_leftover_sentry_token_env_is_reported(self) -> None:
        self.settings["claude_session"] = {"sentry_token_env": "SENTRY_ACCESS_TOKEN"}
        report = {"servers": [], "disabled": []}

        self.assertEqual(self.check({"example-org/app": [0, report], "example-org/site": [0, report]}), [
            "config claude_session.sentry_token_env: is replaced by mcp_servers.sentry, so every worker run is refused (part 10)",
        ])

    def test_a_report_that_is_not_an_object_is_named(self) -> None:
        self.assertEqual(self.check({"example-org/app": [2, ["no"]], "example-org/site": [0, "no"]}), [
            "mcp example-org/app: cannot read the MCP server status: exited 2 without a report (part 10)",
            "mcp example-org/site: cannot read the MCP server status: exited 0 without a report (part 10)",
        ])

    def test_without_an_installed_claude_session_or_settings_nothing_runs(self) -> None:
        (self.worker / "scripts" / "claude-session").unlink()

        self.assertEqual(self.check({}), [])
        self.assertEqual([str(d) for d in check_install.check_mcp_servers(None, self.worker)], [])


FAKE_REGISTRY = """import json
from pathlib import Path
registered = json.loads((Path(__file__).parent / "registered.json").read_text())


class Registry:
    def get_all_tool_names(self):
        return list(registered)

    def get_toolset_for_tool(self, name):
        return registered[name]


registry = Registry()
"""
FAKE_TERMINAL_TOOL = """import json, os
from pathlib import Path
import fake_dependency
(Path(__file__).parent / "seen.json").write_text(json.dumps({
    "hermes_home": os.environ["HERMES_HOME"],
    "no_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
}))
"""
PLUGIN = REPO / "plugins" / "blocker-order" / "__init__.py"


class CheckHermesToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.tree = self.root / "hermes-agent"
        tools = self.tree / "tools"
        tools.mkdir(parents=True)
        for name, source in (("__init__", ""), ("registry", FAKE_REGISTRY), ("process_registry", ""),
                             ("terminal_tool", FAKE_TERMINAL_TOOL)):
            (tools / f"{name}.py").write_text(source)
        self.register({"terminal": "terminal", "process_manage": "terminal", "read_file": "file"})
        python = self.root / "tools" / "python-9" / "bin"
        python.mkdir(parents=True)
        (python / "python3").symlink_to(sys.executable)
        (self.root / "tools" / "facts.json").write_text(json.dumps({"packages": {"python": {"entry": "python-9"}}}))
        site = self.root / "installs" / "key" / "environments" / "env" / "venv" / "lib" / "python9.9" / "site-packages"
        site.mkdir(parents=True)
        (site / "fake_dependency.py").write_text("")
        (self.root / "installs" / "key" / "facts.json").write_text(
            json.dumps({"packages": {"venv": {"environment": str(site.parents[2])}}}))
        self.plugin = self.tmp / "worker" / "plugins" / "blocker-order" / "__init__.py"
        self.plugin.parent.mkdir(parents=True)
        shutil.copy(PLUGIN, self.plugin)

    def register(self, tools: dict) -> None:
        (self.tree / "tools" / "registered.json").write_text(json.dumps(tools))

    def check(self) -> list:
        return [str(d) for d in check_install.check_hermes_tools(self.root, self.plugin)]

    def test_a_hermes_that_registers_the_watched_tools_is_silent(self) -> None:
        self.assertEqual(self.check(), [])

    def test_the_legacy_process_name_alone_still_counts(self) -> None:
        self.register({"terminal": "terminal", "process": "terminal"})

        self.assertEqual(self.check(), [])

    def test_a_renamed_process_tool_names_the_missing_tool(self) -> None:
        self.register({"terminal": "terminal", "process_run": "terminal", "read_file": "file"})

        self.assertEqual(self.check(), [
            "hermes tool blocker-order: Hermes registers none of process_manage, process, which the plugin watches; "
            "its terminal toolset registers process_run, terminal (PROCESS_TOOLS in plugins/blocker-order/__init__.py)",
        ])

    def test_a_missing_terminal_tool_is_named(self) -> None:
        self.register({"process_manage": "terminal"})

        self.assertEqual(self.check(), [
            "hermes tool blocker-order: Hermes registers no terminal, which the plugin watches; "
            "its terminal toolset registers process_manage (REQUIRED_TOOLS in plugins/blocker-order/__init__.py)",
        ])

    def test_the_probe_runs_in_a_scratch_home_and_writes_no_bytecode(self) -> None:
        self.check()

        seen = json.loads((self.tree / "tools" / "seen.json").read_text())
        self.assertNotIn(seen["hermes_home"], {str(self.root), str(self.tmp / "worker")})
        self.assertFalse(Path(seen["hermes_home"]).exists())
        self.assertEqual(seen["no_bytecode"], "1")
        self.assertEqual(list(self.tree.rglob("__pycache__")), [])
        self.assertEqual(list((self.tmp / "worker").rglob("__pycache__")), [])

    def test_a_hermes_that_cannot_be_imported_gives_a_row(self) -> None:
        (self.tree / "tools" / "terminal_tool.py").write_text("raise ImportError('no module named fake')\n")

        [row] = self.check()
        self.assertTrue(row.startswith("hermes tool blocker-order: cannot read Hermes' tool names: "), row)
        self.assertIn("no module named fake", row)

    def test_a_missing_interpreter_or_environment_gives_a_row(self) -> None:
        (self.root / "tools" / "facts.json").unlink()
        self.assertEqual(self.check(), [
            f"hermes tool blocker-order: cannot read Hermes' tool names: no Hermes Python in {self.root / 'tools' / 'facts.json'}",
        ])
        (self.root / "tools" / "facts.json").write_text(json.dumps({"packages": {"python": {"entry": "python-9"}}}))
        (self.root / "installs" / "key" / "facts.json").unlink()
        [row] = self.check()
        self.assertTrue(row.startswith("hermes tool blocker-order: cannot read Hermes' tool names: no Hermes environment"), row)

    def test_a_missing_checkout_gives_a_row(self) -> None:
        shutil.rmtree(self.tree)

        [row] = self.check()
        self.assertTrue(row.startswith("hermes tool blocker-order: cannot read Hermes' tool names: no Hermes checkout"), row)

    def test_without_the_installed_plugin_nothing_runs(self) -> None:
        self.plugin.unlink()

        self.assertEqual(self.check(), [])


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
        self.launchd.states({
            "system/com.example.cloudflared": "running", f"gui/{uid}/ai.hermes.gateway": "running",
            f"gui/{uid}/ai.patinaproject.factory.gateway-watch": "not running",
        })

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
            "service gateway watch: is not loaded in launchd (part 12)",
        ])

    def test_a_missing_watch_job_is_reported(self) -> None:
        self.launchd.watch.unlink()

        self.assertEqual(self.check(), [
            "service gateway watch: no plist matches ~/Library/LaunchAgents/ai.patinaproject.factory.gateway-watch.plist (part 12)",
        ])

    def watch_with(self, *lock: str, root: tuple = ("--root", "/h")) -> None:
        self.launchd.plist(
            f"{str(self.launchd.home).lstrip('/')}/Library/LaunchAgents/ai.patinaproject.factory.gateway-watch.plist",
            "ai.patinaproject.factory.gateway-watch",
            ProgramArguments=["/usr/bin/python3", "/h/scripts/watch-gateway", *root, "--token", "s3cr3t", *lock],
        )
        uid = os.getuid()
        self.launchd.states({
            "system/com.example.cloudflared": "running", f"gui/{uid}/ai.hermes.gateway": "running",
            f"gui/{uid}/ai.patinaproject.factory.gateway-watch": "not running",
        })

    def test_a_watcher_whose_lock_is_hermes_own_gateway_lock_is_reported(self) -> None:
        self.watch_with("--lock", "/h/gateway.lock")

        self.assertEqual(self.check(), [
            "service gateway watch: its --lock is Hermes' own /h/gateway.lock, which the running gateway always holds; "
            "name a factory-owned file or drop --lock (part 12)",
        ])

    def assert_hermes_lock_reported(self, lock: str) -> None:
        rows = self.check()
        self.assertEqual(len(rows), 1, (lock, rows))
        self.assertIn("is Hermes' own", rows[0])
        self.assertTrue(rows[0].startswith("service gateway watch: its --lock is"), rows[0])

    def test_a_hermes_lock_named_with_an_equals_sign_is_reported(self) -> None:
        self.watch_with("--lock=/h/gateway.lock")

        self.assert_hermes_lock_reported("--lock=/h/gateway.lock")

    def test_a_hermes_lock_spelled_with_dot_dot_is_reported(self) -> None:
        self.watch_with("--lock", "/h/sub/../gateway.lock")

        self.assert_hermes_lock_reported("/h/sub/../gateway.lock")

    def test_a_hermes_lock_reached_through_a_symlink_is_reported(self) -> None:
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        (home / "real").mkdir()
        (home / "link").symlink_to(home / "real")
        self.watch_with("--lock", str(home / "link" / "gateway.lock"), root=("--root", str(home / "real")))

        self.assert_hermes_lock_reported("symlink")

    def test_a_hermes_lock_under_the_default_root_is_reported(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/h"}):
            self.watch_with("--lock", "/h/gateway.lock", root=())

            self.assert_hermes_lock_reported("default root")

    def test_a_factory_lock_under_the_default_root_is_silent(self) -> None:
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/h"}):
            self.watch_with("--lock", "/h/factory-gateway.lock", root=())

            self.assertEqual(self.check(), [])

    def test_a_watcher_with_a_factory_owned_lock_or_none_is_silent(self) -> None:
        for lock in (("--lock", "/h/factory-gateway.lock"), ("--lock", "/store/locks/gateway.lock"), ()):
            self.watch_with(*lock)

            self.assertEqual(self.check(), [], lock)

    def test_program_arguments_are_never_printed(self) -> None:
        self.launchd.states({})

        self.assertNotIn("s3cr3t", "\n".join(self.check()))


if __name__ == "__main__":
    unittest.main()
