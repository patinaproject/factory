"""Compare an installed worker home with a commit of the factory checkout.

check-install reports the comparison as drift. claude-session asks `skew` before it starts Claude, because
a deploy that moved the scripts before the plugins they import leaves a home whose parts come from two
commits, and a run on such a home can crash after Claude has worked for minutes. It compares with the commit
auto-deploy recorded as deployed, not the checkout's HEAD, because the checkout moves ahead of the install
whenever a merge is fetched before the next deploy.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import NamedTuple

PROFILE = "profiles/claude-worker"
SCRIPTS = f"{PROFILE}/scripts"
IMPORTED_PLUGINS = ("blocker-order", "linear-agent-session")
DEPLOY_STATE = "auto-deploy.json"


class GitError(Exception):
    pass


class MissingCommit(Exception):
    pass


def deploy_state(home: Path) -> dict:
    """auto-deploy's state file in the root home above the worker home, or {} when it is missing or unreadable."""
    try:
        state = json.loads((home.parent.parent / DEPLOY_STATE).read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def deployed(state: dict) -> str | None:
    commit = state.get("deployed")
    return commit if isinstance(commit, str) and commit else None


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


class Commit(NamedTuple):
    repo: Path
    sha: str
    label: str

    @property
    def short(self) -> str:
        return self.sha[:7]

    def files(self, path: str) -> dict:
        """Map each file path under `path` to its blob id."""
        out = git(self.repo, "ls-tree", "-r", "-z", self.sha, "--", path)
        files = {}
        for entry in filter(None, out.split("\0")):
            meta, name = entry.split("\t", 1)
            files[name] = meta.split()[2]
        return files

    def blob(self, blob_id: str) -> bytes:
        return subprocess.run(
            ["git", "cat-file", "blob", blob_id], cwd=self.repo, capture_output=True, check=True
        ).stdout


def blob_id(data: bytes, like: str) -> str:
    algorithm = hashlib.sha256 if len(like) == 64 else hashlib.sha1
    return algorithm(b"blob %d\0" % len(data) + data).hexdigest()


def compare_files(commit: Commit, files: dict, strip: str, home: Path) -> list:
    details = []
    for path, wanted in sorted(files.items()):
        relative = path[len(strip):]
        installed = home / relative
        if not installed.is_file():
            details.append(f"{relative} is missing")
        elif blob_id(installed.read_bytes(), wanted) != wanted:
            details.append(f"{relative} differs from {commit.label} at {commit.short}")
    return details


def skew(home: Path, repo: Path, sha: str | None) -> list:
    """The parts of `home` that differ from commit `sha` of `repo`, or from its HEAD when `sha` is None."""
    if sha is None:
        commit = Commit(repo, git(repo, "rev-parse", "--verify", "HEAD^{commit}").strip(), "HEAD")
    else:
        git(repo, "rev-parse", "--git-dir")
        try:
            commit = Commit(repo, git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}").strip(), "the deployed commit")
        except GitError:
            raise MissingCommit(f"the deployed commit {sha[:12]} is not in the factory checkout at {repo}") from None
    differing = []
    if compare_files(commit, commit.files(SCRIPTS), f"{PROFILE}/", home):
        differing.append("profile")
    for name in IMPORTED_PLUGINS:
        prefix = f"plugins/{name}/"
        files = {path: blob for path, blob in commit.files(f"plugins/{name}").items() if not path.startswith(f"{prefix}tests/")}
        if compare_files(commit, files, prefix, home / "plugins" / name):
            differing.append(name)
    return differing
