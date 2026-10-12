from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tests.support import SCRIPTS, git

ROUTE_KEY = "platforms.webhook.extra.routes.linear"
RATE_LIMIT_KEY = "platforms.webhook.extra.rate_limit"
ROUTE = {"events": ["AgentSessionEvent"], "secret": "${LINEAR_WEBHOOK_SECRET}", "prompt": "triage"}

RENDER = f"""import json
print(json.dumps({{{RATE_LIMIT_KEY!r}: 600, {ROUTE_KEY!r}: {ROUTE!r}}}))
"""

FAKE_HERMES = """#!/usr/bin/env python3
import os, sys
key = sys.argv[3]
if key == "dashboard":
    path = os.path.join(os.environ["HERMES_HOME"], "installed-dashboard.json")
else:
    path = os.path.join(os.environ["HERMES_HOME"], "installed-config", key + ".json")
if not os.path.exists(path):
    sys.exit("Config key not set: " + sys.argv[3])
print(open(path).read())
"""

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
        self.install(self.root, f"installed-config/{ROUTE_KEY}.json", json.dumps({**ROUTE, "secret": "abc...1234"}))
        self.install(self.root, f"installed-config/{RATE_LIMIT_KEY}.json", "600")
        self.install(self.root, "logs/agent.log", "")

        self.issuer = self.tmp / "issuer"
        self.discovery(["authorization_code_with_pkce", "refresh_tokens"])
        self.dashboard(scopes="openid profile email offline_access")

        self.hermes = self.tmp / "bin" / "fake-hermes"
        self.install(self.tmp, "bin/fake-hermes", FAKE_HERMES)
        os.chmod(self.hermes, 0o755)

    def install(self, home: Path, path: str, text: str) -> None:
        (home / path).parent.mkdir(parents=True, exist_ok=True)
        (home / path).write_text(text)

    def discovery(self, grants: list) -> None:
        self.install(
            self.issuer, ".well-known/openid-configuration", json.dumps({"grant_types_supported": grants})
        )

    def dashboard(self, *, public_url: str = "https://factory.example", issuer: str | None = None, **self_hosted) -> None:
        self_hosted = {"issuer": self.issuer.as_uri() if issuer is None else issuer, "client_id": "cid", **self_hosted}
        config = {"public_url": public_url, "oauth": {"client_id": "", "self_hosted": self_hosted}}
        self.install(self.root, "installed-dashboard.json", json.dumps(config))

    def check(self, *args: str) -> tuple:
        env = {**os.environ, "HERMES_HOME": str(self.worker), "HERMES_BIN": str(self.hermes)}
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
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "HERMES_HOME": str(self.worker)}
        proc = subprocess.run(
            ["/usr/bin/env", "python3", str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True
        )

        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_worker_home_outside_profiles_is_refused(self) -> None:
        env = {**os.environ, "HERMES_HOME": str(self.root), "HERMES_BIN": str(self.hermes)}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check-install")], env=env, capture_output=True, text=True)

        self.assertEqual((proc.returncode, proc.stdout), (2, ""))


if __name__ == "__main__":
    unittest.main()
