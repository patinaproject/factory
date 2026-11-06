from __future__ import annotations

import ast
import contextlib
import importlib.machinery
import importlib.util
import atexit
import http.client
import io
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.support import REPO, SCRIPTS, FakeBroker, FakeXcode, git, with_interpreter

ROUTE_KEY = "platforms.webhook.extra.routes.linear"
RATE_LIMIT_KEY = "platforms.webhook.extra.rate_limit"
LINEAR_ID = "00000000-0000-4000-8000-0000000000c1"
ROUTE = {
    "events": ["AgentSessionEvent"],
    "secret": "${LINEAR_WEBHOOK_SECRET}",
    "prompt": "triage",
    "filters": [{"field": "data.id", "in_file": "owned-refs.json"}],
    "coalesce": {"key": "factoryRoute.issue", "window_seconds": 30},
}
# What `hermes config get` prints for ROUTE: Hermes masks every value under a credential-shaped name.
INSTALLED_ROUTE = {**ROUTE, "secret": "abc...1234", "coalesce": {"key": "fact...ssue", "window_seconds": 30}}

SKILL_NAME = "factory-linear-triage"
SKILL_TEXT = "---\nname: factory-linear-triage\n---\n\nrules\n"
RENDER = f"""import json, sys
if "--skills-json" in sys.argv:
    print(json.dumps({{{SKILL_NAME!r}: {SKILL_TEXT!r}}}))
else:
    print(json.dumps({{{RATE_LIMIT_KEY!r}: 600, {ROUTE_KEY!r}: {ROUTE!r}}}))
"""

FAKE_HERMES = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:4] == ["kanban", "boards", "list"]:
    boards = os.path.join(os.environ["HERMES_HOME"], "installed-boards.json")
    print(open(boards).read() if os.path.exists(boards) else "[]")
    sys.exit()
if sys.argv[1:3] == ["plugins", "list"]:
    listed = os.path.join(os.environ["HERMES_HOME"], "installed-plugins.json")
    if os.path.exists(listed):
        print(open(listed).read())
    else:
        plugins = os.path.join(os.environ["HERMES_HOME"], "plugins")
        print(json.dumps([{"name": n, "status": "enabled"} for n in sorted(os.listdir(plugins))] if os.path.isdir(plugins) else []))
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

WORK_SKILLS = Path(tempfile.mkdtemp()).resolve()
atexit.register(shutil.rmtree, WORK_SKILLS, ignore_errors=True)
(WORK_SKILLS / "example-skill").mkdir()
(WORK_SKILLS / "example-skill" / "SKILL.md").write_text("---\nname: example-skill\n---\n")
WORK_KEY = "plugins.entries.work.settings.skills_dir"
WORK_ALLOW_KEY = "plugins.entries.work.settings.allow"

ARTIFACT_ORG = "11111111-2222-3333-4444-555555555555"
ARTIFACT_TOKEN = "team-token-value"
# Prints the system/init event that real claude prints before it calls the model, then waits to be killed.
FAKE_INIT_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
options = json.load(open(__file__ + ".json"))
with open(options["log"], "a") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}) + "\\n")
print(json.dumps({"type": "system", "subtype": "init", "tools": options["tools"]}), flush=True)
time.sleep(60)
"""
ARTIFACT_FIXTURE = Path(tempfile.mkdtemp(prefix="check-install-artifact-")).resolve()
atexit.register(shutil.rmtree, ARTIFACT_FIXTURE, True)
ARTIFACT_AUTH = ARTIFACT_FIXTURE / "auth"
ARTIFACT_CLAUDE = ARTIFACT_FIXTURE / "claude"
ARTIFACT_LOG = ARTIFACT_FIXTURE / "claude.log"
ARTIFACT_SETTINGS = {"auth_dir": str(ARTIFACT_AUTH), "organization_uuid": ARTIFACT_ORG, "claude": str(ARTIFACT_CLAUDE)}


def artifact_login(organization: str = ARTIFACT_ORG, disabled: bool = False) -> None:
    ARTIFACT_AUTH.mkdir(exist_ok=True)
    for path in ARTIFACT_AUTH.iterdir():
        path.unlink()
    (ARTIFACT_AUTH / "team.json").write_text(json.dumps({
        "access_token": ARTIFACT_TOKEN, "refresh_token": "refresh-value", "expired": "2099-01-01T00:00:00-07:00",
        "organization_uuid": organization, "email": "agent@example.com", "disabled": disabled,
    }))


def artifact_tools(*tools: str) -> None:
    ARTIFACT_CLAUDE.write_text(with_interpreter(FAKE_INIT_CLAUDE))
    ARTIFACT_CLAUDE.chmod(0o755)
    (ARTIFACT_FIXTURE / "claude.json").write_text(json.dumps({"log": str(ARTIFACT_LOG), "tools": list(tools)}))


artifact_login()
artifact_tools("Artifact", "Read")

DEPENDENCY_UPDATES = {"repository": "example-org/example-repo", "team_key": "ABC", "theme_label": "Security", "type_label": "Tools"}
SETTINGS = {
    "operator_user_ids": ["user-operator-1"], "app_user_id": "u1", "repositories": [], "triage": {"max_running": 2}, "claude_artifact": ARTIFACT_SETTINGS,
    "dependency_updates": DEPENDENCY_UPDATES,
}
SENTRY_SYNC = {
    "organization": "example-org", "team_key": "ABC", "repository": "example-org/example-repo",
    "checkout_path": "/srv/example-repo", "rules_path": "config/error-groups.json", "sentry_label": "Sentry",
    "theme_label": "Stability", "mcp_server": "sentry",
}
DEPENDABOT_SWEEP = {"team_key": "ABC", "label": "Security"}
SENTRY_SERVERS = {"sentry": {"url": "https://mcp.example/sentry", "token_env": "SENTRY_ACCESS_TOKEN"}}
DEPENDENCY_UPDATES_KEY = "plugins.entries.linear-agent-session.settings.dependency_updates"
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
    DEPENDENCY_UPDATES_KEY: DEPENDENCY_UPDATES,
    "kanban.max_in_progress": 4,
    "kanban.max_spawn": 2,
    "kanban.review_dispatch": False,
    "kanban.auto_decompose": False,
    "streaming.enabled": True,
    "platforms.slack.extra.native_task_cards": True,
    "platforms.factory_uploads.enabled": True,
    "platforms.factory_uploads.extra.repositories": ["example-org/example-repo"],
    WORK_KEY: str(WORK_SKILLS),
    "gateway.multiplex_profiles": True,
    "gateway.profile_routes": [{"platform": "slack", "profile": "slack"}],
}
WORKER_CONFIG = {
    **{key: ROOT_CONFIG[key] for key in (
        "model.provider", "model.default", "plugins.entries.linear-agent-session.settings", MAX_RUNNING_KEY,
        DEPENDENCY_UPDATES_KEY,
    )},
    "terminal.timeout": 400,
}


@contextlib.contextmanager
def models_server(models: list):
    """An Anthropic-compatible endpoint on 127.0.0.1 whose /v1/models lists `models`. Yields its base URL."""
    body = json.dumps({"data": [{"id": model} for model in models]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path.startswith("/v1/models") else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def cron_job(name: str, minutes: int, deliver: str = "local", **fields) -> dict:
    return {
        "id": name, "name": name, "script": name, "no_agent": True, "enabled": True, "deliver": deliver,
        "schedule": {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"},
        "schedule_display": f"every {minutes}m", "prompt": "", **fields,
    }


WORKER_JOBS = [
    cron_job("refresh-checkouts", 60),
    cron_job("refresh-owned-refs", 1),
    cron_job("reopen-conflicts", 5),
    cron_job("finish-merged", 5),
    cron_job("dispatch-triage", 1),
    cron_job("file-dependency-update", 10080),
    cron_job("file-recurring-issues", 60),
]
RETRY_SCRIPT = "retry-failed-webhooks"
ROOT_JOBS = [cron_job(RETRY_SCRIPT, 10, "slack"), cron_job("report-deploy", 5, "slack")]
RETRY_SOURCE = f"skills/setup-factory/scripts/{RETRY_SCRIPT}"
ROUTE_SCRIPT = "linear-route"
ROUTE_SCRIPT_SOURCE = f"skills/setup-factory/scripts/{ROUTE_SCRIPT}"
SCHEDULED_SOURCE = "skills/setup-factory/scripts/scheduled-command"
SCHEDULED_ENTRY = {
    "name": "example-job", "repository": "example-org/example-repo", "schedule": "0 9 * * 1",
    "commands": [["pnpm", "install", "--frozen-lockfile"], ["pnpm", "run", "example"]], "timeout_minutes": 30,
}
SCHEDULED_KEY = "plugins.entries.linear-agent-session.settings.scheduled_commands"
SCHEDULED_JOB = "scheduled-example-job"


def cron_expression_job(name: str, expression: str, deliver: str = "slack", **fields) -> dict:
    return cron_job(name, 0, deliver, schedule={"kind": "cron", "expr": expression, "display": expression},
                    schedule_display=expression, **fields)

SCRIPT = SCRIPTS / "check-install"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
loader = importlib.machinery.SourceFileLoader("check_install", str(SCRIPT))
check_install = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(check_install)

REMEDY_CLASSES = (
    ("profile", "", "install"),
    ("plugin", "", "install"),
    ("plugin status", "cannot run hermes plugins list", "advisory"),
    ("plugin status", "", "install"),
    ("plugin import", "cannot import the plugins", "advisory"),
    ("plugin import", "", "manual"),
    ("config", r"^config (platforms\.webhook\.(enabled|extra\.host)|kanban\.(review_dispatch|auto_decompose|default_assignee)"
     r"|terminal\.(timeout|env_passthrough)|platforms\.factory_uploads\.enabled|streaming\.enabled"
     r"|platforms\.slack\.extra\.native_task_cards|cron\.catch_up_missed|\S+\.github\.private_key_path) in ", "install"),
    ("config", "", "manual"),
    ("cron job", r"^cron job (retry-failed-webhooks|report-deploy|scheduled-\S+) in .*: (is missing|delivers to \S+, not )", "manual"),
    ("cron job", r": (is missing|is disabled|runs .*, not |is not a script-only job|delivers to \S+, not |belongs to"
     r"|runs without a \S+ section)", "install"),
    ("cron job", ": last run", "advisory"),
    ("cron job", ": is replaced by a launchd job", "manual"),
    ("cron job", "", "manual"),
    ("script", "", "install"),
    ("service", "", "manual"),
    ("gateway watch", "gateway-heartbeat-url: ", "manual"),
    ("gateway watch", "", "advisory"),
    ("auto deploy", "auto-deploy.lock: ", "advisory"),
    ("auto deploy", "", "manual"),
    ("upload broker", "", "advisory"),
    ("broker user", "", "manual"),
    ("secret", "", "manual"),
    ("dashboard sign-in", "", "manual"),
    ("route skill", "", "install"),
    ("webhook config", "render.py failed", "manual"),
    ("webhook config", "", "install"),
    ("webhook deliveries", "", "advisory"),
    ("webhook route", "", "advisory"),
    ("owned refs", "", "install"),
    ("kanban board", "", "install"),
    ("kanban board database", "cannot be read", "advisory"),
    ("kanban board database", "", "manual"),
    ("kanban capacity", "", "manual"),
    ("stranded triage card", "", "advisory"),
    ("dependabot sweep", "", "advisory"),
    ("stranded triage card check", "", "advisory"),
    ("gh login", "", "manual"),
    ("artifact login", "", "manual"),
    ("artifact tool", "", "manual"),
    ("guarded card", "", "manual"),
    ("guarded card check", "", "advisory"),
    ("waiting triage card", "", "manual"),
    ("waiting triage card check", "", "advisory"),
    ("triage playbook", "", "manual"),
    ("source checkout", "", "install"),
    ("checkout", "does not exist", "manual"),
    ("checkout", "", "install"),
    ("work skills", "", "manual"),
    ("model tier", "", "manual"),
    ("mcp", "", "advisory"),
    ("root mcp", "cannot check|does not connect", "advisory"),
    ("root mcp", "", "manual"),
    ("slack profile", "cannot check|cannot be checked", "advisory"),
    ("slack profile", "", "manual"),
    ("hermes tool", "cannot read", "advisory"),
    ("hermes tool", "", "manual"),
    ("prompt command", "", "manual"),
    ("email", "", "manual"),
    ("pdf extractor", "cannot check", "advisory"),
    ("pdf extractor", "", "manual"),
    ("github app", "", "manual"),
    ("app auto-update", "", "manual"),
    ("macos auto-update", "", "manual"),
    ("xcode first launch", "", "manual"),
    ("xcode first launch timeout", "", "manual"),
)


def remedy_class(record: dict) -> str | None:
    for kind, pattern, remedy in REMEDY_CLASSES:
        if record["kind"] == kind and re.search(pattern, record["line"]):
            return remedy
    return None


def classified(case: unittest.TestCase, drifts) -> list:
    records = [d if isinstance(d, dict) else d.to_json() for d in drifts]
    for record in records:
        case.assertEqual(record["remedy"]["type"], remedy_class(record), record["line"])
    return [record["line"] for record in records]

SECRET_NAMES = "plugin.yaml"
SECRET_NAMES_MANIFEST = "kind: platform\noptional_env:\n  - name: LINEAR_CLIENT_SECRET\n  - name: CLAUDE_PROXY_TOKEN\n  - name: SENTRY_ACCESS_TOKEN\n"
TRIAGE_PLAYBOOK = ".agents/skills/patina-mode/playbooks/triage.md"
REPO_FILES = {
    f"plugins/factory-secret-names/{SECRET_NAMES}": SECRET_NAMES_MANIFEST,
    TRIAGE_PLAYBOOK: "playbook\n",
    "profiles/claude-worker/SOUL.md": "soul\n",
    "profiles/claude-worker/config.yaml": "model: repo\n",
    "profiles/claude-worker/scripts/x": "#!/bin/sh\n",
    "plugins/p1/__init__.py": "# plugin\n",
    "plugins/p1/tests/test_a.py": "test\n",
    "plugins/factory-uploads/__init__.py": "# uploads\n",
    "plugins/work/__init__.py": "# work\n",
    "plugins/webhook-retry/__init__.py": "# retry\n",
    "templates/render.py": RENDER,
    RETRY_SOURCE: "#!/usr/bin/env python3\nretry\n",
    ROUTE_SCRIPT_SOURCE: "#!/usr/bin/env python3\nroute\n",
    SCHEDULED_SOURCE: (REPO / SCHEDULED_SOURCE).read_text(),
}


STATUS = {
    "ok": True, "token_present": True, "linear_ready": True, "proxy_token_present": True, "github_app_ready": True,
    "last_state": None,
}


class CheckInstallTest(unittest.TestCase):
    def broker(self) -> FakeBroker:
        broker = FakeBroker(self.root / "factory-uploads.sock", status=[STATUS])
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
            self.install(home, "plugins/p1/__init__.py", "# plugin\n")
            self.install(home, f"plugins/factory-secret-names/{SECRET_NAMES}", SECRET_NAMES_MANIFEST)
        self.install(self.root, "plugins/factory-uploads/__init__.py", "# uploads\n")
        (self.root / "plugins" / "linear-agent-session").mkdir(parents=True)
        (self.root / "plugins" / "linear-agent-session" / "broker_client.py").symlink_to(
            REPO / "plugins" / "linear-agent-session" / "broker_client.py"
        )
        self.install(self.root, "plugins/work/__init__.py", "# work\n")
        self.install(self.root, "plugins/webhook-retry/__init__.py", "# retry\n")
        self.install(self.worker, "SOUL.md", "soul\n")
        self.expiry(time.time() + 3600)
        self.install(self.worker, "scripts/x", "#!/bin/sh\n")
        self.install(self.worker, "config.yaml", "model: local\n")
        self.install(
            self.worker, "distribution.yaml", f"name: claude-worker\nsource: {self.repo}/profiles/claude-worker\n"
        )
        self.install(self.root, f"installed-config/{ROUTE_KEY}.json", json.dumps(INSTALLED_ROUTE))
        self.install(self.root, f"installed-config/{RATE_LIMIT_KEY}.json", "600")
        self.install(self.root, f"skills/{SKILL_NAME}/SKILL.md", SKILL_TEXT)
        self.install(self.root, "logs/agent.log", "")
        self.install(self.root, f"scripts/{RETRY_SCRIPT}", REPO_FILES[RETRY_SOURCE])
        self.install(self.root, f"scripts/{ROUTE_SCRIPT}", REPO_FILES[ROUTE_SCRIPT_SOURCE])
        self.install(self.root, ".env", "SLACK_HOME_CHANNEL=C0123\n")
        self.owned_refs = self.root / "owned-refs.json"
        self.install(self.root, "owned-refs.json", f'["goodyear/abc-1-fix", "{LINEAR_ID}"]\n')
        self.install(self.root, "triage-refs.json", "[]\n")
        self.watch_state({"checked_at": time.time(), "state": "healthy", "restarts": []})
        self.install(self.root, "gateway-heartbeat-url", "https://monitor.example/ping/id\n")
        self.install(self.root, "auto-deploy.json", json.dumps({"deployed": git(self.repo, "rev-parse", "HEAD"), "hold": None}))
        os.chmod(self.root / "gateway-heartbeat-url", 0o600)
        self.boards(BOARDS)

        self.issuer = self.tmp / "issuer"
        self.discovery(["authorization_code_with_pkce", "refresh_tokens"])
        self.dashboard(scopes="openid profile email offline_access")

        install_fake_hermes(self.root)
        self.hermes = self.tmp / "bin" / "fake-hermes"
        self.install(self.tmp, "bin/fake-hermes", with_interpreter(FAKE_HERMES))
        os.chmod(self.hermes, 0o755)

        self.config(self.root, ROOT_CONFIG)
        self.config(self.worker, WORKER_CONFIG)
        self.slack = self.root / "profiles" / "slack"
        self.install(self.slack, "config.yaml", '{"model": {"default": "opus"}}')
        self.config(self.slack, {"model.provider": ROOT_CONFIG["model.provider"], "model.default": "opus"})
        self.cron(self.worker, WORKER_JOBS)
        self.cron(self.root, ROOT_JOBS)
        self.launchd = Launchd(self.tmp)
        self.host = HostUpdates(self.tmp)
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

    def expiry(self, expires_at: float | str | None) -> None:
        path = self.worker / "gh" / "token-expiry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        if expires_at is not None:
            path.write_text(expires_at if isinstance(expires_at, str) else json.dumps({"expires_at": expires_at}))

    def run_check(self, *args: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ, **self.launchd.env, **self.host.env, "HERMES_HOME": str(self.worker), "HERMES_BIN": str(self.hermes),
        }
        return subprocess.run([sys.executable, str(SCRIPTS / "check-install"), *args], env=env, capture_output=True, text=True)

    def check(self, *args: str) -> tuple:
        if "--print-source" in args:
            proc = self.run_check(*args)
            return proc.returncode, proc.stdout
        proc = self.run_check("--json", *args)
        self.raw = proc.stdout
        if not proc.stdout:
            return proc.returncode, ""
        lines = classified(self, json.loads(proc.stdout))
        return proc.returncode, "".join(f"{line}\n" for line in lines)

    def test_matching_install_is_silent(self) -> None:
        self.assertEqual(self.check(), (0, ""))
        self.assertEqual(json.loads(self.raw), [])

    def test_an_app_without_the_push_event_adds_one_manual_line_and_nothing_else(self) -> None:
        settings = {**SETTINGS, "github": {"login": "example-app[bot]"}}
        for home, config in ((self.root, ROOT_CONFIG), (self.worker, WORKER_CONFIG)):
            self.config(home, {**config, "plugins.entries.linear-agent-session.settings": settings})
        app = self.tmp / "app.json"
        self.install(self.tmp, "bin/gh", with_interpreter(
            "#!/usr/bin/env python3\nimport sys\nassert sys.argv[1:] == ['api', 'apps/example-app']\n"
            f"print(open({str(app)!r}).read())\n"
        ))
        os.chmod(self.tmp / "bin" / "gh", 0o755)
        app.write_text(json.dumps({"events": ["issues", "pull_request"], "permissions": {"contents": "read"}}))
        env = {"GH_BIN": str(self.tmp / "bin" / "gh")}

        with mock.patch.dict(os.environ, env):
            code, out = self.check()
        records = json.loads(self.raw)
        app.write_text(json.dumps({"events": ["issues", "push"], "permissions": {}}))
        with mock.patch.dict(os.environ, env):
            subscribed = self.check()

        self.assertEqual((code, out), (1, 'github app example-app: does not subscribe to the "Push" event, so no merge '
                                          "starts auto-deploy until its next interval run\n"))
        self.assertEqual([record["remedy"] for record in records], [{
            "type": "manual", "action": 'in the App\'s GitHub settings, under Permissions & events, subscribe to "Push"',
            "gates_runs": False,
        }])
        self.assertEqual(subscribed, (0, ""))

    def test_the_text_output_is_the_lines_of_the_json_records(self) -> None:
        self.install(self.worker, "SOUL.md", "stale\n")
        shutil.rmtree(self.root / "plugins" / "p1")
        self.config(self.root, {**ROOT_CONFIG, "kanban.review_dispatch": True, "kanban.max_spawn": 0})
        self.cron(self.worker, [job for job in WORKER_JOBS if job["name"] != "dispatch-triage"])
        os.chmod(self.root / "gateway-heartbeat-url", 0o644)
        self.boards([BOARDS[0]])

        text, records = self.run_check(), self.run_check("--json")

        self.assertEqual((text.returncode, records.returncode), (1, 1))
        self.assertEqual(text.stdout, "".join(f"{record['line']}\n" for record in json.loads(records.stdout)))
        self.assertEqual(len(text.stdout.splitlines()), 7)

    def test_each_record_names_its_remedy(self) -> None:
        self.install(self.worker, "SOUL.md", "stale\n")
        self.config(self.root, {**ROOT_CONFIG, "kanban.review_dispatch": True})
        self.cron(self.worker, [job for job in WORKER_JOBS if job["name"] != "dispatch-triage"])
        os.chmod(self.root / "gateway-heartbeat-url", 0o644)

        self.check()

        hermes, beat = str(self.hermes), self.root / "gateway-heartbeat-url"
        self.assertEqual(json.loads(self.raw), [
            {
                "kind": "profile", "where": "claude-worker", "detail": f"SOUL.md differs from main at {self.sha}",
                "line": f"profile claude-worker: SOUL.md differs from main at {self.sha}",
                "remedy": {
                    "type": "install", "step": "PROFILE", "argv": [hermes, "profile", "update", "claude-worker", "-y"],
                    "restarts_gateway": False,
                },
            },
            {
                "kind": "config", "where": f"kanban.review_dispatch in {self.root}", "detail": "is not false (part 10)",
                "line": f"config kanban.review_dispatch in {self.root}: is not false (part 10)",
                "remedy": {
                    "type": "install", "step": "CONFIG", "argv": [hermes, "config", "set", "kanban.review_dispatch", "false"],
                    "restarts_gateway": True,
                },
            },
            {
                "kind": "cron job", "where": f"dispatch-triage in {self.worker}", "detail": "is missing (part 12)",
                "line": f"cron job dispatch-triage in {self.worker}: is missing (part 12)",
                "remedy": {
                    "type": "install", "step": "CRON",
                    "argv": [
                        hermes, "-p", "claude-worker", "cron", "create", "every 1m", "--name", "dispatch-triage",
                        "--no-agent", "--script", "dispatch-triage", "--deliver", "local",
                    ],
                    "restarts_gateway": False,
                },
            },
            {
                "kind": "gateway watch", "where": str(beat), "detail": "has mode 644; it holds a URL, so use 600 (part 12)",
                "line": f"gateway watch {beat}: has mode 644; it holds a URL, so use 600 (part 12)",
                "remedy": {
                    "type": "manual",
                    "action": "ask the operator to store the heartbeat URL, mode 600, as part 12's checkpoint describes",
                    "gates_runs": False,
                },
            },
        ])

    def test_a_missing_login_job_gates_runs_and_a_stale_expiry_does_not(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("the gh login refresh job is a launchd job")
        self.launchd.gh_login.unlink()
        self.expiry(time.time() + 60)

        self.check()

        records = {record["kind"]: record["remedy"] for record in json.loads(self.raw)}
        self.assertEqual(set(records), {"service"})
        self.assertTrue(records["service"]["gates_runs"])

        self.launchd.plist(
            f"{str(self.launchd.home).lstrip('/')}/Library/LaunchAgents/{GH_LOGIN_LABEL}.plist", GH_LOGIN_LABEL, **GH_LOGIN_FIELDS,
        )
        for expiry in (None, "{not json", time.time() + 60, time.time() - 60):
            with self.subTest(expiry=expiry):
                self.expiry(expiry)
                self.check()
                [record] = json.loads(self.raw)
                self.assertEqual(record["kind"], "gh login")
                self.assertEqual(record["where"], str(self.worker / "gh"))
                self.assertEqual(record["remedy"]["type"], "manual")
                self.assertFalse(record["remedy"]["gates_runs"])
        self.expiry(time.time() + 3600)
        self.assertEqual(self.check(), (0, ""))

    def test_a_differing_cron_job_is_edited_in_place(self) -> None:
        self.cron(self.worker, [
            cron_job("dispatch-triage", 5, enabled=False) if job["name"] == "dispatch-triage" else job for job in WORKER_JOBS
        ])

        self.check()

        cron = [str(self.hermes), "-p", "claude-worker", "cron"]
        self.assertEqual([record["remedy"]["argv"] for record in json.loads(self.raw)], [
            [*cron, "resume", "dispatch-triage"],
            [*cron, "edit", "dispatch-triage", "--schedule", "every 1m"],
        ])

    def test_a_render_that_fails_needs_the_operator(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "broken-render")
        self.install(self.repo, "templates/render.py", "import sys\nsys.exit('no factory settings')\n")
        git(self.repo, "commit", "-q", "-am", "break render")
        sha = git(self.repo, "rev-parse", "--short=7", "HEAD")

        self.assertEqual(self.check("--ref", "broken-render", "--no-fetch"), (1, (
            f"webhook config {self.root}: render.py failed at {sha}: no factory settings\n"
        )))
        self.assertEqual(json.loads(self.raw)[0]["remedy"]["type"], "manual")

    def test_render_reads_the_worker_marker_from_the_commit(self) -> None:
        git(self.repo, "checkout", "-q", "-b", "marker-render")
        self.install(self.repo, "profiles/claude-worker/scripts/reply_inbox.py", "MARKER = 'x'\n")
        reads_marker = "from pathlib import Path\nPath(__file__).parent.parent.joinpath('profiles/claude-worker/scripts/reply_inbox.py').read_text()\n"
        self.install(self.repo, "templates/render.py", reads_marker + RENDER)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "render reads the marker")

        _, out = self.check("--ref", "marker-render", "--no-fetch")

        self.assertNotIn("render.py failed", out)

    def test_every_drift_kind_check_install_builds_has_a_remedy_class(self) -> None:
        tree = ast.parse(SCRIPT.read_text())
        kinds = {
            node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Drift"
        }

        self.assertEqual(kinds - {kind for kind, _, _ in REMEDY_CLASSES}, set())
        self.assertIn("source checkout", kinds)

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
        self.assertNotIn("monitor.example", self.raw)

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

    def test_host_updates_are_checked_on_macos(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("the update settings are macOS preferences")
        self.host.set("Preferences", COMMERCE, AutoUpdate=True)
        self.host.set("Preferences", SOFTWARE_UPDATE, **{MACOS_KEY: True})
        self.host.xcode.answer(status=1)

        self.assertEqual(self.check(), (1, APP_STORE_LINE + MACOS_LINE + first_launch_line(self.host.xcode.developer)))

    def test_no_host_update_check_runs_off_macos(self) -> None:
        self.host.set("Preferences", COMMERCE, AutoUpdate=True)
        self.host.xcode.answer(status=1)
        env = {
            **os.environ, **self.launchd.env, **self.host.env, "HERMES_HOME": str(self.worker), "HERMES_BIN": str(self.hermes),
        }
        code = (
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "sys.platform = 'linux'\n"
            f"sys.argv = ['check-install', '--no-fetch']\n"
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
        )

        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)

        self.assertNotRegex(proc.stdout, "auto-update|first launch")
        self.assertEqual(self.host.xcode.called(), [])

    def test_auto_deploy_is_checked_on_macos(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("auto-deploy is a launchd job")
        state = self.root / "auto-deploy.json"
        deployed = state.read_text()
        state.unlink()
        missing = self.check()
        state.write_text(deployed)
        self.config(self.root, {**ROOT_CONFIG, "kanban.dispatch_profiles": []})
        held = self.check()

        self.assertEqual((missing, held), (
            (1, f"auto deploy {state}: is missing, so auto-deploy has never run (part 12)\n"),
            (1, f"auto deploy kanban.dispatch_profiles in {self.root}: is set, and no auto-deploy run set it, so Hermes "
                "claims no card for a profile outside it and auto-deploy deploys nothing\n"),
        ))

    def test_the_auto_deploy_wrappers_are_compared_with_the_repository(self) -> None:
        for name in ("auto-deploy", "report-deploy"):
            self.install(self.repo, f"skills/setup-factory/scripts/{name}", f"#!/usr/bin/env python3\n{name}\n")
        git(self.repo, "checkout", "-q", "-b", "deploy")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "deploy")
        self.install(self.root, "scripts/report-deploy", "stale\n")
        sha = git(self.repo, "rev-parse", "--short=7", "HEAD")

        self.assertEqual(self.check("--ref", "deploy", "--no-fetch"), (1, (
            f"script {self.root}/scripts/auto-deploy: is missing; install it from "
            "skills/setup-factory/scripts/auto-deploy (part 12)\n"
            f"script {self.root}/scripts/report-deploy: differs from deploy at {sha} (part 12)\n"
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
        launchd_name = Path(next(s.plist for s in check_install.SERVICES if s.name == "gateway watch")).name
        self.assertEqual(launchd_name, f"{watch.LABEL}.plist")

    def test_plugin_difference_names_the_home(self) -> None:
        self.install(self.worker, "plugins/p1/__init__.py", "# old\n")

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
        self.install(self.root, "plugins/factory-uploads/__init__.py", "# old\n")
        self.assertEqual(
            self.check(),
            (1, f"plugin factory-uploads in {self.root}: __init__.py differs from main at {self.sha}\n"),
        )
        shutil.rmtree(self.root / "plugins" / "factory-uploads")
        self.assertEqual(self.check(), (1, f"plugin factory-uploads in {self.root}: not installed\n"))

    def test_the_work_plugin_is_root_only(self) -> None:
        shutil.rmtree(self.root / "plugins" / "work")
        self.assertEqual(self.check(), (1, f"plugin work in {self.root}: not installed\n"))

        self.install(self.root, "plugins/work/__init__.py", "# work\n")
        self.install(self.worker, "plugins/work/__init__.py", "# work\n")
        self.assertEqual(
            self.check(), (1, f"plugin work in {self.worker}: installed, but it runs only in the root home\n")
        )

    def test_a_root_only_plugin_installed_in_the_worker_home_is_reported(self) -> None:
        self.install(self.worker, "plugins/factory-uploads/__init__.py", "# uploads\n")

        self.assertEqual(
            self.check(),
            (1, f"plugin factory-uploads in {self.worker}: installed, but it runs only in the root home\n"),
        )

    def commit_plugins(self, *names: str, mail_profile: bool = False) -> str:
        for name in names:
            self.install(self.repo, f"plugins/{name}/__init__.py", f"# {name}\n")
        if mail_profile:
            self.install(self.repo, "profiles/mail-worker/SOUL.md", "# mail-worker\n")
        git(self.repo, "checkout", "-q", "-b", "mail")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "mail")
        return git(self.repo, "rev-parse", "--short=7", "HEAD")

    def test_the_mail_plugin_is_required_only_in_the_mail_home_and_never_in_another(self) -> None:
        self.commit_plugins("email-outcome", "email-intake")
        self.install(self.root, "plugins/email-intake/__init__.py", "# email-intake\n")
        mail = self.root / "profiles" / "mail-worker"

        self.assertEqual(self.check("--ref", "mail", "--no-fetch"), (0, ""))

        mail.mkdir(parents=True)
        self.assertEqual(self.check("--ref", "mail", "--no-fetch"), (1, f"plugin email-outcome in {mail}: not installed\n"))

        self.install(mail, "plugins/email-outcome/__init__.py", "# email-outcome\n")
        for home in (self.root, self.worker):
            self.install(home, "plugins/email-outcome/__init__.py", "# email-outcome\n")
        self.install(self.worker, "plugins/email-intake/__init__.py", "# email-intake\n")
        self.assertEqual(self.check("--ref", "mail", "--no-fetch"), (1, "".join([
            f"plugin email-intake in {self.worker}: installed, but it runs only in the root home\n",
            f"plugin email-outcome in {self.root}: installed, but it runs only in the mail-worker home\n",
            f"plugin email-outcome in {self.worker}: installed, but it runs only in the mail-worker home\n",
        ])))

    def test_a_missing_mail_profile_is_reported_with_its_install_command(self) -> None:
        self.commit_plugins(mail_profile=True)

        code, out = self.check("--ref", "mail", "--no-fetch")

        self.assertEqual((code, out), (1, "profile mail-worker: is not installed (part 5)\n"))
        self.assertEqual(json.loads(self.raw)[0]["remedy"]["argv"][1:3], ["profile", "install"])

    def test_plugin_tests_are_ignored(self) -> None:
        self.install(self.worker, "plugins/p1/tests/test_a.py", "different\n")

        self.assertEqual(self.check(), (0, ""))

    # The fixture's fake Hermes lists every installed plugin as enabled unless installed-plugins.json says otherwise.
    def listing(self, home: Path, **statuses: str) -> None:
        names = sorted(path.name for path in (home / "plugins").iterdir())
        entries = [{"name": name, "status": statuses.get(name, "enabled")} for name in names if statuses.get(name) != "absent"]
        self.install(home, "installed-plugins.json", json.dumps(entries))

    def test_a_plugin_that_is_installed_but_not_enabled_is_reported_by_name(self) -> None:
        self.listing(self.worker, p1="not enabled")

        self.assertEqual(self.check(), (1, (
            f"plugin status p1 in {self.worker}: is not enabled in hermes plugins list, so its hooks do not run\n"
        )))

    def test_a_disabled_plugin_is_reported_by_name(self) -> None:
        self.listing(self.root, p1="disabled")

        self.assertEqual(self.check(), (1, (
            f"plugin status p1 in {self.root}: is disabled in hermes plugins list, so its hooks do not run\n"
        )))

    def test_enabled_plugins_are_silent(self) -> None:
        self.listing(self.root)
        self.listing(self.worker)

        self.assertEqual(self.check(), (0, ""))

    def test_a_plugin_missing_from_the_list_is_reported(self) -> None:
        self.listing(self.worker, p1="absent")

        self.assertEqual(self.check(), (1, (
            f"plugin status p1 in {self.worker}: is not in the list in hermes plugins list, so its hooks do not run\n"
        )))

    def test_a_list_that_is_not_json_gives_one_row_for_the_home(self) -> None:
        self.install(self.worker, "installed-plugins.json", "no list here")

        self.assertEqual(self.check(), (1, (
            f"plugin status {self.worker}: cannot run hermes plugins list: "
            "did not print a JSON list of plugins with a name and status\n"
        )))

    def test_a_plugin_that_fails_to_import_is_reported_by_name(self) -> None:
        self.install(self.worker, "plugins/p1/__init__.py", "raise RuntimeError('boom\\nhere')\n")

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertIn(f"plugin import p1 in {self.worker}: fails to import: RuntimeError: boom here\n", out)
        self.assertEqual(out.count("plugin import"), 1)

    def test_homes_that_cannot_import_give_one_row_each(self) -> None:
        (self.root / "tools" / "facts.json").unlink()

        reason = f"no Hermes Python in {self.root / 'tools' / 'facts.json'}"
        self.assertEqual(self.check(), (1, (
            f"slack profile {self.slack}: cannot check: {reason} (part 13)\n"
            f"root mcp {self.slack}: cannot check: {reason} (part 10)\n"
            f"plugin import {self.root}: cannot import the plugins: {reason}\n"
            f"plugin import {self.worker}: cannot import the plugins: {reason}\n"
        )))

    def test_a_plugin_with_no_code_is_not_imported(self) -> None:
        self.assertFalse((self.worker / "plugins" / "factory-secret-names" / "__init__.py").exists())
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
        stale = {**INSTALLED_ROUTE, "prompt": "old triage"}
        self.install(self.root, f"installed-config/{ROUTE_KEY}.json", json.dumps(stale))

        self.assertEqual(
            self.check(), (1, f"webhook config {ROUTE_KEY}: differs from the value rendered at {self.sha}\n")
        )

    def test_a_masked_route_field_with_another_value_differs(self) -> None:
        stale = {**INSTALLED_ROUTE, "coalesce": {"key": "fact...ther", "window_seconds": 30}}
        self.install(self.root, f"installed-config/{ROUTE_KEY}.json", json.dumps(stale))

        self.assertEqual(
            self.check(), (1, f"webhook config {ROUTE_KEY}: differs from the value rendered at {self.sha}\n")
        )

    def test_a_missing_route_skill_is_installed_before_the_routes(self) -> None:
        (self.root / "skills" / SKILL_NAME / "SKILL.md").unlink()
        stale = {**INSTALLED_ROUTE, "prompt": "old triage"}
        self.install(self.root, f"installed-config/{ROUTE_KEY}.json", json.dumps(stale))

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            f"route skill {self.root}/skills/{SKILL_NAME}/SKILL.md: is missing; a route at {self.sha} names it",
            f"webhook config {ROUTE_KEY}: differs from the value rendered at {self.sha}",
        ])
        installs = check_install.install_plan.plan([check_install.install_plan.drift_from_json(r) for r in json.loads(self.raw)]).installs
        self.assertEqual([install.step.name for install in installs], ["SKILLS", "ROUTES"])
        self.assertEqual(installs[0].argv[-1], "--install-skills")
        self.assertTrue(installs[0].restarts_gateway)

    def test_a_differing_route_skill_is_reported(self) -> None:
        self.install(self.root, f"skills/{SKILL_NAME}/SKILL.md", "old rules\n")

        self.assertEqual(self.check(), (
            1, f"route skill {self.root}/skills/{SKILL_NAME}/SKILL.md: differs from the skill rendered at {self.sha}\n"
        ))

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

    def test_a_slack_message_with_no_profile_route_is_reported(self) -> None:
        self.config(self.root, {k: v for k, v in ROOT_CONFIG.items() if k != "gateway.profile_routes"})

        self.assertEqual(self.check(), (
            1, f"slack profile gateway.profile_routes in {self.root}: has 0 routes for platform slack, not one (part 13)\n",
        ))

    def test_a_slack_channel_override_is_reported(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "platforms.slack.channel_overrides": {"C0000000001": {"model": "x"}}})

        self.assertEqual(self.check(), (
            1, f"slack profile platforms.slack.channel_overrides in {self.root}: is set; the Slack profile's model.default replaces it (part 13)\n",
        ))

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

        self.assertEqual(self.uploads.bodies, [{"op": "status"}])

    def test_broker_that_is_not_listening(self) -> None:
        self.uploads.close()

        self.assertEqual(
            self.check(),
            (1, f"upload broker {self.root}/factory-uploads.sock: is unreachable: the gateway's broker at "
                f"{self.root}/factory-uploads.sock did not answer: No such file or directory (part 5)\n"),
        )

    def test_broker_without_a_token(self) -> None:
        self.uploads.answers["status"] = [{**STATUS, "token_present": False}]

        self.assertEqual(
            self.check(),
            (
                1,
                f"upload broker {self.root}/factory-uploads.sock: "
                "reports token_present false: the broker holds no upload token (part 5)\n",
            ),
        )

    def test_broker_without_linear_credentials_leaves_every_worker_without_linear(self) -> None:
        self.uploads.answers["status"] = [{**STATUS, "linear_ready": False}]

        self.assertEqual(
            self.check(),
            (
                1,
                f"upload broker {self.root}/factory-uploads.sock: reports linear_ready false: the broker holds no "
                "Linear app credentials, so no worker reaches Linear (part 5)\n",
            ),
        )

    def test_broker_without_the_github_app_leaves_the_workers_gh_to_expire(self) -> None:
        self.uploads.answers["status"] = [{**STATUS, "github_app_ready": False}]

        self.assertEqual(
            self.check(),
            (
                1,
                f"upload broker {self.root}/factory-uploads.sock: reports github_app_ready false: the broker cannot "
                "mint the GitHub App installation token, so the worker's gh login expires within the hour (part 5)\n",
            ),
        )

    def test_broker_without_the_proxy_token_matters_only_when_one_is_configured(self) -> None:
        self.uploads.answers["status"] = [{**STATUS, "proxy_token_present": False}]
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
        self.uploads.answers["status"] = [{**STATUS, "mcp_tokens_present": {"sentry": True, "posthog": False}}]
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
                self.uploads.answers["status"] = [reply]

                code, out = self.check()

                self.assertEqual(code, 1)
                self.assertIn("upload broker", out)

    def test_worker_wait_limit_that_differs_from_the_runbook(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, "terminal.timeout": 1200})

        self.assertEqual(self.check(), (1, f"config terminal.timeout in {self.worker}: is not 400 (part 5)\n"))

    def test_proxy_provider_from_part_3_is_accepted(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "model.provider": "custom:proxy"})
        self.config(self.worker, {**WORKER_CONFIG, "model.provider": "custom:proxy"})
        with models_server(["opus"]) as api:
            self.config(self.slack, {"model.provider": "custom:proxy", "model.default": "opus", "providers.proxy.api": api})

            self.assertEqual(self.check(), (0, ""))

    def test_provider_the_runbook_does_not_document(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "model.provider": "openrouter"})
        self.config(self.worker, {**WORKER_CONFIG, "model.provider": "openrouter"})
        self.config(self.slack, {"model.provider": "openrouter", "model.default": "opus"})

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
        self.assertNotIn("s3cr3t-value", self.raw)

    def test_declared_input_that_is_not_set(self) -> None:
        config = dict(ROOT_CONFIG)
        del config["model.default"]
        self.config(self.root, {**config, "kanban.max_in_progress": ""})

        self.assertEqual(
            self.check(),
            (1, f"config model.default in {self.root}: is not set to the operator's model alias (part 4)\n"
                f"config kanban.max_in_progress in {self.root}: is not a positive card limit (part 10)\n"),
        )

    def test_capacity_limits_that_are_not_positive_integers(self) -> None:
        for bad in (True, 2.0, "8", 0, None):
            with self.subTest(bad=bad):
                config = {**ROOT_CONFIG, "kanban.max_in_progress": bad, "kanban.max_spawn": bad}
                if bad is None:
                    del config["kanban.max_in_progress"], config["kanban.max_spawn"]
                self.config(self.root, config)

                self.assertEqual(self.check(), (1, (
                    f"config kanban.max_in_progress in {self.root}: is not a positive card limit (part 10)\n"
                    f"config kanban.max_spawn in {self.root}: is not a positive limit on worker spawns per board "
                    "(part 10)\n"
                )))

    def test_capacity_that_leaves_triage_no_reserved_slot(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "kanban.max_spawn": 3})

        self.assertEqual(self.check(), (1, (
            "kanban capacity kanban.max_spawn: 3 plus triage.max_running 2 is more than kanban.max_in_progress 4, so "
            "the default board can take every slot triage needs (part 10)\n"
        )))

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
                self.assertNotIn("s3cr3t-value", self.raw)

    def test_worker_without_a_proxy_token_loads_no_secret(self) -> None:
        self.config(self.worker, {**WORKER_CONFIG, "secrets.command": {"enabled": True, "command": "s3cr3t-value"}})

        code, out = self.check()

        self.assertEqual(
            (code, out),
            (1, f"config secrets.command in {self.worker}: is set, but a worker without a proxy token loads no secret (part 5)\n"),
        )
        self.assertNotIn("s3cr3t-value", self.raw)

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
        self.assertNotIn("s3cr3t", self.raw)

    def test_the_email_route_secret_in_either_worker_env_is_reported_by_name_only(self) -> None:
        mail = self.root / "profiles" / "mail-worker"
        self.install(self.worker, ".env", "EMAIL_WEBHOOK_SECRET=s3cr3t-7\n")
        self.install(mail, ".env", "EMAIL_WEBHOOK_SECRET=s3cr3t-8\n")

        code, out = self.check()

        held = "is set, but the worker holds no factory secret; remove it"
        self.assertEqual((code, out), (1, "".join([
            f"secret EMAIL_WEBHOOK_SECRET in {self.worker}/.env: {held} (part 11)\n",
            f"secret EMAIL_WEBHOOK_SECRET in {mail}/.env: {held} (part 11)\n",
        ])))
        self.assertNotIn("s3cr3t", self.raw)

    def test_an_email_whose_fetch_failed_or_stalled_is_reported_by_card(self) -> None:
        path = self.root / "email-intake" / "ledger.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE emails (message_id TEXT UNIQUE, received_at REAL, delivery TEXT, state TEXT, card_id TEXT, reason TEXT, outcome TEXT)"
        )
        now = time.time()
        conn.executemany("INSERT INTO emails VALUES (?, ?, '{}', ?, ?, NULL, ?)", [
            ("<a@x>", now, "failed", "t_a", None),
            ("<b@x>", now - 3600, "fetching", "t_b", None),
            ("<c@x>", now, "fetching", "t_c", None),
            ("<d@x>", now, "ready", "t_d", "pending new-issue"),
            ("<e@x>", now, "ready", "t_e", "no-action A newsletter."),
            ("<f@x>", now - 25 * 3600, "ready", "t_f", None),
            ("<g@x>", now - 3600, "ready", "t_g", None),
        ])
        conn.commit()
        conn.close()

        self.assertEqual(self.check(), (1, "".join([
            "email email card t_a: failed its attachment fetch; the card stays blocked with the reason\n",
            "email email card t_b: has been fetching for 60 minutes; the gateway's email-intake never finished it\n",
            "email email card t_d: stopped while it recorded its outcome (pending new-issue); check Linear before a retry\n",
            "email email card t_f: has had its files for 25 hours and no outcome\n",
        ])))

    def test_an_mcp_token_in_the_worker_env_is_reported_by_name_only(self) -> None:
        self.uploads.answers["status"] = [{**STATUS, "mcp_tokens_present": {"sentry": True}}]
        self.proxy("CLAUDE_PROXY_TOKEN", self.helper(), {"sentry": "SENTRY_ACCESS_TOKEN"})
        self.install(self.worker, ".env", "SENTRY_ACCESS_TOKEN=s3cr3t-6\n")

        code, out = self.check()

        self.assertEqual((code, out), (
            1,
            f"secret SENTRY_ACCESS_TOKEN in {self.worker}/.env: is set, but the worker holds no factory secret; "
            "remove it (part 3)\n",
        ))
        self.assertNotIn("s3cr3t", self.raw)

    def test_an_mcp_token_variable_hermes_would_pass_to_every_process(self) -> None:
        self.uploads.answers["status"] = [{**STATUS, "mcp_tokens_present": {"sentry": True}}]
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
                f"cron job {RETRY_SCRIPT} in {self.root}: exists 2 times (part 12)\n"
                f"cron job report-deploy in {self.root}: exists 2 times (part 12)\n"),
        )

    def test_cron_job_changed_by_hand(self) -> None:
        self.cron(self.worker, [
            cron_job("refresh-checkouts", 15, enabled=False, no_agent=False),
            cron_job("refresh-owned-refs", 1),
            cron_job("reopen-conflicts", 5),
            cron_job("finish-merged", 5),
            cron_job("dispatch-triage", 1),
            cron_job("file-dependency-update", 10080),
            cron_job("file-recurring-issues", 60),
        ])
        self.cron(self.root, [cron_job(RETRY_SCRIPT, 10, "local"), ROOT_JOBS[1]])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-checkouts in {self.worker}: is disabled (part 12)\n"
                f"cron job refresh-checkouts in {self.worker}: runs every 15m, not every 60m (part 12)\n"
                f"cron job refresh-checkouts in {self.worker}: is not a script-only job that runs refresh-checkouts "
                "(part 12)\n"
                f'cron job {RETRY_SCRIPT} in {self.root}: delivers to "local", not the operator\'s chat platform '
                "(part 12)\n"),
        )

    def scheduled(self, entries: list | None = None, *, job: dict | None = None, script: str | None = None) -> None:
        entries = [SCHEDULED_ENTRY] if entries is None else entries
        for home, config in ((self.root, ROOT_CONFIG), (self.worker, WORKER_CONFIG)):
            self.config(home, {
                **config, "plugins.entries.linear-agent-session.settings": {**SETTINGS, "scheduled_commands": entries}, SCHEDULED_KEY: entries,
            })
        self.cron(self.root, [*ROOT_JOBS, job or cron_expression_job(SCHEDULED_JOB, "0 9 * * 1")])
        self.install(self.root, f"scripts/{SCHEDULED_JOB}", REPO_FILES[SCHEDULED_SOURCE] if script is None else script)

    def test_a_scheduled_command_that_matches_its_entry_is_silent(self) -> None:
        self.scheduled()

        self.assertEqual(self.check(), (0, ""))

    def test_a_scheduled_command_with_no_job_or_script_is_named(self) -> None:
        self.scheduled()
        self.cron(self.root, ROOT_JOBS)
        (self.root / "scripts" / SCHEDULED_JOB).unlink()

        self.assertEqual(self.check(), (1,
            f"script {self.root}/scripts/{SCHEDULED_JOB}: is missing; install it from {SCHEDULED_SOURCE} (part 12)\n"
            f"cron job {SCHEDULED_JOB} in {self.root}: is missing (part 12)\n"))

    def test_a_scheduled_command_job_that_exists_twice(self) -> None:
        job = cron_expression_job(SCHEDULED_JOB, "0 9 * * 1")
        self.scheduled(job=job)
        self.cron(self.root, [*ROOT_JOBS, job, job])

        self.assertEqual(self.check(), (1, f"cron job {SCHEDULED_JOB} in {self.root}: exists 2 times (part 12)\n"))

    def test_a_scheduled_command_job_in_the_worker_profile(self) -> None:
        job = cron_expression_job(SCHEDULED_JOB, "0 9 * * 1")
        self.scheduled(job=job)
        self.cron(self.root, ROOT_JOBS)
        self.cron(self.worker, [*WORKER_JOBS, job])

        self.assertEqual(self.check(), (1,
            f"cron job {SCHEDULED_JOB} in {self.root}: is missing (part 12)\n"
            f"cron job {SCHEDULED_JOB} in {self.worker}: belongs to the root profile (part 12)\n"))

    def test_a_scheduled_command_job_on_another_cron_expression(self) -> None:
        self.scheduled(job=cron_expression_job(SCHEDULED_JOB, "0 8 * * 1"))

        self.assertEqual(self.check(), (1, f"cron job {SCHEDULED_JOB} in {self.root}: runs 0 8 * * 1, not 0 9 * * 1 (part 12)\n"))

    def test_a_scheduled_command_job_that_delivers_nowhere(self) -> None:
        for deliver in ("local", ""):
            with self.subTest(deliver):
                self.scheduled(job=cron_expression_job(SCHEDULED_JOB, "0 9 * * 1", deliver))

                self.assertEqual(self.check(), (1,
                    f"cron job {SCHEDULED_JOB} in {self.root}: delivers to {json.dumps(deliver)}, "
                    "not the operator's chat platform (part 12)\n"))

    def test_a_scheduled_command_script_that_differs_from_the_runner(self) -> None:
        self.scheduled(script="#!/usr/bin/env python3\nold\n")

        code, out = self.check()

        self.assertEqual((code, out), (1,
            f"script {self.root}/scripts/{SCHEDULED_JOB}: differs from main at {self.sha} (part 12)\n"))

    def test_a_scheduled_job_with_no_entry_is_reported(self) -> None:
        self.scheduled(entries=[])
        (self.root / "scripts" / SCHEDULED_JOB).unlink()

        self.assertEqual(self.check(), (1, f"cron job {SCHEDULED_JOB} in {self.root}: has no scheduled_commands entry of that name (part 12)\n"))

    def test_an_invalid_scheduled_entry_is_reported_instead_of_crashing(self) -> None:
        self.scheduled(entries=[{**SCHEDULED_ENTRY, "schedule": "every day"}])

        code, out = self.check()

        self.assertEqual(code, 1)
        self.assertIn(f"config scheduled_commands in {self.worker}: is invalid: scheduled_commands example-job: schedule 'every day'", out)

    def test_catching_up_missed_runs_turned_off(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, "cron.catch_up_missed": False})

        self.assertEqual(self.check(), (1, f"config cron.catch_up_missed in {self.root}: is not true (part 12)\n"))

    def test_the_login_refresh_cron_job_that_launchd_replaced_is_reported(self) -> None:
        self.cron(self.worker, [*WORKER_JOBS, cron_job("refresh-gh-app-login", 30)])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-gh-app-login in {self.worker}: is replaced by a launchd job; "
                "remove it with hermes cron remove (part 10)\n"),
        )

    def test_cron_jobs_the_runbook_does_not_create(self) -> None:
        self.cron(self.worker, [*WORKER_JOBS, cron_job("nightly-cleanup", 1440)])
        self.cron(self.root, [*ROOT_JOBS, cron_job("refresh-checkouts", 60)])

        self.assertEqual(
            self.check(),
            (1, f"cron job refresh-checkouts in {self.root}: belongs to the worker profile (part 12)\n"
                f"cron job nightly-cleanup in {self.worker}: is not a job the setup runbook creates\n"),
        )

    def sentry_sync(self, section: dict | None, servers: dict | None = None) -> None:
        settings = {**SETTINGS, "claude_session": {"mcp_servers": servers or {}}}
        if section is not None:
            settings["sentry_sync"] = section
        self.config(self.root, {**ROOT_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        self.uploads.answers["status"] = [{**STATUS, "mcp_tokens_present": {"sentry": True}}]

    def test_the_sentry_sync_job_follows_its_settings_section(self) -> None:
        job = cron_job("sentry-linear-sync", 30)
        for name, section, jobs, expected in (
            ("section and job", SENTRY_SYNC, [*WORKER_JOBS, job], (0, "")),
            ("section and no job", SENTRY_SYNC, WORKER_JOBS, (1, f"cron job sentry-linear-sync in {self.worker}: is missing (part 12)\n")),
            ("no section and no job", None, WORKER_JOBS, (0, "")),
            ("empty section and no job", {}, WORKER_JOBS, (0, "")),
            ("no section and a job", None, [*WORKER_JOBS, job], (
                1, f"cron job sentry-linear-sync in {self.worker}: runs without a sentry_sync section in the factory "
                   "settings; remove it (part 12)\n")),
        ):
            with self.subTest(name):
                self.sentry_sync(section, SENTRY_SERVERS)
                self.cron(self.worker, jobs)
                self.assertEqual(self.check(), expected)
        records = {r["where"]: r for r in json.loads(self.raw)}
        self.assertEqual(records[f"sentry-linear-sync in {self.worker}"]["remedy"]["argv"][-2:], ["remove", "sentry-linear-sync"])

    def test_each_malformed_sentry_sync_field_and_an_unknown_mcp_server_is_one_drift(self) -> None:
        self.sentry_sync({**SENTRY_SYNC, "team_key": "abc", "checkout_path": "relative", "mcp_server": "other"}, SENTRY_SERVERS)
        self.cron(self.worker, [*WORKER_JOBS, cron_job("sentry-linear-sync", 30)])

        self.assertEqual(self.check(), (1, (
            f"config sentry_sync in {self.worker}: sentry_sync.team_key 'abc' must be a Linear team key such as ABC (part 10)\n"
            f"config sentry_sync in {self.worker}: sentry_sync.checkout_path 'relative' must be an absolute path to the "
            "worker-owned checkout (part 10)\n"
            f"config sentry_sync in {self.worker}: sentry_sync.mcp_server 'other' is not a claude_session.mcp_servers entry (part 10)\n"
        )))

    def dependabot_sweep(self, section: dict | None) -> None:
        settings = dict(SETTINGS)
        if section is not None:
            settings["dependabot_sweep"] = section
        self.config(self.root, {**ROOT_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})

    def test_the_dependabot_sweep_job_follows_its_settings_section(self) -> None:
        job = cron_job("dependabot-sweep", 1440)
        for name, section, jobs, expected in (
            ("section and job", DEPENDABOT_SWEEP, [*WORKER_JOBS, job], (0, "")),
            ("section and no job", DEPENDABOT_SWEEP, WORKER_JOBS, (1, f"cron job dependabot-sweep in {self.worker}: is missing (part 12)\n")),
            ("no section and no job", None, WORKER_JOBS, (0, "")),
            ("no section and a job", None, [*WORKER_JOBS, job], (
                1, f"cron job dependabot-sweep in {self.worker}: runs without a dependabot_sweep section in the factory "
                   "settings; remove it (part 12)\n")),
        ):
            with self.subTest(name):
                self.dependabot_sweep(section)
                self.cron(self.worker, jobs)
                self.assertEqual(self.check(), expected)

    def test_a_malformed_dependabot_sweep_field_is_one_drift(self) -> None:
        self.dependabot_sweep({**DEPENDABOT_SWEEP, "team_key": "abc"})
        self.cron(self.worker, [*WORKER_JOBS, cron_job("dependabot-sweep", 1440)])

        self.assertEqual(self.check(), (1, (
            f"config dependabot_sweep in {self.worker}: dependabot_sweep.team_key 'abc' must be a Linear team key such as ABC (part 10)\n"
        )))

    def test_each_repository_the_last_dependabot_sweep_did_not_file_is_one_advisory(self) -> None:
        self.dependabot_sweep(DEPENDABOT_SWEEP)
        self.cron(self.worker, [*WORKER_JOBS, cron_job("dependabot-sweep", 1440)])
        (self.worker / "dependabot-sweep-state.json").write_text(json.dumps({"run_at": "2026-11-03T12:00:00+00:00", "repositories": [
            {"repository": "example-org/other", "kind": "unbuildable", "open_alerts": 32, "reason": "private repository that is not the default repository"},
            {"repository": "example-org/off", "kind": "skipped", "open_alerts": 0, "reason": "Dependabot alerts are disabled"},
        ]}))

        self.assertEqual(self.check(), (1, (
            f"dependabot sweep {self.worker}/dependabot-sweep-state.json: example-org/other is not swept: 32 open Dependabot "
            "alerts; private repository that is not the default repository (part 12)\n"
            f"dependabot sweep {self.worker}/dependabot-sweep-state.json: example-org/off was skipped: no open Dependabot "
            "alerts read; Dependabot alerts are disabled (part 12)\n"
        )))

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

    def test_undelivered_last_run_names_its_delivery_error(self) -> None:
        undelivered = {**ROOT_JOBS[0], "last_status": "delivery_failed", "last_error": None,
                       "last_delivery_error": "slack: channel_not_found\nmore"}
        self.cron(self.root, [undelivered, *ROOT_JOBS[1:]])

        self.assertEqual(
            self.check(),
            (1, f"cron job {RETRY_SCRIPT} in {self.root}: last run's output was not delivered: "
                "slack: channel_not_found (part 12)\n"),
        )

    def test_check_install_job_left_from_an_earlier_setup(self) -> None:
        self.cron(self.root, [*ROOT_JOBS, {**cron_job("check-install", 60, "slack"), "script": "check-install-alert"}])
        self.cron(self.worker, [*WORKER_JOBS, cron_job("check-install", 60, "slack")])

        self.assertEqual(
            self.check(),
            (1, f"cron job check-install in {self.root}: is not a job the setup runbook creates\n"
                f"cron job check-install in {self.worker}: is not a job the setup runbook creates\n"),
        )

    def test_delivery_without_a_home_channel_is_reported(self) -> None:
        (self.root / ".env").unlink()

        self.assertEqual(
            self.check(),
            (1, f"cron job {RETRY_SCRIPT} in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"
                f"cron job report-deploy in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"),
        )

    def test_empty_home_channel_is_not_a_target(self) -> None:
        self.install(self.root, ".env", "SLACK_HOME_CHANNEL=\nSLACK_BOT_TOKEN=x\n")

        self.assertEqual(
            self.check(),
            (1, f"cron job {RETRY_SCRIPT} in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"
                f"cron job report-deploy in {self.root}: delivers to slack, which has no home channel in "
                f"{self.root}, so Hermes drops its output (part 12)\n"),
        )

    def test_home_channel_from_config_resolves_delivery(self) -> None:
        (self.root / ".env").unlink()
        self.config(self.root, {**ROOT_CONFIG, "platforms.slack.home_channel": {"chat_id": "C0123"}})

        self.assertEqual(self.check(), (0, ""))

    def test_explicit_and_local_delivery_targets_need_no_home_channel(self) -> None:
        (self.root / ".env").unlink()
        self.cron(self.root, [cron_job(RETRY_SCRIPT, 10, "slack:C0123"), cron_job("report-deploy", 5, "slack:C0123")])

        self.assertEqual(self.check(), (0, ""))

    def test_retry_cron_job_missing_or_changed(self) -> None:
        self.cron(self.root, [])
        missing = self.check()
        self.cron(self.root, [cron_job(RETRY_SCRIPT, 30, "slack", enabled=False), ROOT_JOBS[1]])
        changed = self.check()

        self.assertEqual(
            (missing, changed),
            ((1, f"cron job {RETRY_SCRIPT} in {self.root}: is missing (part 12)\n"
                 f"cron job report-deploy in {self.root}: is missing (part 12)\n"),
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

    def test_the_linear_route_script_missing_or_changed_is_reported_and_installs_before_the_routes(self) -> None:
        script = self.root / "scripts" / ROUTE_SCRIPT
        script.write_text("old\n")
        changed = self.check()
        script.unlink()
        missing = self.check()
        step = json.loads(self.raw)[0]["remedy"]["step"]

        self.assertEqual(
            (changed, missing, step),
            ((1, f"script {script}: differs from main at {self.sha} (part 12)\n"),
             (1, f"script {script}: is missing; install it from {ROUTE_SCRIPT_SOURCE} (part 12)\n"),
             "ROUTE_SCRIPTS"),
        )

    def test_webhook_retry_installed_in_the_worker_home_is_reported(self) -> None:
        self.install(self.worker, "plugins/webhook-retry/__init__.py", "# retry\n")

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
            board.execute("CREATE TABLE task_events (id, task_id, kind, payload, created_at)")
            board.execute("CREATE TABLE task_comments (id, task_id, author, body, created_at)")
        board.close()
        return path

    def test_board_databases_that_hold_the_columns_the_blocker_check_reads_are_silent(self) -> None:
        columns = "id, title, body, branch_name, status, block_kind, completion_contract, assignee, claim_lock, created_at"
        self.kanban_database(self.root / "kanban.db", columns)
        self.kanban_database(self.root / "kanban" / "boards" / "triage" / "kanban.db", columns)

        self.assertEqual(self.check(), (0, ""))

    def test_a_triage_board_with_a_stranded_card_is_reported_by_the_full_check(self) -> None:
        board = self.kanban_database(
            self.root / "kanban" / "boards" / "triage" / "kanban.db",
            "id, title, body, branch_name, status, block_kind, completion_contract, assignee, claim_lock, created_at",
        )
        with sqlite3.connect(board) as db:
            db.execute(
                "INSERT INTO tasks (id, status, assignee, created_at) VALUES ('t_old', 'ready', 'claude-worker', ?)",
                (time.time() - 3 * 3600 - 30,),
            )
        db.close()

        self.assertEqual(self.check(), (1, (
            f"stranded triage card t_old: has waited 180 minutes in ready with an assignee and "
            "no worker, so the default board is probably holding every worker slot (part 10)\n"
        )))

    def test_a_board_database_missing_a_column_the_blocker_check_reads(self) -> None:
        triage = self.kanban_database(self.root / "kanban" / "boards" / "triage" / "kanban.db", "id, title, body, state")

        self.assertEqual(self.check(), (1, (
            f"kanban board database {triage}: has no tasks.completion_contract, so a merged card cannot be "
            "completed: card_transition.py merged and finish-merged set it to local-only before they complete "
            "the card (part 15, check 24)\n"
            f"kanban board database {triage}: has no tasks.branch_name, tasks.status, tasks.block_kind, so a card "
            "cannot wait for its blockers or a reply (part 15, check 29)\n"
            f"stranded triage card check {triage}: cannot be read: no such column: t.created_at (part 10)\n"
            f"guarded card check {triage}: cannot be read: no such column: t.status\n"
            f"waiting triage card check {triage}: cannot be read: no such column: status (part 15, check 27)\n"
        )))

    def test_a_board_database_missing_a_comment_or_event_column_the_release_reads(self) -> None:
        board = self.root / "kanban.db"
        with sqlite3.connect(board) as db:
            db.execute("CREATE TABLE tasks (id, title, body, branch_name, status, block_kind, completion_contract)")
            db.execute("CREATE TABLE task_events (id, task_id, kind, data, created_at)")
            db.execute("CREATE TABLE task_comments (id, task_id, author, text, created_at)")
        db.close()

        self.assertEqual(self.check(), (1, (
            f"kanban board database {board}: has no task_comments.body, task_events.payload, so a card cannot wait "
            "for its blockers or a reply (part 15, check 29)\n"
            f"guarded card check {board}: cannot be read: no such column: e.payload\n"
        )))

    def test_a_board_database_without_completion_contract_says_a_merged_card_cannot_finish(self) -> None:
        columns = "id, title, body, branch_name, status, block_kind, assignee, claim_lock, created_at"
        board = self.kanban_database(self.root / "kanban.db", columns)

        self.assertEqual(self.check(), (1, (
            f"kanban board database {board}: has no tasks.completion_contract, so a merged card cannot be "
            "completed: card_transition.py merged and finish-merged set it to local-only before they complete "
            "the card (part 15, check 24)\n"
        )))

    def test_a_board_database_that_cannot_be_read(self) -> None:
        board = self.root / "kanban.db"
        board.write_text("not a database")

        code, output = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(
            output, f"kanban board database {board}: cannot be read for blockers: file is not a database "
            f"(part 15, check 29)\nguarded card check {board}: cannot be read: file is not a database\n",
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

    def recurring_issues(self, section: object) -> None:
        settings = {**SETTINGS, "recurring_issues": section}
        self.config(self.root, {**ROOT_CONFIG, "plugins.entries.linear-agent-session.settings": settings})
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})

    def test_valid_and_absent_recurring_issues_are_silent(self) -> None:
        entry = {
            "repository": "example-org/example-repo", "team_key": "ABC", "title_prefix": "Audit, week of ",
            "cadence": "weekly", "labels": ["Security"], "description": "Audit it.",
        }
        for section in ([], [entry], [entry, {**entry, "team_key": "DEF"}]):
            with self.subTest(section=section):
                self.recurring_issues(section)

                self.assertEqual(self.check(), (0, ""))

    def test_a_malformed_recurring_issue_is_reported_by_index(self) -> None:
        entry = {
            "repository": "example-org/example-repo", "team_key": "ABC", "title_prefix": "Audit, week of ",
            "cadence": "weekly", "labels": [], "description": "Audit it.",
        }
        self.recurring_issues([entry, {**entry, "cadence": "yearly"}])

        self.assertEqual(self.check(), (
            1, f"config recurring_issues in {self.worker}: "
               "recurring_issues[1] cadence 'yearly' must be one of daily, weekly, monthly\n",
        ))

    def test_a_recurring_issues_section_that_is_not_a_list_is_reported(self) -> None:
        self.recurring_issues({"cadence": "weekly"})

        self.assertEqual(self.check(), (
            1, f"config recurring_issues in {self.worker}: "
               "recurring_issues {'cadence': 'weekly'} must be a list, so file-recurring-issues files nothing\n",
        ))

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

    def test_a_setting_that_refuses_a_worker_run_is_reported_with_the_refusal_text(self) -> None:
        self.claude_session(fresh_after_tokens=-1)

        self.assertEqual(self.check(), (
            1, f"config claude_session in {self.worker}: claude_session.fresh_after_tokens -1 must be a positive integer\n",
        ))

    def test_a_leftover_sentry_token_env_is_reported(self) -> None:
        self.claude_session(sentry_token_env="SENTRY_ACCESS_TOKEN")

        self.assertEqual(self.check(), (1, (
            f"config claude_session in {self.worker}: "
            "claude_session.sentry_token_env is replaced by claude_session.mcp_servers.sentry\n"
        )))

    def test_a_repository_setting_that_refuses_its_run_is_reported_though_its_checkout_is_missing(self) -> None:
        settings = {**SETTINGS, "repositories": [{"full_name": "org/gone", "path": str(self.tmp / "gone"), "worker_entry": "{x}"}]}
        self.config(self.worker, {**WORKER_CONFIG, "plugins.entries.linear-agent-session.settings": settings})

        status, output = self.check()

        self.assertEqual(status, 1)
        self.assertIn(f"config org/gone in {self.worker}: worker_entry '{{x}}' must be a plain prefix", output)

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

    def test_work_skills_directory_that_does_not_exist(self) -> None:
        missing = self.tmp / "missing"
        self.config(self.root, {**ROOT_CONFIG, WORK_KEY: str(missing)})

        self.assertEqual(self.check(), (1, f"work skills {missing}: does not exist (part 5)\n"))

    def test_work_skills_directory_with_no_skill(self) -> None:
        (self.tmp / "skills" / "not-a-skill").mkdir(parents=True)
        self.config(self.root, {**ROOT_CONFIG, WORK_KEY: str(self.tmp / "skills")})

        self.assertEqual(self.check(), (1, f"work skills {self.tmp}/skills: holds no <name>/SKILL.md (part 5)\n"))

    def test_work_skills_allow_naming_a_skill_the_directory_lacks(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, WORK_ALLOW_KEY: ["example-skill", "ghost", "alpha"]})

        self.assertEqual(
            self.check(),
            (1, f"work skills {WORK_SKILLS}: lacks the allowed skill alpha (part 5)\n"
                f"work skills {WORK_SKILLS}: lacks the allowed skill ghost (part 5)\n"),
        )

    def test_work_skills_allow_that_is_not_a_list(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, WORK_ALLOW_KEY: "example-skill"})

        self.assertEqual(
            self.check(),
            (1, "work skills plugins.entries.work.settings.allow: is not a list of skill names (part 5)\n"),
        )

    def test_work_skills_directory_holding_only_an_invalid_name(self) -> None:
        skills = self.tmp / "skills"
        (skills / "bad.name").mkdir(parents=True)
        (skills / "bad.name" / "SKILL.md").write_text("---\nname: bad.name\n---\n")
        self.config(self.root, {**ROOT_CONFIG, WORK_KEY: str(skills)})

        self.assertEqual(self.check(), (1, f"work skills {skills}: holds no <name>/SKILL.md (part 5)\n"))

    def test_work_skills_with_a_matching_allow_is_silent(self) -> None:
        self.config(self.root, {**ROOT_CONFIG, WORK_ALLOW_KEY: ["example-skill"]})

        self.assertEqual(self.check(), (0, ""))

    def test_unset_work_skills_directory_is_reported_once_by_the_config_row(self) -> None:
        config = dict(ROOT_CONFIG)
        del config[WORK_KEY]
        self.config(self.root, config)

        self.assertEqual(
            self.check(),
            (1, f"config {WORK_KEY} in {self.root}: is not set to the work skills directory (part 5)\n"),
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
        self.launchd.plist(f"{agents}/ai.patinaproject.factory.gh-login.plist", GH_LOGIN_LABEL, **GH_LOGIN_FIELDS)
        self.launchd.plist(f"{agents}/{AUTO_DEPLOY_LABEL}.plist", AUTO_DEPLOY_LABEL, **AUTO_DEPLOY_FIELDS)
        self.launchd.plist(f"{agents}/{ARTIFACT_LABEL}.plist", ARTIFACT_LABEL, **ARTIFACT_FIELDS)
        env = {
            "PATH": "/usr/bin:/bin", **self.launchd.env, **self.host.env, "HOME": str(home), "HERMES_HOME": str(self.worker),
        }
        proc = subprocess.run(
            ["/usr/bin/env", "python3", str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True
        )

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_worker_home_outside_profiles_is_refused(self) -> None:
        env = {**os.environ, "HERMES_HOME": str(self.root), "HERMES_BIN": str(self.hermes)}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True)

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))


class CheckAppEventsTest(unittest.TestCase):
    SETTINGS = {"github": {"login": "example-app[bot]"}}

    def check(self, fetch, settings=SETTINGS) -> tuple:
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            lines = classified(self, check_install.check_app_events(settings, fetch))
        return lines, stderr.getvalue()

    def test_only_a_missing_push_event_adds_a_line(self) -> None:
        asked = []

        def fetch(slug: str) -> dict:
            asked.append(slug)
            return {"events": ["issues"], "permissions": {}}

        self.assertEqual(self.check(fetch), (
            ['github app example-app: does not subscribe to the "Push" event, so no merge starts auto-deploy until its next interval run'], "",
        ))
        self.assertEqual(asked, ["example-app"])
        self.assertEqual(self.check(lambda slug: {"events": ["push"], "permissions": {"contents": "none"}}), ([], ""))

    def test_a_failed_lookup_adds_no_line_and_one_note(self) -> None:
        def fetch(slug: str) -> dict:
            raise check_install.app_events.AppLookupError("gh api apps/example-app exited 1: HTTP 502")

        lines, stderr = self.check(fetch)

        self.assertEqual(lines, [])
        self.assertEqual(stderr.count("\n"), 1)
        self.assertIn("HTTP 502", stderr)

    def test_no_app_login_makes_no_lookup(self) -> None:
        def fetch(slug: str) -> dict:
            raise AssertionError("looked up an App")

        self.assertEqual(self.check(fetch, {"github": {}}), ([], ""))
        self.assertEqual(self.check(fetch, None), ([], ""))


class CheckTriageCapacityTest(unittest.TestCase):
    def rows(self, max_spawn, max_in_progress, max_running) -> list:
        return classified(self, check_install.check_triage_capacity(max_spawn, max_in_progress, max_running))

    def test_limits_that_fit_report_nothing(self) -> None:
        self.assertEqual(self.rows(2, 4, 2), [])
        self.assertEqual(self.rows(3, 4, 1), [])

    def test_the_spawn_cap_and_triage_cap_together_exceed_the_host_limit(self) -> None:
        self.assertEqual(self.rows(3, 4, 2), [(
            "kanban capacity kanban.max_spawn: 3 plus triage.max_running 2 is more than kanban.max_in_progress 4, so "
            "the default board can take every slot triage needs (part 10)"
        )])

    def test_the_triage_cap_exceeds_the_spawn_cap(self) -> None:
        self.assertEqual(self.rows(1, 4, 2), [(
            "kanban capacity kanban.max_spawn: triage.max_running 2 is more than kanban.max_spawn 1, so triage cannot run "
            "that many cards on its own board (part 10)"
        )])

    def test_both_rules_report_separately(self) -> None:
        self.assertEqual(len(self.rows(2, 3, 3)), 2)

    def test_a_value_that_is_not_a_positive_integer_reports_nothing(self) -> None:
        for bad in (True, 2.0, "8", 0, -1, None):
            with self.subTest(bad=bad):
                self.assertEqual(self.rows(bad, 4, 2), [])
                self.assertEqual(self.rows(2, bad, 2), [])
                self.assertEqual(self.rows(2, 4, bad), [])


class CheckGuardedCardsTest(unittest.TestCase):
    NOW = 1_000_000.0
    GUARD = ("respawn_guarded", '{"reason": "active_pr"}')

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "hermes"
        self.root.mkdir()
        self.default = self.board(self.root / "kanban.db")

    def board(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute("CREATE TABLE tasks (id, status)")
        db.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id, kind, payload, created_at)")
        return db

    def card(self, id: str, events: list, *, status: str = "ready", db: sqlite3.Connection | None = None) -> None:
        db = db or self.default
        db.execute("INSERT INTO tasks VALUES (?, ?)", (id, status))
        for kind, payload, age in events:
            db.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (id, kind, payload, self.NOW - age * 60),
            )
        db.commit()

    def rows(self) -> list:
        return classified(self, check_install.check_guarded_cards(self.root, self.NOW))

    def test_a_ready_card_held_as_active_pr_for_eleven_minutes(self) -> None:
        self.card("t_held", [("review_reopened", None, 12), (*self.GUARD, 11), (*self.GUARD, 6), (*self.GUARD, 0)])

        self.assertEqual(self.rows(), [(
            "guarded card t_held: has been held in ready from its worker as active_pr for 11 minutes, because a "
            "card comment holds a pull request URL; an `assigned` handoff lifts the hold"
        )])

    def test_a_card_on_another_board_is_reported(self) -> None:
        other = self.board(self.root / "kanban" / "boards" / "triage" / "kanban.db")
        self.card("t_triage", [("created", None, 60), (*self.GUARD, 59), (*self.GUARD, 1)], db=other)

        self.assertEqual([row.split(":")[0] for row in self.rows()], ["guarded card t_triage"])

    def test_a_hold_of_five_minutes_is_not_reported(self) -> None:
        self.card("t_new", [("review_reopened", None, 6), (*self.GUARD, 5), (*self.GUARD, 0)])

        self.assertEqual(self.rows(), [])

    def test_a_run_broken_by_another_event_counts_from_the_newer_run(self) -> None:
        self.card("t_back", [(*self.GUARD, 60), ("assigned", None, 7), (*self.GUARD, 6), (*self.GUARD, 0)])

        self.assertEqual(self.rows(), [])

    def test_a_newest_guard_with_another_reason_is_not_reported(self) -> None:
        self.card("t_rate", [(*self.GUARD, 30), ("respawn_guarded", '{"reason": "rate_limit_cooldown"}', 0)])

        self.assertEqual(self.rows(), [])

    def test_a_card_that_left_ready_is_not_reported(self) -> None:
        self.card("t_review", [(*self.GUARD, 30), (*self.GUARD, 0)], status="review")

        self.assertEqual(self.rows(), [])

    def test_an_unreadable_board_is_reported(self) -> None:
        (self.root / "kanban.db").write_bytes(b"not a database")

        self.assertEqual(self.rows(), [f"guarded card check {self.root / 'kanban.db'}: cannot be read: file is not a database"])


class CheckStaleFailureCardsTest(unittest.TestCase):
    NOW = 1_000_000.0
    GUARD = ("respawn_guarded", '{"reason": "blocker_auth"}')

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "hermes"
        self.root.mkdir()
        self.default = self.board(self.root / "kanban.db")

    def board(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.execute("CREATE TABLE tasks (id, status)")
        db.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id, kind, payload, created_at)")
        db.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id, outcome, ended_at)")
        return db

    def card(self, id: str, events: list, runs: list, *, db: sqlite3.Connection | None = None) -> None:
        db = db or self.default
        db.execute("INSERT INTO tasks VALUES (?, 'ready')", (id,))
        for kind, payload, age in events:
            db.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (id, kind, payload, self.NOW - age * 60),
            )
        for outcome, age in runs:
            db.execute("INSERT INTO task_runs (task_id, outcome, ended_at) VALUES (?, ?, ?)", (id, outcome, self.NOW - age * 60))
        db.commit()

    def rows(self) -> list:
        return classified(self, check_install.check_guarded_cards(self.root, self.NOW))

    def test_a_card_held_as_blocker_auth_for_an_hour_after_a_review_run_is_reported(self) -> None:
        self.card("t_held", [(*self.GUARD, 61), (*self.GUARD, 30), (*self.GUARD, 0)], [("rate_limited", 200), ("review_requested", 90)])

        self.assertEqual(self.rows(), [(
            "guarded card t_held: has been held in ready from its worker as blocker_auth for 61 minutes, because "
            "last_failure_error still names an old quota or auth error after its newest run ended in review or "
            "completion; reassigning the card clears the error"
        )])

    def test_a_card_whose_newest_run_completed_is_reported(self) -> None:
        self.card("t_done", [(*self.GUARD, 60), (*self.GUARD, 0)], [("completed", 90)])

        self.assertEqual([row.split(":")[0] for row in self.rows()], ["guarded card t_done"])

    def test_a_hold_of_fifty_nine_minutes_is_not_reported(self) -> None:
        self.card("t_new", [(*self.GUARD, 59), (*self.GUARD, 0)], [("review_requested", 90)])

        self.assertEqual(self.rows(), [])

    def test_a_card_whose_newest_run_is_rate_limited_or_crashed_is_not_reported(self) -> None:
        for outcome in ("rate_limited", "crashed"):
            with self.subTest(outcome):
                self.default.execute("DELETE FROM tasks")
                self.default.execute("DELETE FROM task_events")
                self.default.execute("DELETE FROM task_runs")
                self.card("t_other", [(*self.GUARD, 90), (*self.GUARD, 0)], [("review_requested", 200), (outcome, 100)])

                self.assertEqual(self.rows(), [])

    def test_a_hold_broken_by_another_event_counts_from_the_newer_run(self) -> None:
        self.card("t_back", [(*self.GUARD, 120), ("assigned", None, 40), (*self.GUARD, 30), (*self.GUARD, 0)], [("review_requested", 200)])

        self.assertEqual(self.rows(), [])

    def test_an_active_pr_hold_is_not_reported_as_a_stale_failure(self) -> None:
        self.card("t_pr", [("respawn_guarded", '{"reason": "active_pr"}', 90), ("respawn_guarded", '{"reason": "active_pr"}', 0)], [("review_requested", 200)])

        self.assertEqual(len(self.rows()), 1)
        self.assertIn(" as active_pr for ", self.rows()[0])

    def test_a_card_on_another_board_is_reported(self) -> None:
        other = self.board(self.root / "kanban" / "boards" / "triage" / "kanban.db")
        self.card("t_triage", [(*self.GUARD, 90), (*self.GUARD, 0)], [("review_requested", 200)], db=other)

        self.assertEqual([row.split(":")[0] for row in self.rows()], ["guarded card t_triage"])

    def test_a_board_without_task_runs_is_reported_as_unreadable(self) -> None:
        self.default.execute("DROP TABLE task_runs")
        self.card("t_held", [(*self.GUARD, 90), (*self.GUARD, 0)], [])

        self.assertEqual(self.rows(), [f"guarded card check {self.root / 'kanban.db'}: cannot be read: no such table: task_runs"])

    def test_an_unreadable_board_is_reported(self) -> None:
        (self.root / "kanban.db").write_bytes(b"not a database")

        self.assertEqual(self.rows(), [f"guarded card check {self.root / 'kanban.db'}: cannot be read: file is not a database"])


class CheckStrandedTriageTest(unittest.TestCase):
    NOW = 1_000_000.0

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "hermes"
        self.path = self.root / "kanban" / "boards" / "triage" / "kanban.db"
        self.path.parent.mkdir(parents=True)
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE tasks (id, assignee, status, created_at, claim_lock)")
        self.db.execute("CREATE TABLE task_events (task_id, kind, created_at)")

    def card(self, id: str, *, assignee="claude-worker", status="ready", age=3600, claim=None, events=()) -> None:
        self.db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", (id, assignee, status, self.NOW - age * 60, claim)
        )
        for kind, event_age in events:
            self.db.execute("INSERT INTO task_events VALUES (?, ?, ?)", (id, kind, self.NOW - event_age * 60))
        self.db.commit()

    def rows(self) -> list:
        return classified(self, check_install.check_stranded_triage(self.root, self.NOW))

    def test_an_assigned_ready_card_with_no_claim_past_the_threshold(self) -> None:
        self.card("t_stuck", age=95)

        self.assertEqual(self.rows(), [(
            "stranded triage card t_stuck: has waited 95 minutes in ready with an assignee and no worker, so "
            "the default board is probably holding every worker slot (part 10)"
        )])

    def test_a_card_that_waits_for_an_assignee_is_not_stranded(self) -> None:
        self.card("t_none", assignee=None)
        self.card("t_blank", assignee="  ")

        self.assertEqual(self.rows(), [])

    def test_a_claimed_card_is_not_stranded(self) -> None:
        self.card("t_claimed", claim="lock-1")

        self.assertEqual(self.rows(), [])

    def test_a_card_in_another_status_is_not_stranded(self) -> None:
        self.card("t_run", status="running")

        self.assertEqual(self.rows(), [])

    def test_a_card_under_the_threshold_is_not_stranded(self) -> None:
        self.card("t_new", age=29)

        self.assertEqual(self.rows(), [])

    def test_the_wait_starts_at_the_threshold(self) -> None:
        self.card("t_edge", age=30)

        self.assertEqual(len(self.rows()), 1)

    def test_the_latest_event_sets_the_wait(self) -> None:
        self.card("t_assigned", age=300, events=[("created", 300), ("assigned", 10)])
        self.card("t_promoted", age=300, events=[("created", 300), ("promoted", 45)])

        self.assertEqual(self.rows(), [(
            "stranded triage card t_promoted: has waited 45 minutes in ready with an assignee and no worker, so "
            "the default board is probably holding every worker slot (part 10)"
        )])

    def test_other_events_do_not_reset_the_wait(self) -> None:
        self.card("t_comment", age=300, events=[("commented", 1)])

        self.assertEqual(len(self.rows()), 1)

    def test_a_card_with_no_events_waits_from_its_creation(self) -> None:
        self.card("t_bare", age=40)

        self.assertEqual(self.rows(), [
            "stranded triage card t_bare: has waited 40 minutes in ready with an assignee and no worker, so "
            "the default board is probably holding every worker slot (part 10)"
        ])

    def test_a_card_held_by_auto_deploy_is_not_stranded(self) -> None:
        self.card("t_held", age=95)
        (self.root / "auto-deploy.json").write_text(json.dumps({"hold": {"pid": 4242, "dispatch_profiles": True}}))
        held = self.rows()
        (self.root / "auto-deploy.json").write_text(json.dumps({"hold": None}))

        self.assertEqual((held, len(self.rows())), ([], 1))

    def test_a_missing_database_is_silent(self) -> None:
        self.path.unlink()

        self.assertEqual(self.rows(), [])

    def test_a_database_that_cannot_be_read_is_reported(self) -> None:
        self.db.close()
        self.path.write_text("not a database")

        rows = self.rows()

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith(f"stranded triage card check {self.path}: cannot be read: "))


GH_LOGIN_LABEL = "ai.patinaproject.factory.gh-login"
GH_LOGIN_FIELDS = {
    "StartInterval": 900, "EnvironmentVariables": {"GH_CONFIG_DIR": "/w/gh", "HERMES_HOME": "/w"},
}
AUTO_DEPLOY_LABEL = "ai.patinaproject.factory.auto-deploy"
ARTIFACT_LABEL = "ai.patinaproject.factory.claude-artifact"
ARTIFACT_FIELDS = {"EnvironmentVariables": {"HERMES_HOME": "/w", "PATH": "/w/.local/bin:/usr/bin"}}
AUTO_DEPLOY_FIELDS = {
    "StartInterval": 300, "EnvironmentVariables": {"GH_CONFIG_DIR": "/w/gh", "HERMES_HOME": "/h", "PATH": "/h/.local/bin:/usr/bin"},
}
class CheckAutoDeployTest(unittest.TestCase):
    NOW = 2_000_000.0
    SHA = "c" * 40

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.alive: set = set()
        self.commit = check_install.Commit(self.root, self.SHA, "main")

    def state(self, **fields) -> None:
        (self.root / "auto-deploy.json").write_text(json.dumps({"deployed": self.SHA, "hold": None, **fields}))

    def check(self, dispatch_profiles=None, *, age_hours: float = 1, commit="default") -> list:
        commit = self.commit if commit == "default" else commit
        return classified(self, check_install.check_auto_deploy(
            self.root, dispatch_profiles, commit, self.NOW - age_hours * 3600, self.NOW, self.alive.__contains__,
        ))

    def hold(self, pid: int = 4242, **parts) -> dict:
        return {"pid": pid, "commit": "d" * 40, "dispatch_profiles": False, "pause": None, "gateway_lock": False, **parts}

    def test_a_deployed_commit_with_no_hold_is_silent(self) -> None:
        self.state()

        self.assertEqual(self.check(), [])

    def test_a_missing_or_unreadable_state_file_is_reported(self) -> None:
        missing = self.check()
        (self.root / "auto-deploy.json").write_text("{not json")
        unreadable = self.check()

        self.assertEqual((missing, unreadable), (
            [f"auto deploy {self.root}/auto-deploy.json: is missing, so auto-deploy has never run (part 12)"],
            [f"auto deploy {self.root}/auto-deploy.json: is unreadable, so every auto-deploy run stops before it deploys"],
        ))

    def test_a_stale_run_lock_is_reported_and_a_live_ones_silences_everything(self) -> None:
        self.state(hold=self.hold(dispatch_profiles=True))
        (self.root / "auto-deploy.lock").write_text("4242\n")

        stale = self.check()
        self.alive.add(4242)
        live = self.check([])

        self.assertEqual(stale, [
            f"auto deploy {self.root}/auto-deploy.lock: names pid 4242, which is not a running auto-deploy; the next run reclaims it",
            f"auto deploy kanban.dispatch_profiles in {self.root}: was set by auto-deploy run 4242, which is not running, "
            "so Hermes claims no card",
        ])
        self.assertEqual(live, [])

    def test_a_dead_runs_hold_pause_and_gateway_lock_are_reported(self) -> None:
        self.state(hold=self.hold(dispatch_profiles=True, pause="auto-deploy 4242: installing", gateway_lock=True))
        (self.root / "factory-gateway.lock").write_text("4242\n")

        self.assertEqual(self.check([]), [
            f"auto deploy kanban.dispatch_profiles in {self.root}: was set by auto-deploy run 4242, which is not running, "
            "so Hermes claims no card",
            f"auto deploy {self.root}/auto-deploy.json: records a hermes pause of auto-deploy run 4242, which is not "
            "running, so no turn may run",
            f"auto deploy {self.root}/factory-gateway.lock: holds auto-deploy run 4242, which is not running, so "
            "watch-gateway restarts nothing",
        ])

    def test_a_pending_restart_with_no_live_run_is_reported(self) -> None:
        self.state(restart_pending=self.SHA)
        pending = self.check()
        (self.root / "auto-deploy.lock").write_text("4242\n")
        self.alive.add(4242)
        running = self.check()

        self.assertEqual((pending, running), ([
            f"auto deploy {self.root}/auto-deploy.json: records a gateway restart for {self.SHA[:12]} that has not "
            "succeeded, so the gateway may still run the plugins and config from before it",
        ], []))

    def test_a_hold_whose_run_is_alive_is_silent(self) -> None:
        self.state(hold=self.hold(dispatch_profiles=True, gateway_lock=True))
        (self.root / "factory-gateway.lock").write_text("4242\n")
        self.alive.add(4242)

        self.assertEqual(self.check([]), [])

    def test_a_gateway_lock_a_person_holds_is_not_reported(self) -> None:
        self.state(hold=self.hold(gateway_lock=True))
        (self.root / "factory-gateway.lock").write_text("deploying by hand since 10:00\n")

        self.assertEqual(self.check(), [])

    def test_a_dispatch_profiles_key_no_run_set_is_reported(self) -> None:
        self.state()

        self.assertEqual(self.check([]), [
            f"auto deploy kanban.dispatch_profiles in {self.root}: is set, and no auto-deploy run set it, so Hermes "
            "claims no card for a profile outside it and auto-deploy deploys nothing",
        ])
        self.assertEqual(self.check(["claude-worker"])[0], self.check([])[0])

    def test_an_undeployed_commit_a_day_old_is_reported_with_its_outcome(self) -> None:
        self.state(deployed="b" * 40, attempted=self.SHA, outcome="FAILED", detail="check-settings exited 1: model differs")
        failed = self.check(age_hours=30)
        self.state(deployed="b" * 40, attempted="b" * 40, outcome="DEPLOYED")
        lost = self.check(age_hours=24)

        self.assertEqual(failed, [
            f"auto deploy origin/main at ccccccc: is not deployed 30 hours after it was committed; auto-deploy "
            "recorded FAILED: check-settings exited 1: model differs",
        ])
        self.assertEqual(lost, [
            "auto deploy origin/main at ccccccc: is not deployed 24 hours after it was committed; auto-deploy "
            "recorded no outcome for it, so its push did not start a run",
        ])

    def test_an_undeployed_commit_under_a_day_or_a_ref_is_silent(self) -> None:
        self.state(deployed="b" * 40, attempted=self.SHA, outcome="WAITING")

        self.assertEqual((self.check(age_hours=23.9), self.check(age_hours=48, commit=None)), ([], []))

    def test_live_deploy_needs_a_running_auto_deploy(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "auto-deploy"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        done = subprocess.Popen([sys.executable, "-c", "pass", "auto-deploy"])
        done.wait()

        self.assertEqual(
            [check_install.live_deploy(pid) for pid in (proc.pid, os.getpid(), done.pid, None, "4242")],
            [True, False, False, False, False],
        )

    def test_the_names_equal_auto_deploy_and_the_push_hook(self) -> None:
        scripts = SCRIPTS.parents[2] / "skills" / "setup-factory" / "scripts"
        loader = importlib.machinery.SourceFileLoader("auto_deploy", str(scripts / "auto-deploy"))
        deploy = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(deploy)
        hook_spec = importlib.util.spec_from_file_location("factory_deploy", SCRIPTS.parents[2] / "plugins" / "factory-deploy" / "__init__.py")
        hook = importlib.util.module_from_spec(hook_spec)
        hook_spec.loader.exec_module(hook)
        service = next(s for s in check_install.SERVICES if s.name == "auto deploy")

        self.assertEqual((Path(service.plist).name, check_install.HOLD_KEY), (f"{deploy.LABEL}.plist", deploy.HOLD_KEY))
        printed = plistlib.loads(deploy.plist(Path("/h"), Path("/h/profiles/claude-worker")))
        self.assertEqual(sorted(printed["EnvironmentVariables"]), sorted(service.env))
        self.assertEqual((printed["StartInterval"], service.interval), (300, 300))
        self.assertEqual(hook.LABEL, deploy.LABEL)
        self.assertIn("factory-deploy", check_install.ROOT_ONLY_PLUGINS)


COMMERCE = "com.apple.commerce"
SOFTWARE_UPDATE = "com.apple.SoftwareUpdate"
MACOS_KEY = "AutomaticallyInstallMacOSUpdates"
DISABLE_SCRIPT = "skills/setup-factory/scripts/disable-auto-updates"
APP_STORE_LINE = f"app auto-update {COMMERCE}: AutoUpdate is on or unset; run sudo python3 {DISABLE_SCRIPT}\n"
MACOS_LINE = f"macos auto-update {SOFTWARE_UPDATE}: {MACOS_KEY} is on or unset; run sudo python3 {DISABLE_SCRIPT}\n"


def first_launch_line(developer: Path) -> str:
    return (
        f"xcode first launch {developer}: xcodebuild -checkFirstLaunchStatus reports a first launch pending; "
        "run sudo xcodebuild -runFirstLaunch\n"
    )


class HostUpdates:
    """A preferences root with both automatic updates off, and an Xcode with no first launch pending."""

    def __init__(self, tmp: Path) -> None:
        self.preferences = tmp / "library"
        self.xcode = FakeXcode(tmp)
        self.set("Preferences", COMMERCE, AutoUpdate=False)
        self.set("Preferences", SOFTWARE_UPDATE, **{MACOS_KEY: False})

    def set(self, folder: str, domain: str, **values) -> None:
        path = self.preferences / folder / f"{domain}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(values))

    @property
    def env(self) -> dict:
        return {
            "PREFERENCES_ROOT": str(self.preferences), "XCODEBUILD_BIN": str(self.xcode.xcodebuild),
            "DEVELOPER_DIR": str(self.xcode.developer),
        }


class CheckHostUpdatesTest(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host = HostUpdates(self.tmp)

    def lines(self, developer: Path | None = None) -> str:
        env = {"DEVELOPER_DIR": str(developer or self.host.xcode.developer)}
        drifts = check_install.check_host_updates(self.host.preferences, str(self.host.xcode.xcodebuild), env)
        records = [drift.to_json() for drift in drifts]
        self.assertEqual({record["remedy"]["type"] for record in records} - {"manual"}, set())
        self.assertEqual({record["remedy"]["gates_runs"] for record in records} - {False}, set())
        return "".join(f"{line}\n" for line in classified(self, records))

    def test_everything_off_and_no_first_launch_pending_is_clean(self) -> None:
        self.assertEqual(self.lines(), "")

    def test_app_store_auto_update_on_or_unset_is_reported(self) -> None:
        self.host.set("Preferences", COMMERCE, AutoUpdate=True)
        on = self.lines()
        self.host.set("Preferences", COMMERCE)
        unset = self.lines()
        (self.host.preferences / "Preferences" / f"{COMMERCE}.plist").unlink()
        absent = self.lines()

        self.assertEqual((on, unset, absent), (APP_STORE_LINE,) * 3)

    def test_automatic_macos_install_on_or_unset_is_reported(self) -> None:
        self.host.set("Preferences", SOFTWARE_UPDATE, **{MACOS_KEY: True})
        on = self.lines()
        self.host.set("Preferences", SOFTWARE_UPDATE)
        unset = self.lines()

        self.assertEqual((on, unset), (MACOS_LINE,) * 2)

    def test_a_managed_value_overrides_the_local_one(self) -> None:
        self.host.set("Managed Preferences", COMMERCE, AutoUpdate=True)
        self.host.set("Managed Preferences", SOFTWARE_UPDATE, **{MACOS_KEY: True})
        managed_on = self.lines()
        self.host.set("Preferences", COMMERCE, AutoUpdate=True)
        self.host.set("Managed Preferences", COMMERCE, AutoUpdate=False)
        managed_off = self.lines()

        self.assertEqual((managed_on, managed_off), (APP_STORE_LINE + MACOS_LINE, MACOS_LINE))

    def test_a_pending_first_launch_is_reported(self) -> None:
        self.host.xcode.answer(status=1)

        self.assertEqual(self.lines(), first_launch_line(self.host.xcode.developer))
        self.assertEqual(self.host.xcode.called(), ["-checkFirstLaunchStatus"])

    def test_a_first_launch_check_that_hangs_is_reported_as_a_timeout_without_the_sudo_remedy(self) -> None:
        self.host.xcode.answer(hang=True)

        with mock.patch.object(check_install.xcode, "FIRST_LAUNCH_SECONDS", 0.5):
            lines = self.lines()

        self.assertEqual(lines, (
            f"xcode first launch timeout {self.host.xcode.developer}: xcodebuild -checkFirstLaunchStatus timed out "
            "after 0.5 seconds; host load is a likely cause, so run check-install again once it drops\n"
        ))
        self.assertNotIn("sudo", lines)

    def test_the_first_launch_check_waits_thirty_seconds(self) -> None:
        self.assertEqual(check_install.xcode.FIRST_LAUNCH_SECONDS, 30)

    def test_command_line_tools_alone_skip_the_first_launch_check(self) -> None:
        self.host.xcode.answer(status=1)

        self.assertEqual(self.lines(self.host.xcode.command_line_tools), "")
        self.assertEqual(self.lines(self.host.xcode.dir / "missing"), "")
        self.assertEqual(self.host.xcode.called(), [])


class Launchd:

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
        self.gh_login = self.plist(
            f"{str(self.home).lstrip('/')}/Library/LaunchAgents/{GH_LOGIN_LABEL}.plist", GH_LOGIN_LABEL, **GH_LOGIN_FIELDS,
        )
        self.deploy = self.plist(
            f"{str(self.home).lstrip('/')}/Library/LaunchAgents/{AUTO_DEPLOY_LABEL}.plist", AUTO_DEPLOY_LABEL,
            **AUTO_DEPLOY_FIELDS,
        )
        self.artifact = self.plist(
            f"{str(self.home).lstrip('/')}/Library/LaunchAgents/{ARTIFACT_LABEL}.plist", ARTIFACT_LABEL, **ARTIFACT_FIELDS,
        )
        uid = os.getuid()
        self.states({
            "system/com.example.cloudflared": "running", f"user/{uid}/ai.hermes.gateway": "running",
            f"user/{uid}/ai.patinaproject.factory.gateway-watch": "not running",
            f"user/{uid}/{GH_LOGIN_LABEL}": "not running",
            f"user/{uid}/ai.patinaproject.factory.auto-deploy": "not running",
            f"user/{uid}/{ARTIFACT_LABEL}": "running",
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
        return classified(self, check_install.check_mcp_servers(self.settings, self.worker))

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

    def test_a_setting_that_refuses_every_run_skips_the_probes(self) -> None:
        self.settings["claude_session"] = {"sentry_token_env": "SENTRY_ACCESS_TOKEN"}
        report = {"servers": [{"name": "sentry", "status": "failed"}], "disabled": []}

        self.assertEqual(self.check({"example-org/app": [0, report], "example-org/site": [0, report]}), [])

    def test_a_report_that_is_not_an_object_is_named(self) -> None:
        self.assertEqual(self.check({"example-org/app": [2, ["no"]], "example-org/site": [0, "no"]}), [
            "mcp example-org/app: cannot read the MCP server status: exited 2 without a report (part 10)",
            "mcp example-org/site: cannot read the MCP server status: exited 0 without a report (part 10)",
        ])

    def test_without_an_installed_claude_session_or_settings_nothing_runs(self) -> None:
        (self.worker / "scripts" / "claude-session").unlink()

        self.assertEqual(self.check({}), [])
        self.assertEqual(classified(self, check_install.check_mcp_servers(None, self.worker)), [])


FAKE_ROOT_MCP_HERMES = """#!/usr/bin/env python3
import json, os, sys
home = os.environ["HERMES_HOME"]
if sys.argv[1:3] == ["mcp", "test"]:
    sys.exit(0 if sys.argv[3] in json.load(open(os.path.join(home, "connects.json"))) else 1)
sys.exit("check-install must read mcp_servers from config.yaml, not from hermes: " + " ".join(sys.argv[1:]))
"""

FAKE_RUAMEL = """import json


class YAML:
    def __init__(self, typ="rt", pure=False):
        assert typ == "safe" and pure is True
    def load(self, stream):
        text = stream.read()
        try:
            return json.loads(text)
        except ValueError:
            raise ValueError("while parsing a flow sequence: " + text) from None
"""

SECRET = "sntryu_0123456789abcdef"
ROOT_SERVER = {
    "url": "https://mcp.example/mcp/<org>",
    "headers": {"Authorization": "Sentry-Bearer ${SENTRY_ACCESS_TOKEN}"},
    "sampling": {"enabled": False},
}


class CheckRootMcpServersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.home = self.root / "profiles" / "slack"
        self.home.mkdir(parents=True)
        self.hermes = self.tmp / "hermes"
        self.hermes.write_text(with_interpreter(FAKE_ROOT_MCP_HERMES))
        self.hermes.chmod(0o755)
        (self.root / "tools" / "py").mkdir(parents=True)
        (self.root / "tools" / "py" / "bin").mkdir()
        (self.root / "tools" / "py" / "bin" / "python3").symlink_to(sys.executable)
        (self.root / "tools" / "facts.json").write_text(json.dumps({"packages": {"python": {"entry": "py"}}}))
        env = self.tmp / "env"
        site = env / "lib" / "python3" / "site-packages"
        (site / "ruamel" / "yaml").mkdir(parents=True)
        (site / "ruamel" / "yaml" / "__init__.py").write_text(FAKE_RUAMEL)
        (self.root / "installs" / "one").mkdir(parents=True)
        (self.root / "installs" / "one" / "facts.json").write_text(
            json.dumps({"packages": {"venv": {"environment": str(env)}}})
        )

    def check(self, servers, connects=("sentry",)) -> list:
        (self.home / "config.yaml").write_text(json.dumps({"model": "x", "mcp_servers": servers}))
        (self.home / "connects.json").write_text(json.dumps(list(connects)))
        return self.run_check()

    def run_check(self) -> list:
        return classified(self, check_install.check_root_mcp_servers(self.root, self.home, str(self.hermes)))

    def test_a_connected_server_with_sampling_off_and_a_reference_is_silent(self) -> None:
        self.assertEqual(self.check({"sentry": ROOT_SERVER}), [])

    def test_a_server_that_does_not_connect_is_reported(self) -> None:
        self.assertEqual(self.check({"sentry": ROOT_SERVER}, connects=()), [
            f"root mcp sentry in {self.home}: does not connect: run hermes mcp test sentry (part 10)",
        ])

    def test_sampling_on_or_missing_is_reported(self) -> None:
        for sampling in ({"enabled": True}, {}, None):
            with self.subTest(sampling=sampling):
                server = {k: v for k, v in {**ROOT_SERVER, "sampling": sampling}.items() if v is not None}
                self.assertEqual(self.check({"sentry": server}), [
                    f"root mcp sentry in {self.home}: does not set sampling.enabled to false (part 10)",
                ])

    def test_a_literal_header_is_reported_without_its_value(self) -> None:
        for value in (f"Sentry-Bearer {SECRET}", f"Bearer {SECRET} ${{SENTRY_ACCESS_TOKEN}}", f"{SECRET}${{X}}"):
            with self.subTest(value=value):
                lines = self.check({"sentry": {**ROOT_SERVER, "headers": {"Authorization": value}}})

                self.assertEqual(lines, [
                    f"root mcp sentry in {self.home}: header Authorization holds a literal value, not a ${{VAR}} reference (part 10)",
                ])
                self.assertNotIn(SECRET, "".join(lines))

    def test_a_bare_reference_is_a_reference(self) -> None:
        self.assertEqual(self.check({"sentry": {**ROOT_SERVER, "headers": {"X-Token": "${SENTRY_ACCESS_TOKEN}"}}}), [])

    def test_a_disabled_server_is_skipped(self) -> None:
        self.assertEqual(self.check({"sentry": {**ROOT_SERVER, "sampling": {}, "enabled": False}}, connects=()), [])

    def test_no_servers_is_silent(self) -> None:
        (self.home / "config.yaml").write_text('{"model": "x"}')
        self.assertEqual(self.run_check(), [])

    def test_a_missing_config_is_check_configs_to_report(self) -> None:
        self.assertEqual(self.run_check(), [])

    def test_a_reference_is_never_expanded_and_never_reported(self) -> None:
        (self.home / "config.yaml").write_text(json.dumps({"mcp_servers": {"sentry": ROOT_SERVER}}))
        (self.home / "connects.json").write_text('["sentry"]')
        with mock.patch.dict(os.environ, {"SENTRY_ACCESS_TOKEN": SECRET}):
            self.assertEqual(self.run_check(), [])

    def test_a_config_that_does_not_parse_is_one_drift_that_quotes_none_of_it(self) -> None:
        (self.home / "config.yaml").write_text(f"mcp_servers: [unclosed Sentry-Bearer {SECRET}\n")

        lines = self.run_check()

        self.assertEqual(lines, [f"root mcp {self.home}: cannot check: cannot read config.yaml: ValueError (part 10)"])

    def test_a_config_read_that_times_out_is_one_drift(self) -> None:
        (self.home / "config.yaml").write_text(json.dumps({"mcp_servers": {"sentry": ROOT_SERVER}}))
        with mock.patch.object(check_install.subprocess, "run", side_effect=subprocess.TimeoutExpired("python3", 60)):
            lines = self.run_check()

        self.assertEqual(lines, [f"root mcp {self.home}: cannot check: cannot read config.yaml: timed out (part 10)"])

    def test_a_malformed_config_is_one_drift(self) -> None:
        for servers, problem in (
            (["sentry"], "mcp_servers is not a mapping"),
            ({"sentry": "url"}, "mcp_servers.sentry is not a mapping"),
            ({"sentry": {**ROOT_SERVER, "headers": {"Authorization": 5}}}, "mcp_servers.sentry.headers.Authorization is not a string"),
            ({"sentry": {**ROOT_SERVER, "headers": ["x"]}}, "mcp_servers.sentry.headers is not a mapping"),
            ({"sentry": {**ROOT_SERVER, "sampling": True}}, "mcp_servers.sentry.sampling is not a mapping"),
        ):
            with self.subTest(servers=servers):
                self.assertEqual(self.check(servers), [f"root mcp {self.home}: cannot check: {problem} (part 10)"])


FAKE_CONFIG_HERMES = """#!/usr/bin/env python3
import json, os, sys
config = os.path.join(os.environ["HERMES_HOME"], "installed-config.json")
values = json.load(open(config)) if os.path.exists(config) else {}
if sys.argv[1:3] != ["config", "get"] or sys.argv[3] not in values:
    sys.exit("Config key not set: " + " ".join(sys.argv[1:]))
print(json.dumps(values[sys.argv[3]]))
"""

SLACK_ROUTE = {"platform": "slack", "profile": "slack"}
DIRECT = "claude-subscription-directsdk-experimental"
PROXY = "custom:proxy"
SLACK_MODEL = "example-opus"
PROXY_API = "http://127.0.0.1:8317"
PROXY_TOKEN = "proxy-token-0123456789abcdef"
ROOT_SLACK_CONFIG = {"gateway.multiplex_profiles": True, "model.provider": DIRECT}
SLACK_CONFIG = {"model.provider": DIRECT, "model.default": SLACK_MODEL}


class SlackRouteTest(unittest.TestCase):
    root = Path("/hermes")
    worker = root / "profiles" / "claude-worker"

    def route(self, routes):
        return check_install.slack_route(routes, self.root, self.worker)

    def test_one_slack_route_with_no_discriminator_names_the_profile_home(self) -> None:
        routes = [{"platform": "telegram", "profile": "other", "chat_id": "1"}, SLACK_ROUTE]

        self.assertEqual(self.route(routes), ("slack", self.root / "profiles" / "slack", ()))

    def test_the_profile_name_is_read_the_way_hermes_normalizes_it(self) -> None:
        self.assertEqual(self.route([{**SLACK_ROUTE, "profile": " Slack "}]).home, self.root / "profiles" / "slack")

    def test_a_missing_or_second_slack_route_is_a_problem(self) -> None:
        for routes, count in ((None, 0), ([], 0), ("slack", 0), ([SLACK_ROUTE, SLACK_ROUTE], 2)):
            with self.subTest(routes=routes):
                self.assertEqual(
                    self.route(routes), (None, None, (f"has {count} routes for platform slack, not one",))
                )

    def test_each_discriminator_and_bot_profile_is_a_problem(self) -> None:
        for key in ("guild_id", "chat_id", "thread_id", "user_id", "bot_profile"):
            with self.subTest(key=key):
                self.assertEqual(
                    self.route([{**SLACK_ROUTE, key: "x"}]).problems,
                    (f"sets {key} on the Slack route, so it covers only part of Slack",),
                )

    def test_a_disabled_route_is_a_problem(self) -> None:
        self.assertEqual(self.route([{**SLACK_ROUTE, "enabled": False}]).problems, ("disables the Slack route",))

    def test_a_truthy_enabled_value_is_enabled_as_hermes_reads_it(self) -> None:
        self.assertEqual(self.route([{**SLACK_ROUTE, "enabled": "true"}]).problems, ())

    def test_a_route_to_no_valid_profile_has_no_home(self) -> None:
        for profile in (None, "", "../root", 5):
            with self.subTest(profile=profile):
                self.assertEqual(
                    self.route([{**SLACK_ROUTE, "profile": profile}]),
                    (None, None, ("names no valid profile on the Slack route",)),
                )

    def test_a_route_to_the_default_or_worker_profile_has_no_home(self) -> None:
        for profile in ("default", "claude-worker"):
            with self.subTest(profile=profile):
                self.assertEqual(
                    self.route([{**SLACK_ROUTE, "profile": profile}]),
                    (profile, None, (f"routes Slack to the {profile} profile, not to a Slack profile",)),
                )


class CheckSlackProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        CheckRootMcpServersTest.setUp(self)
        self.hermes.write_text(with_interpreter(FAKE_CONFIG_HERMES))
        self.listed = [SLACK_MODEL]
        self.asked = []
        self.broker_token = PROXY_TOKEN
        self.configs(ROOT_SLACK_CONFIG, SLACK_CONFIG)
        self.yaml(self.home, {"model": {"default": SLACK_MODEL}})

    def configs(self, root: dict, slack: dict) -> None:
        (self.root / "installed-config.json").write_text(json.dumps(root))
        (self.home / "installed-config.json").write_text(json.dumps(slack))

    def yaml(self, home: Path, config: dict) -> None:
        (home / "config.yaml").write_text(json.dumps(config))

    def list_models(self, api: str, token):
        self.asked.append((api, token))
        if isinstance(self.listed, Exception):
            raise self.listed
        return self.listed

    def proxy_token(self, root: Path) -> str:
        self.assertEqual(root, self.root)
        if isinstance(self.broker_token, Exception):
            raise self.broker_token
        return self.broker_token

    def check(self, routes=(SLACK_ROUTE,)) -> list:
        route = check_install.slack_route(list(routes), self.root, self.root / "profiles" / "claude-worker")
        drifts = check_install.check_slack_profile(
            self.root, str(self.hermes), route, list_models=self.list_models, proxy_token=self.proxy_token,
        )
        return classified(self, drifts)

    def test_a_routed_profile_on_the_shared_transport_is_silent(self) -> None:
        self.assertEqual(self.check(), [])

    def test_a_missing_route_is_reported_without_reading_a_profile(self) -> None:
        self.assertEqual(self.check(routes=()), [
            f"slack profile gateway.profile_routes in {self.root}: has 0 routes for platform slack, not one (part 13)",
        ])

    def test_a_route_with_a_discriminator_is_reported_and_the_profile_still_checked(self) -> None:
        (self.home / "gateway.parked").write_text("")

        self.assertEqual(self.check(routes=({**SLACK_ROUTE, "chat_id": "C0000000001"},)), [
            f"slack profile gateway.profile_routes in {self.root}: sets chat_id on the Slack route, so it covers only part of Slack (part 13)",
            f"slack profile {self.home}: is parked, so the gateway drops every Slack message (part 13)",
        ])

    def test_multiplexing_that_is_not_true_is_reported(self) -> None:
        for value in (False, "true", None):
            with self.subTest(value=value):
                self.configs({**ROOT_SLACK_CONFIG, "gateway.multiplex_profiles": value}, SLACK_CONFIG)

                self.assertEqual(self.check(), [
                    f"slack profile gateway.multiplex_profiles in {self.root}: is not true, so the gateway serves no profile but the default (part 13)",
                ])

    def test_any_channel_override_in_the_root_home_is_reported(self) -> None:
        self.configs({**ROOT_SLACK_CONFIG, "platforms.slack.channel_overrides": {"C0000000001": {"model": "x"}}}, SLACK_CONFIG)

        self.assertEqual(self.check(), [
            f"slack profile platforms.slack.channel_overrides in {self.root}: is set; the Slack profile's model.default replaces it (part 13)",
        ])

    def test_a_native_mcp_server_in_the_root_home_is_reported(self) -> None:
        self.yaml(self.root, {"mcp_servers": {"sentry": ROOT_SERVER, "posthog": ROOT_SERVER}})

        self.assertEqual(self.check(), [
            f"slack profile mcp_servers in {self.root}: holds posthog, sentry, which belong in the Slack profile (part 13)",
        ])

    def test_a_missing_profile_is_reported(self) -> None:
        (self.home / "config.yaml").unlink()

        self.assertEqual(self.check(), [
            f"slack profile {self.home}: is missing, so the gateway drops every Slack message (part 13)",
        ])

    def test_a_parked_profile_is_reported(self) -> None:
        (self.home / "gateway.parked").write_text("")

        self.assertEqual(self.check(), [
            f"slack profile {self.home}: is parked, so the gateway drops every Slack message (part 13)",
        ])

    def test_a_standalone_profile_is_reported(self) -> None:
        for value in (True, "yes", " ON "):
            with self.subTest(value=value):
                self.yaml(self.home, {"gateway": {"standalone": value}})

                self.assertEqual(self.check(), [
                    f"slack profile {self.home}: sets gateway.standalone, so the host gateway does not serve it (part 13)",
                ])

    def test_a_profile_that_is_not_standalone_is_silent(self) -> None:
        for value in (False, "no", None):
            with self.subTest(value=value):
                self.yaml(self.home, {"gateway": {"standalone": value}})

                self.assertEqual(self.check(), [])

    def test_a_slack_block_in_the_profile_is_reported(self) -> None:
        for config in ({"platforms": {"slack": {"enabled": True}}}, {"slack": {"enabled": True}}):
            with self.subTest(config=config):
                self.yaml(self.home, config)

                self.assertEqual(self.check(), [
                    f"slack profile {self.home}: has a platforms.slack block; only the default profile runs the Slack bot (part 13)",
                ])

    def test_a_slack_token_in_the_profile_env_is_reported_without_its_value(self) -> None:
        (self.home / ".env").write_text("SLACK_BOT_TOKEN=xoxb-0123456789-secret\nOTHER=1\n")

        lines = self.check()

        self.assertEqual(lines, [
            f"slack profile SLACK_BOT_TOKEN in {self.home}/.env: is set; only the default profile holds a Slack token (part 13)",
        ])
        self.assertNotIn("xoxb", "".join(lines))

    def test_a_secret_the_config_references_but_the_env_lacks_is_reported(self) -> None:
        self.yaml(self.home, {
            "mcp_servers": {"sentry": ROOT_SERVER},
            "providers": {"proxy": {"api": PROXY_API, "key_env": "CLAUDE_PROXY_TOKEN"}},
        })

        self.assertEqual(self.check(), [
            f"slack profile CLAUDE_PROXY_TOKEN in {self.home}/.env: is not set, but the profile's config.yaml uses it (part 13)",
            f"slack profile SENTRY_ACCESS_TOKEN in {self.home}/.env: is not set, but the profile's config.yaml uses it (part 13)",
        ])

    def test_a_secret_the_env_holds_is_silent(self) -> None:
        self.yaml(self.home, {"mcp_servers": {"sentry": ROOT_SERVER}})
        (self.home / ".env").write_text(f"SENTRY_ACCESS_TOKEN={SECRET}\n")

        self.assertEqual(self.check(), [])

    def test_a_profile_with_a_secrets_command_is_not_judged_by_its_env(self) -> None:
        self.yaml(self.home, {"mcp_servers": {"sentry": ROOT_SERVER}, "secrets": {"command": {"enabled": True}}})

        self.assertEqual(self.check(), [])

    def test_a_provider_that_differs_from_the_root_homes_is_reported(self) -> None:
        self.configs(ROOT_SLACK_CONFIG, {**SLACK_CONFIG, "model.provider": "openrouter"})

        self.assertEqual(self.check(), [
            f"slack profile model.provider in {self.home}: differs from the root home's, so Slack turns leave the factory's transport (part 13)",
        ])

    def test_an_unset_model_is_reported(self) -> None:
        for model in (None, "", " "):
            with self.subTest(model=model):
                self.configs(ROOT_SLACK_CONFIG, {**SLACK_CONFIG, "model.default": model})

                self.assertEqual(self.check(), [
                    f"slack profile model.default in {self.home}: is not set to the operator's Slack model (part 13)",
                ])

    def proxy_configs(self, **slack) -> None:
        self.configs({**ROOT_SLACK_CONFIG, "model.provider": PROXY}, {
            **SLACK_CONFIG, "model.provider": PROXY, "providers.proxy.api": PROXY_API,
            "providers.proxy.key_env": "CLAUDE_PROXY_TOKEN", **slack,
        })

    def test_a_model_the_proxy_lists_is_silent_and_asked_with_the_brokers_token(self) -> None:
        self.proxy_configs()

        self.assertEqual(self.check(), [])
        self.assertEqual(self.asked, [(PROXY_API, PROXY_TOKEN)])

    def test_the_profiles_own_token_is_used_before_the_brokers(self) -> None:
        self.proxy_configs()
        self.broker_token = OSError("not asked")
        (self.home / ".env").write_text("CLAUDE_PROXY_TOKEN=own-token\n")

        self.assertEqual(self.check(), [])
        self.assertEqual(self.asked, [(PROXY_API, "own-token")])

    def test_a_model_the_proxy_does_not_list_is_reported(self) -> None:
        self.proxy_configs()
        self.listed = ["example-sonnet"]

        self.assertEqual(self.check(), [
            f"slack profile model.default in {self.home}: is {SLACK_MODEL}, which {PROXY_API}/v1/models does not list (part 13)",
        ])

    def test_a_proxy_that_cannot_be_asked_is_advisory_and_names_no_token(self) -> None:
        self.proxy_configs()
        self.listed = OSError("connection refused")

        lines = self.check()

        self.assertEqual(lines, [
            f"slack profile model.default in {self.home}: cannot be checked: {PROXY_API} lists no models: connection refused (part 13)",
        ])
        self.assertNotIn(PROXY_TOKEN, "".join(lines))

    def test_a_proxy_reply_cut_short_is_advisory(self) -> None:
        self.proxy_configs()
        self.listed = http.client.IncompleteRead(b"")

        self.assertEqual(self.check(), [
            f"slack profile model.default in {self.home}: cannot be checked: {PROXY_API} lists no models: "
            "IncompleteRead(0 bytes read) (part 13)",
        ])

    def test_a_key_env_that_is_not_a_name_is_reported(self) -> None:
        self.proxy_configs(**{"providers.proxy.key_env": ["CLAUDE_PROXY_TOKEN"]})

        self.assertEqual(self.check(), [f"slack profile providers.proxy.key_env in {self.home}: is not a variable name (part 13)"])

    def test_a_proxy_provider_without_an_api_is_reported(self) -> None:
        self.proxy_configs(**{"providers.proxy.api": ""})

        self.assertEqual(self.check(), [f"slack profile providers.proxy.api in {self.home}: is not set (part 13)"])

    def test_a_config_it_cannot_read_is_one_advisory_drift(self) -> None:
        (self.home / "config.yaml").write_text(f"model: [unclosed {SECRET}\n")

        lines = self.check()

        self.assertEqual(lines, [f"slack profile {self.home}: cannot check: cannot read config.yaml: ValueError (part 13)"])


class BrokerProxyTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        client = self.root / check_install.BROKER_CLIENT
        client.parent.mkdir(parents=True)
        shutil.copy(REPO / check_install.BROKER_CLIENT, client)

    def broker(self, reply: dict) -> None:
        broker = FakeBroker(self.root / "factory-uploads.sock", proxy_token=[reply])
        self.addCleanup(broker.close)

    def test_the_brokers_token_is_returned(self) -> None:
        self.broker({"ok": True, "token": PROXY_TOKEN})

        self.assertEqual(check_install.broker_proxy_token(self.root), PROXY_TOKEN)

    def test_a_reply_without_a_token_is_an_os_error(self) -> None:
        self.broker({"ok": True})

        with self.assertRaisesRegex(OSError, "^the gateway's broker gave no proxy token$"):
            check_install.broker_proxy_token(self.root)


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


def install_fake_hermes(root: Path) -> Path:
    """A Hermes checkout, store Python, and environment under `root`. Returns the checkout."""
    tree = root / "hermes-agent"
    tools = tree / "tools"
    tools.mkdir(parents=True)
    for name, source in (("__init__", ""), ("registry", FAKE_REGISTRY), ("process_registry", ""),
                         ("terminal_tool", FAKE_TERMINAL_TOOL), ("kanban_tools", "")):
        (tools / f"{name}.py").write_text(source)
    (tools / "registered.json").write_text(json.dumps(
        {"terminal": "terminal", "process_manage": "terminal", "kanban_schedule": "kanban"}))
    python = root / "tools" / "python-9" / "bin"
    python.mkdir(parents=True)
    (python / "python3").symlink_to(sys.executable)
    (root / "tools" / "facts.json").write_text(json.dumps({"packages": {"python": {"entry": "python-9"}}}))
    site = root / "installs" / "key" / "environments" / "env" / "venv" / "lib" / "python9.9" / "site-packages"
    site.mkdir(parents=True)
    (site / "fake_dependency.py").write_text("")
    (site / "ruamel" / "yaml").mkdir(parents=True)
    (site / "ruamel" / "__init__.py").write_text("")
    (site / "ruamel" / "yaml" / "__init__.py").write_text(FAKE_RUAMEL)
    (root / "installs" / "key" / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(site.parents[2])}}}))
    return tree


class CheckWaitingTriageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "hermes"
        self.path = self.root / "kanban" / "boards" / "triage" / "kanban.db"
        self.path.parent.mkdir(parents=True)
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE tasks (id, status, block_kind)")
        self.db.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id, author, body, created_at)")
        self.db.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id, kind, payload, created_at)")
        self.plugin = PLUGIN

    def card(self, id: str, status: str, block_kind: str | None = None, comments=(), parks=()) -> None:
        self.db.execute("INSERT INTO tasks VALUES (?, ?, ?)", (id, status, block_kind))
        for body, at in comments:
            self.db.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'claude-worker', ?, ?)",
                            (id, body, at))
        for at in parks:
            self.db.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, ?)",
                            (id, json.dumps({"reason": "Needs input: which?\n\ncomments_seen: 0"}), at))
        self.db.commit()

    def check(self) -> list:
        return classified(self, check_install.check_waiting_triage(self.root, self.plugin))

    def test_each_card_that_waits_for_input_the_old_way_is_reported(self) -> None:
        self.card("t_blocked", "blocked", "needs_input")
        self.card("t_parked", "triage", "needs_input")

        self.assertEqual(self.check(), [
            "waiting triage card t_blocked: is blocked with block kind needs_input, but a triage round waits for a "
            "reply in scheduled (part 15, check 27)",
            "waiting triage card t_parked: is triage with block kind needs_input, but a triage round waits for a "
            "reply in scheduled (part 15, check 27)",
        ])

    def test_a_waiting_card_a_real_loop_and_another_block_are_silent(self) -> None:
        self.card("t_waits", "scheduled", "needs_input", comments=[("Linear reply: earlier", 50)], parks=[100])
        self.card("t_loop", "triage", "transient")
        self.card("t_stuck", "blocked", "capability")

        self.assertEqual(self.check(), [])

    def test_a_round_not_posted_after_the_newest_park_is_reported(self) -> None:
        self.card("t_failed", "scheduled", comments=[("Triage round not posted: failed: RuntimeError", 100)], parks=[100])
        self.card("t_unknown", "scheduled", comments=[("Triage round not posted: unknown", 300)], parks=[100, 300])
        self.card("t_reposted", "scheduled", comments=[("Triage round not posted: unknown", 100)], parks=[100, 400])

        self.assertEqual(self.check(), [
            "waiting triage card t_failed: waits in scheduled, but its round may never have reached Linear: "
            "Triage round not posted: failed: RuntimeError (part 15, check 27)",
            "waiting triage card t_unknown: waits in scheduled, but its round may never have reached Linear: "
            "Triage round not posted: unknown (part 15, check 27)",
        ])

    def test_an_unreadable_board_gives_a_row(self) -> None:
        self.db.execute("DROP TABLE task_comments")

        [row] = self.check()
        self.assertEqual(row, f"waiting triage card check {self.path}: cannot be read: no such table: task_comments (part 15, check 27)")

    def test_without_a_triage_board_nothing_runs(self) -> None:
        self.db.close()
        self.path.unlink()

        self.assertEqual(self.check(), [])


FAKE_APPROVAL = """
def detect_dangerous_command(command):
    if command.startswith("python3 ") and "<<" in command:
        return True, "script execution via heredoc", "script execution via heredoc"
    return False, None, None
"""
HEREDOC_SKILL = """1. Pipe the reply into the card:

   ```sh
   python3 /srv/hermes/card_transition.py --board triage triage-reply <task id> <<'LINEAR_REPLY'
   <reply>
   LINEAR_REPLY
   ```

2. Then run `python3 /srv/hermes/card_transition.py reopen <task id> --comment 'x'` and `hermes kanban show <task id>`.
"""


class CheckPromptCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.tree = install_fake_hermes(self.root)
        (self.tree / "tools" / "approval.py").write_text(FAKE_APPROVAL)

    def check(self, skills: dict) -> list:
        return classified(self, check_install.check_prompt_commands(self.root, skills))

    def test_the_commands_are_each_fenced_shell_block_and_each_inline_command(self) -> None:
        self.assertEqual(check_install.prompt_commands(HEREDOC_SKILL), [
            "python3 /srv/hermes/card_transition.py --board triage triage-reply <task id> <<'LINEAR_REPLY'\n<reply>\nLINEAR_REPLY",
            "python3 /srv/hermes/card_transition.py reopen <task id> --comment 'x'",
            "hermes kanban show <task id>",
        ])

    def test_a_flagged_heredoc_step_fails(self) -> None:
        self.assertEqual(self.check({"factory-linear-triage": HEREDOC_SKILL, "factory-github-triage": "Run `hermes kanban show <task id>`."}), [
            "prompt command factory-linear-triage: Hermes flags \"python3 /srv/hermes/card_transition.py --board triage "
            "triage-reply <task id> <<'LINEAR_REPLY'\" as script execution via heredoc, so a turn with no person present "
            "waits for an approval or refuses it",
        ])

    def test_a_prompt_whose_commands_hermes_allows_is_silent(self) -> None:
        self.assertEqual(self.check({"factory-linear-triage": "Call `linear_card_reply`, then run `hermes kanban show <task id>`."}), [])

    def test_a_hermes_that_cannot_be_imported_fails_and_says_so(self) -> None:
        (self.tree / "tools" / "approval.py").write_text("raise ImportError('no module named fake')\n")

        [row] = self.check({"factory-linear-triage": HEREDOC_SKILL})
        self.assertTrue(row.startswith("prompt command factory-linear-triage: cannot ask Hermes which commands it flags: "), row)
        self.assertIn("no module named fake", row)


class CheckHermesToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "root"
        self.tree = install_fake_hermes(self.root)
        self.register({"terminal": "terminal", "process_manage": "terminal", "kanban_schedule": "kanban", "read_file": "file"})
        self.plugin = self.tmp / "worker" / "plugins" / "blocker-order" / "__init__.py"
        shutil.copytree(PLUGIN.parent, self.plugin.parent, ignore=shutil.ignore_patterns("tests", "__pycache__"))

    def register(self, tools: dict) -> None:
        (self.tree / "tools" / "registered.json").write_text(json.dumps(tools))

    def check(self) -> list:
        return classified(self, check_install.check_hermes_tools(self.root, self.plugin))

    def test_a_hermes_that_registers_the_watched_tools_is_silent(self) -> None:
        self.assertEqual(self.check(), [])

    def test_the_legacy_process_name_alone_still_counts(self) -> None:
        self.register({"terminal": "terminal", "process": "terminal", "kanban_schedule": "kanban"})

        self.assertEqual(self.check(), [])

    def test_a_renamed_process_tool_names_the_missing_tool(self) -> None:
        self.register({"terminal": "terminal", "process_run": "terminal", "kanban_schedule": "kanban", "read_file": "file"})

        self.assertEqual(self.check(), [
            "hermes tool blocker-order: Hermes registers none of process_manage, process, which the plugin watches; "
            "its terminal and kanban toolsets register kanban_schedule, process_run, terminal (PROCESS_TOOLS in plugins/blocker-order/__init__.py)",
        ])

    def test_a_missing_terminal_tool_is_named(self) -> None:
        self.register({"process_manage": "terminal", "kanban_schedule": "kanban"})

        self.assertEqual(self.check(), [
            "hermes tool blocker-order: Hermes registers no terminal, which the plugin needs; "
            "its terminal and kanban toolsets register kanban_schedule, process_manage (REQUIRED_TOOLS in plugins/blocker-order/__init__.py)",
        ])

    def test_a_missing_kanban_schedule_tool_is_named(self) -> None:
        self.register({"terminal": "terminal", "process_manage": "terminal", "kanban_block": "kanban"})

        self.assertEqual(self.check(), [
            "hermes tool blocker-order: Hermes registers no kanban_schedule, which the plugin needs; "
            "its terminal and kanban toolsets register kanban_block, process_manage, terminal "
            "(REQUIRED_TOOLS in plugins/blocker-order/__init__.py)",
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


class ImportPluginsTest(unittest.TestCase):
    def run_helper(self, plugins: dict) -> dict:
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        dirs = []
        for name, files in plugins.items():
            for path, text in files.items():
                (tmp / "plugins" / name / path).parent.mkdir(parents=True, exist_ok=True)
                (tmp / "plugins" / name / path).write_text(text)
            dirs.append(str(tmp / "plugins" / name))
        (tmp / "checkout").mkdir()
        (tmp / "site").mkdir()
        (tmp / "site" / "dependency.py").write_text("VALUE = 1\n")
        proc = subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPTS / "import-plugins"), str(tmp / "checkout"), str(tmp / "site"), *dirs],
            cwd=tmp, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_each_plugin_maps_to_null_or_its_error(self) -> None:
        result = self.run_helper({
            "fine": {"__init__.py": "import dependency\n"},
            "relative": {"__init__.py": "from . import helper\nfrom .helper import VALUE\n", "helper.py": "VALUE = 2\n"},
            "broken": {"__init__.py": "raise ValueError('no good')\n"},
            "missing-import": {"__init__.py": "import nothing_here\n"},
            "broken-submodule": {"__init__.py": "from . import helper\n", "helper.py": "1 / 0\n"},
            "exits": {"__init__.py": "raise SystemExit(3)\n"},
        })

        self.assertEqual(result, {
            "fine": None,
            "relative": None,
            "broken": "ValueError: no good",
            "missing-import": "ModuleNotFoundError: No module named 'nothing_here'",
            "broken-submodule": "ZeroDivisionError: division by zero",
            "exits": "SystemExit: 3",
        })

    def test_register_is_never_called(self) -> None:
        result = self.run_helper({"p": {"__init__.py": "def register(ctx):\n    raise RuntimeError('called')\n"}})

        self.assertEqual(result, {"p": None})


class CheckOperatorsTest(unittest.TestCase):
    def check(self, settings) -> list:
        return [str(drift) for drift in check_install.check_operators(settings, Path("/worker"))]

    def test_an_empty_or_unset_list_is_one_advisory_naming_the_setting(self) -> None:
        advisory = (
            "config operator_user_ids in /worker: is empty or not set, so an operator's replies on issues assigned to others are context "
            "only until the operators' Linear user ids are added to the setting from the factory's private configuration record"
        )

        self.assertEqual([self.check({"operator_user_ids": []}), self.check({})], [[advisory], [advisory]])
        self.assertIs(check_install.check_operators({}, Path("/worker"))[0].remedy, check_install.ADVISORY)

    def test_a_configured_list_is_silent(self) -> None:
        self.assertEqual(self.check({"operator_user_ids": ["user-operator-1"]}), [])

    def test_settings_that_cannot_be_read_are_left_to_the_other_checks(self) -> None:
        self.assertEqual(self.check(None), [])


class CheckArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(artifact_login)
        self.addCleanup(artifact_tools, "Artifact", "Read")
        ARTIFACT_LOG.unlink(missing_ok=True)

    def check(self, settings: dict | None = None) -> list:
        drifts = check_install.check_artifact({"claude_artifact": ARTIFACT_SETTINGS} if settings is None else settings)
        return [str(drift) for drift in drifts]

    def test_a_team_login_whose_claude_offers_the_artifact_tool_is_silent_and_never_calls_the_model(self) -> None:
        self.assertEqual(self.check(), [])
        (run,) = [json.loads(line) for line in ARTIFACT_LOG.read_text().splitlines()]
        self.assertEqual((run["token"], run["argv"][:4]), (ARTIFACT_TOKEN, ["-p", "--output-format", "stream-json", "--verbose"]))

    def test_each_login_and_tool_problem_is_named_without_a_secret(self) -> None:
        artifact_login(disabled=True)
        disabled = self.check()
        artifact_login(organization="99999999-8888-7777-6666-555555555555")
        other = self.check()
        for path in ARTIFACT_AUTH.iterdir():
            path.unlink()
        missing = self.check()
        artifact_login()
        artifact_tools("Read")
        no_tool = self.check()
        unset = self.check({})

        self.assertEqual([disabled, other, missing, no_tool], [
            ["artifact login claude_artifact: the configured organization's proxy login is disabled (part 18)"],
            ["artifact login claude_artifact: no proxy login belongs to the configured organization (part 18)"],
            ["artifact login claude_artifact: the proxy's auth directory holds no login (part 18)"],
            ["artifact tool claude_artifact: the pinned claude offers no Artifact tool (part 18)"],
        ])
        self.assertEqual(len(unset), 1)
        printed = json.dumps([disabled, other, missing, no_tool, unset])
        for secret in (ARTIFACT_TOKEN, "refresh-value", "agent@example.com", ARTIFACT_ORG, str(ARTIFACT_AUTH)):
            self.assertNotIn(secret, printed)


class CheckServicesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        self.launchd = Launchd(tmp)
        env = mock.patch.dict(os.environ, self.launchd.env)
        env.start()
        self.addCleanup(env.stop)

    def check(self) -> list:
        return classified(self, check_install.check_services(self.launchd.root, str(self.launchd.launchctl)))

    def test_installed_running_services_are_silent(self) -> None:
        self.assertEqual(self.check(), [])

    def test_agent_loaded_from_a_login_session(self) -> None:
        uid = os.getuid()
        self.launchd.states({
            "system/com.example.cloudflared": "running", f"gui/{uid}/ai.hermes.gateway": "running",
            f"gui/{uid}/ai.patinaproject.factory.gateway-watch": "not running",
            f"gui/{uid}/{GH_LOGIN_LABEL}": "not running",
            f"gui/{uid}/ai.patinaproject.factory.auto-deploy": "not running",
            f"gui/{uid}/{ARTIFACT_LABEL}": "running",
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
            "service gh login refresh: is not loaded in launchd (part 10)",
            "service auto deploy: is not loaded in launchd (part 12)",
            "service claude artifact: is not loaded in launchd (part 18)",
        ])

    def test_a_missing_watch_job_is_reported(self) -> None:
        self.launchd.watch.unlink()

        self.assertEqual(self.check(), [
            "service gateway watch: no plist matches ~/Library/LaunchAgents/ai.patinaproject.factory.gateway-watch.plist (part 12)",
        ])

    def test_a_missing_login_refresh_job_is_reported(self) -> None:
        self.launchd.gh_login.unlink()

        self.assertEqual(self.check(), [
            "service gh login refresh: no plist matches "
            "~/Library/LaunchAgents/ai.patinaproject.factory.gh-login.plist (part 10)",
        ])

    def login_refresh_with(self, **fields) -> None:
        self.launchd.plist(
            f"{str(self.launchd.home).lstrip('/')}/Library/LaunchAgents/{GH_LOGIN_LABEL}.plist", GH_LOGIN_LABEL, **fields
        )
        uid = os.getuid()
        self.launchd.states({
            "system/com.example.cloudflared": "running", f"user/{uid}/ai.hermes.gateway": "running",
            f"user/{uid}/ai.patinaproject.factory.gateway-watch": "not running", f"user/{uid}/{GH_LOGIN_LABEL}": "waiting",
            f"user/{uid}/ai.patinaproject.factory.auto-deploy": "not running",
            f"user/{uid}/{ARTIFACT_LABEL}": "running",
        })

    def test_a_login_refresh_job_without_its_environment_is_reported_by_name_only(self) -> None:
        self.login_refresh_with(StartInterval=900, EnvironmentVariables={"GH_CONFIG_DIR": "/w/gh", "TOKEN": "s3cr3t"})

        self.assertEqual(self.check(), ["service gh login refresh: does not set HERMES_HOME (part 10)"])

        self.login_refresh_with(StartInterval=900)

        self.assertEqual(self.check(), ["service gh login refresh: does not set GH_CONFIG_DIR, HERMES_HOME (part 10)"])

    def test_a_login_refresh_job_that_runs_at_another_interval_is_reported(self) -> None:
        self.login_refresh_with(**{**GH_LOGIN_FIELDS, "StartInterval": 1800})

        self.assertEqual(self.check(), ["service gh login refresh: does not run every 900 seconds (part 10)"])

    def test_the_auto_deploy_job_is_loaded_between_runs_and_runs_at_load(self) -> None:
        uid = os.getuid()
        states = {
            "system/com.example.cloudflared": "running", f"user/{uid}/ai.hermes.gateway": "running",
            f"user/{uid}/ai.patinaproject.factory.gateway-watch": "not running", f"user/{uid}/{GH_LOGIN_LABEL}": "not running",
            f"user/{uid}/{ARTIFACT_LABEL}": "running",
        }
        self.launchd.states({**states, f"user/{uid}/ai.patinaproject.factory.auto-deploy": "running"})
        running = self.check()
        self.launchd.states(states)
        unloaded = self.check()
        self.launchd.plist(
            f"{str(self.launchd.home).lstrip('/')}/Library/LaunchAgents/ai.patinaproject.factory.auto-deploy.plist",
            "ai.patinaproject.factory.auto-deploy", RunAtLoad=False, **AUTO_DEPLOY_FIELDS,
        )
        self.launchd.states({**states, f"user/{uid}/ai.patinaproject.factory.auto-deploy": "not running"})
        not_at_load = self.check()
        self.launchd.plist(
            f"{str(self.launchd.home).lstrip('/')}/Library/LaunchAgents/ai.patinaproject.factory.auto-deploy.plist",
            "ai.patinaproject.factory.auto-deploy", EnvironmentVariables=AUTO_DEPLOY_FIELDS["EnvironmentVariables"],
        )
        no_interval = self.check()
        self.launchd.deploy.unlink()

        self.assertEqual((running, unloaded, not_at_load, no_interval, self.check()), (
            [],
            ["service auto deploy: is not loaded in launchd (part 12)"],
            ["service auto deploy: does not run at load (part 12)"],
            ["service auto deploy: does not run every 300 seconds (part 12)"],
            ["service auto deploy: no plist matches ~/Library/LaunchAgents/ai.patinaproject.factory.auto-deploy.plist (part 12)"],
        ))

    def test_an_auto_deploy_job_without_its_environment_is_reported_by_name_only(self) -> None:
        uid = os.getuid()
        self.launchd.plist(
            f"{str(self.launchd.home).lstrip('/')}/Library/LaunchAgents/{AUTO_DEPLOY_LABEL}.plist", AUTO_DEPLOY_LABEL,
            StartInterval=300, EnvironmentVariables={"HERMES_HOME": "/h", "TOKEN": "s3cr3t"},
        )
        self.launchd.states({
            "system/com.example.cloudflared": "running", f"user/{uid}/ai.hermes.gateway": "running",
            f"user/{uid}/ai.patinaproject.factory.gateway-watch": "not running", f"user/{uid}/{GH_LOGIN_LABEL}": "not running",
            f"user/{uid}/{AUTO_DEPLOY_LABEL}": "not running",
            f"user/{uid}/{ARTIFACT_LABEL}": "running",
        })

        self.assertEqual(self.check(), ["service auto deploy: does not set GH_CONFIG_DIR, PATH (part 12)"])

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
            f"gui/{uid}/{GH_LOGIN_LABEL}": "not running",
            f"gui/{uid}/ai.patinaproject.factory.auto-deploy": "not running",
            f"gui/{uid}/{ARTIFACT_LABEL}": "running",
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
