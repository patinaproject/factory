from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO as CHECKOUT, SCRIPTS, FakeBroker, with_interpreter

REPO = "example-org/example-repo"
SHA = "a" * 40
ALL_PERMISSIONS = {
    "contents": "write", "pull_requests": "write", "issues": "write", "checks": "read",
    "statuses": "read", "actions": "write", "metadata": "read",
}
TOKEN = "ghs_FAKEinstallationtoken"


def minted(permissions: dict | None) -> dict:
    reply = {"ok": True, "token": TOKEN, "expires_at": 4102444800.0}
    return reply if permissions is None else {**reply, "permissions": permissions}

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
state = json.load(open(os.environ["FAKE_GH_STATE"]))
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({"argv": args, "env": {k: os.environ.get(k) for k in ("GH_CONFIG_DIR",)}}) + "\n")
endpoint = args[1]
denied = state.get("deny", [])

def refuse(permission, graphql=False):
    if permission in denied:
        if graphql:
            sys.exit("GraphQL: Resource not accessible by integration (repository.pullRequest)")
        sys.exit("gh: Resource not accessible by integration (HTTP 403)")

repo = state["repo"]
if endpoint == "apps/example-app":
    print(json.dumps({"slug": "example-app", "permissions": state["app_permissions"], "events": state["app_events"]}))
elif endpoint == "graphql":
    refuse("graphql", graphql=True)
    print(json.dumps({"data": {"repository": {"pullRequest": {"headRefOid": state["sha"]}}}}))
elif endpoint == f"repos/{repo}/pulls?state=all&per_page=1":
    refuse("pull_requests")
    print(json.dumps(state["pulls"]))
elif endpoint == f"repos/{repo}":
    print(json.dumps({"default_branch": "main"}))
elif endpoint == f"repos/{repo}/commits/main":
    refuse("contents")
    print(json.dumps({"sha": state["sha"]}))
elif endpoint.startswith(f"repos/{repo}/rules/branches/"):
    print(json.dumps([]))
elif endpoint == f"repos/{repo}/commits/{state['sha']}/check-runs?per_page=1":
    refuse("checks")
    print(json.dumps({"total_count": 0, "check_runs": []}))
elif endpoint == f"repos/{repo}/commits/{state['sha']}/statuses?per_page=1":
    refuse("statuses")
    print(json.dumps([]))
elif endpoint == f"repos/{repo}/activity?activity_type=branch_creation&per_page=1":
    refuse("activity")
    print(json.dumps([]))
elif endpoint == f"repos/{repo}/actions/runs?head_sha={state['sha']}&per_page=1":
    refuse("actions")
    print(json.dumps({"total_count": 0, "workflow_runs": []}))
else:
    sys.exit("gh: Not Found (HTTP 404)")
"""


class CheckGithubAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.gh = self.tmp / "fake-gh"
        self.gh.write_text(with_interpreter(FAKE_GH))
        os.chmod(self.gh, 0o755)
        self.log = self.tmp / "gh-calls.jsonl"
        root = self.tmp / "root"
        self.worker = root / "profiles" / "claude-worker"
        plugin = self.worker / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "broker_client.py").symlink_to(CHECKOUT / "plugins" / "linear-agent-session" / "broker_client.py")
        (self.worker / "scripts").mkdir()
        for name in ("check-github-access", "factory_settings.py", "app_events.py"):
            shutil.copy(SCRIPTS / name, self.worker / "scripts" / name)
        self.broker_path = root / "factory-uploads.sock"
        self.installation = dict(ALL_PERMISSIONS)
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps({
            "repositories": [{"full_name": REPO, "path": "/srv/example-repo", "routing": "default"}],
            "github": {"login": "example-app[bot]"},
        }))
        self.state = {
            "repo": REPO,
            "sha": SHA,
            "app_permissions": dict(ALL_PERMISSIONS),
            "app_events": ["check_run", "issues", "pull_request", "push"],
            "pulls": [{"number": 7, "head": {"sha": SHA}, "base": {"ref": "main"}}],
            "deny": [],
        }

    def check(self, env: dict | None = None, broker: list | None = None) -> tuple:
        if broker is None:
            broker = [minted(self.installation)]
        self.broker = FakeBroker(self.broker_path, github_token=broker) if broker else None
        if self.broker:
            self.addCleanup(self.broker.close)
        state = self.tmp / "state.json"
        state.write_text(json.dumps(self.state))
        base = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        base.update(GH_BIN=str(self.gh), GH_CONFIG_DIR=str(self.tmp / "gh"), FAKE_GH_STATE=str(state),
                    FAKE_GH_LOG=str(self.log))
        base.update(env or {})
        proc = subprocess.run(
            [sys.executable, str(self.worker / "scripts" / "check-github-access"), "--settings-json", str(self.settings)],
            env=base, capture_output=True, text=True,
        )
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        return proc.returncode, proc.stdout.splitlines(), proc.stderr

    def endpoints(self) -> list:
        return [json.loads(line)["argv"][1] for line in self.log.read_text().splitlines()]

    def test_full_access_is_silent(self) -> None:
        status, lines, _ = self.check()

        self.assertEqual((status, lines), (0, []))
        self.assertEqual(self.broker.requests, ["github_token"])
        self.assertEqual(self.endpoints(), [
            "apps/example-app",
            f"repos/{REPO}/pulls?state=all&per_page=1",
            "graphql",
            f"repos/{REPO}/rules/branches/main",
            f"repos/{REPO}/commits/{SHA}/check-runs?per_page=1",
            f"repos/{REPO}/commits/{SHA}/statuses?per_page=1",
            f"repos/{REPO}/activity?activity_type=branch_creation&per_page=1",
            f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=1",
        ])

    def test_missing_commit_statuses_names_the_permission(self) -> None:
        self.state["deny"] = ["statuses"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            f"{REPO}: reading commit statuses of aaaaaaa refused (HTTP 403): "
            'grant the App "Commit statuses: Read-only" and accept it on the installation',
        ])

    def test_refused_branch_creation_activity_names_the_permission(self) -> None:
        self.state["deny"] = ["activity"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            f"{REPO}: reading the branch creation activity refused (HTTP 403): "
            'grant the App "Contents: Read and write" and accept it on the installation',
        ])

    def test_missing_checks_names_the_permission(self) -> None:
        self.state["deny"] = ["checks"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            f"{REPO}: reading check runs of aaaaaaa refused (HTTP 403): "
            'grant the App "Checks: Read-only" and accept it on the installation',
        ])

    def test_refused_workflow_runs_names_the_actions_permission(self) -> None:
        self.state["deny"] = ["actions"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            f"{REPO}: reading workflow runs of aaaaaaa refused (HTTP 403): "
            'grant the App "Actions: Read and write" and accept it on the installation',
        ])

    def test_read_only_actions_cannot_rerun_failed_jobs(self) -> None:
        self.state["app_permissions"] = {**ALL_PERMISSIONS, "actions": "read"}

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, ['App example-app: grant "Actions: Read and write", it has read'])

    def test_an_installation_that_never_accepted_actions_write_cannot_rerun_failed_jobs(self) -> None:
        self.installation["actions"] = "read"

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            'Installation of example-app: accept "Actions: Read and write" on the installation, it has read',
        ])

    def test_a_permission_the_app_lacks_is_reported_once_as_a_grant(self) -> None:
        self.state["app_permissions"] = {**ALL_PERMISSIONS, "actions": "read"}
        self.installation["actions"] = "read"
        del self.installation["checks"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            'App example-app: grant "Actions: Read and write", it has read',
            'Installation of example-app: accept "Checks: Read-only" on the installation, it has no access',
        ])

    def test_an_app_that_does_not_subscribe_to_push_is_reported(self) -> None:
        self.state["app_events"] = ["check_run", "issues", "pull_request"]

        status, lines, _ = self.check()

        self.assertEqual((status, lines), (1, ['App example-app: subscribe to the "Push" event, which starts auto-deploy']))

    def test_a_broker_that_does_not_report_permissions_is_reported(self) -> None:
        status, lines, _ = self.check(broker=[minted(None)])

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            "Installation of example-app: the gateway's broker does not report the installation's permissions; "
            "deploy the current factory to the broker",
        ])

    def test_a_broker_that_refuses_the_token_is_reported(self) -> None:
        refusal = {"ok": False, "state": "unavailable", "message": "The gateway holds no GitHub App private key."}

        status, lines, _ = self.check(broker=[refusal])

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            "Installation of example-app: cannot read its permissions: the gateway's broker refused github_token: "
            "The gateway holds no GitHub App private key.",
        ])

    def test_a_home_without_the_broker_client_is_reported(self) -> None:
        (self.worker / "plugins" / "linear-agent-session" / "broker_client.py").unlink()

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            "Installation of example-app: cannot read its permissions: No module named 'broker_client'",
        ])

    def test_refused_pull_requests_still_checks_the_default_branch(self) -> None:
        self.state["deny"] = ["pull_requests", "statuses"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            f"{REPO}: reading pull requests refused (HTTP 403): "
            'grant the App "Pull requests: Read and write" and accept it on the installation',
            f"{REPO}: reading commit statuses of aaaaaaa refused (HTTP 403): "
            'grant the App "Commit statuses: Read-only" and accept it on the installation',
        ])

    def test_refused_graphql_read_names_pull_requests(self) -> None:
        self.state["deny"] = ["graphql"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            f"{REPO}: reading pull request #7 through GraphQL refused (HTTP 403): "
            'grant the App "Pull requests: Read and write" and accept it on the installation',
        ])

    def test_repository_without_pull_requests_reads_the_default_branch_head(self) -> None:
        self.state["pulls"] = []
        self.state["deny"] = ["statuses"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertIn(f"repos/{REPO}/commits/main", self.endpoints())
        self.assertEqual(len(lines), 1)
        self.assertIn('"Commit statuses: Read-only"', lines[0])

    def test_app_permission_below_the_requirement_is_reported(self) -> None:
        self.state["app_permissions"] = {**ALL_PERMISSIONS, "issues": "read"}
        del self.state["app_permissions"]["statuses"]

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            'App example-app: grant "Issues: Read and write", it has read',
            'App example-app: grant "Commit statuses: Read-only", it has no access',
        ])

    def test_uninstalled_repository_is_reported_as_not_found(self) -> None:
        self.state["repo"] = "example-org/other"

        status, lines, _ = self.check()

        self.assertEqual(status, 1)
        self.assertEqual(lines, [
            f"{REPO}: reading pull requests not found (HTTP 404): install the App on {REPO}, "
            'or grant it "Pull requests: Read and write"',
            f"{REPO}: reading the repository not found (HTTP 404): install the App on {REPO}, "
            'or grant it "Metadata: Read-only"',
        ])

    def test_refuses_to_run_without_the_worker_gh_directory(self) -> None:
        status, lines, stderr = self.check(env={"GH_CONFIG_DIR": ""})

        self.assertEqual((status, lines), (2, []))
        self.assertIn("GH_CONFIG_DIR is not set", stderr)

    def test_refuses_when_a_token_variable_overrides_the_app_login(self) -> None:
        status, lines, stderr = self.check(env={"GH_TOKEN": "x"})

        self.assertEqual((status, lines), (2, []))
        self.assertIn("GH_TOKEN is set", stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.broker.requests, [])


if __name__ == "__main__":
    unittest.main()
