"""Fakes at dependabot-sweep's boundaries: GitHub's REST API behind a `gh` stand-in, and the CISA KEV feed.

Linear's fake is `FakeLinear` in sentry_fakes.py. Nothing here came from a real repository or advisory.
"""
from __future__ import annotations

import json
import re
import threading
from urllib.parse import parse_qs, urlsplit

from tests.sentry_fakes import _Server

APP_LOGIN = "example-app[bot]"
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys, urllib.request

payload = {"argv": sys.argv[1:], "stdin": "" if sys.stdin.isatty() else sys.stdin.read(), "config_dir": os.environ.get("GH_CONFIG_DIR")}
request = urllib.request.Request(os.environ["FAKE_GH_URL"], json.dumps(payload).encode(), {"Content-Type": "application/json"})
reply = json.load(urllib.request.urlopen(request))
sys.stdout.write(reply["stdout"])
sys.stderr.write(reply["stderr"])
sys.exit(reply["status"])
"""


def raw_alert(number: int, *, package: str = "lodash", ecosystem: str = "npm", state: str = "open", severity: str = "high",
              ghsa: str | None = None, cves=("CVE-2026-0001",), scope: str | None = "runtime", relationship: str = "direct",
              epss: str | None = None, patched: str | None = "1.2.3", summary: str = "A flaw", references=("https://example.invalid/advisory",)) -> dict:
    return {
        "number": number, "state": state, "html_url": f"https://github.com/example-org/example-repo/security/dependabot/{number}",
        "dependency": {
            "package": {"ecosystem": ecosystem, "name": package}, "manifest_path": "package.json", "scope": scope,
            "relationship": relationship,
        },
        "security_advisory": {
            "ghsa_id": ghsa or f"GHSA-aaaa-bbbb-{number:04d}", "cve_id": cves[0] if cves else None,
            "identifiers": [{"type": "GHSA", "value": "x"}, *({"type": "CVE", "value": c} for c in cves)],
            "summary": summary, "severity": severity, "references": [{"url": u} for u in references],
            "epss": [{"percentage": 0.001, "percentile": epss}] if epss is not None else [],
        },
        "security_vulnerability": {
            "vulnerable_version_range": "< 1.2.3", "first_patched_version": {"identifier": patched} if patched else None,
        },
    }


def raw_repo(full_name: str, visibility: str = "private", archived: bool = False) -> dict:
    return {"full_name": full_name, "visibility": visibility, "archived": archived}


def github_issue(number: int, repo: str, body: str, *, state: str = "open", created_at: str = "2026-10-01T00:00:00Z",
                 user: str = APP_LOGIN, assignees=(), pull_request: bool = False, title: str = "t") -> dict:
    issue = {
        "number": number, "title": title, "body": body, "state": state, "created_at": created_at, "user": {"login": user},
        "assignees": [{"login": a} for a in assignees], "html_url": f"https://github.com/{repo}/issues/{number}",
    }
    if pull_request:
        issue["pull_request"] = {"url": "x"}
    return issue


class FakeGitHub(_Server):
    """A GitHub organization behind `gh`. `writes` records each issue write as (operation, repository, details).

    `errors` maps a repository's alerts, or "repositories", "labels:<repo>", or "issues:<repo>", to (status, message).
    `fail_writes` holds (operation, repository) pairs that fail.
    """

    def __init__(self, repos=(), *, alerts=None, labels=None, issues=None, errors=None, fail_writes=(), page_size: int = 100) -> None:
        self.page_size = page_size
        self.pages_served = 0
        self.repos = list(repos)
        self.alerts = alerts or {}
        self.labels = labels or {}
        self.issues = {repo: list(rows) for repo, rows in (issues or {}).items()}
        self.errors = errors or {}
        self.fail_writes = set(fail_writes)
        self.writes: list = []
        self.requests: list = []
        self.config_dirs: set = set()
        self.next_number = 700
        self.lock = threading.Lock()
        super().__init__(self.handle)

    def handle(self, method: str, path: str, body):
        with self.lock:
            self.requests.append(body["argv"])
            self.config_dirs.add(body["config_dir"])
            status, stdout, stderr = self.run(body["argv"], body["stdin"])
        return 200, {"status": 0 if status == 200 else 1, "stdout": stdout, "stderr": stderr}, {}

    def run(self, argv: list, stdin: str):
        if argv[0] == "api":
            assert argv[1:3] == ["--paginate", "--slurp"], argv
            return self.api(argv[3])
        return self.issue_command(argv, stdin)

    def failure(self, status: int, message: str):
        return status, json.dumps({"message": message}), f"gh: {message} (HTTP {status})\n"

    def page(self, rows: list, query: dict, wrap=None):
        """What `gh api --paginate --slurp` prints: a JSON array of every page."""
        chunks = [rows[start:start + self.page_size] for start in range(0, len(rows), self.page_size)] or [[]]
        self.pages_served += len(chunks)
        return 200, json.dumps([wrap(chunk, len(rows)) if wrap else chunk for chunk in chunks]), ""

    def api(self, endpoint: str):
        parts = urlsplit(endpoint)
        query = parse_qs(parts.query)
        route = parts.path
        if route == "installation/repositories":
            if "repositories" in self.errors:
                return self.failure(*self.errors["repositories"])
            return self.page(self.repos, query, lambda chunk, total: {"total_count": total, "repositories": chunk})
        found = re.fullmatch(r"repos/([^/]+/[^/]+)/(dependabot/alerts|labels|issues)", route)
        if not found:
            return self.failure(404, "Not Found")
        repo, kind = found.groups()
        if kind == "dependabot/alerts":
            if repo in self.errors:
                return self.failure(*self.errors[repo])
            return self.page(self.alerts.get(repo, []), query)
        if f"{kind}:{repo}" in self.errors:
            return self.failure(*self.errors[f"{kind}:{repo}"])
        if kind == "labels":
            return self.page([{"name": n} for n in self.labels.get(repo, [])], query)
        assert query["creator"] == [APP_LOGIN] and query["state"] == ["all"], query
        return self.page(self.issues.get(repo, []), query)

    def issue_command(self, argv: list, stdin: str):
        operation = argv[1]
        repo = argv[argv.index("-R") + 1]
        if (operation, repo) in self.fail_writes:
            return 1, "", f"gh: {operation} failed\n"
        if operation == "create":
            labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
            number = self.next_number
            self.next_number += 1
            title = argv[argv.index("--title") + 1]
            self.writes.append(("create", repo, {"title": title, "body": stdin, "labels": labels, "args": argv}))
            self.issues.setdefault(repo, []).append(github_issue(number, repo, stdin, created_at="2026-11-03T12:00:00Z", title=title))
            return 200, f"https://github.com/{repo}/issues/{number}\n", ""
        number = int(argv[2])
        issue = next(i for i in self.issues[repo] if i["number"] == number)
        if operation == "edit":
            issue["body"] = stdin
            self.writes.append(("edit", repo, {"number": number, "body": stdin}))
        elif operation == "comment":
            self.writes.append(("comment", repo, {"number": number, "body": stdin}))
        elif operation == "close":
            issue["state"] = "closed"
            self.writes.append(("close", repo, {"number": number, "reason": argv[argv.index("--reason") + 1]}))
        return 200, "", ""

    def written(self, operation: str | None = None) -> list:
        return [w for w in self.writes if operation in (None, w[0])]


class FakeKev(_Server):
    def __init__(self, cves=(), *, status: int = 200) -> None:
        self.cves = list(cves)
        self.status = status
        self.requests = 0
        super().__init__(self.handle)

    def handle(self, method: str, path: str, body):
        self.requests += 1
        return self.status, {"vulnerabilities": [{"cveID": c} for c in self.cves]}, {}
