from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional, Sequence

from .linear import GITHUB_ISSUE_URL, LinearError, role_key

GH_TIMEOUT_SECONDS = 30
# The gateway runs this with every factory secret in its environment, so gh sees only these.
GH_ENV_NAMES = ("PATH", "HOME", "GH_CONFIG_DIR")
WORKER_GH_DIR = Path.home() / ".hermes/profiles/claude-worker/gh"

# Each status name the Linear route may ask for, keyed by role_key, and the GitHub state it means.
CLOSED_COMPLETED = {"state": "closed", "state_reason": "completed"}
CLOSED_NOT_PLANNED = {"state": "closed", "state_reason": "not_planned"}
OPEN = {"state": "open"}
STATES = {
    "done": CLOSED_COMPLETED,
    "completed": CLOSED_COMPLETED,
    "closed": CLOSED_COMPLETED,
    "canceled": CLOSED_NOT_PLANNED,
    "cancelled": CLOSED_NOT_PLANNED,
    "wontfix": CLOSED_NOT_PLANNED,
    "notplanned": CLOSED_NOT_PLANNED,
    "todo": OPEN,
    "backlog": OPEN,
    "triage": OPEN,
    "open": OPEN,
    "reopen": OPEN,
}

Gh = Callable[[Sequence[str], Optional[str]], str]


def run_gh(args: Sequence[str], stdin: Optional[str] = None) -> str:
    """Run gh as the GitHub App. gh gets only GH_ENV_NAMES, never the gateway's secrets."""
    env = {k: os.environ[k] for k in GH_ENV_NAMES if k in os.environ}
    env.setdefault("GH_CONFIG_DIR", str(WORKER_GH_DIR))
    gh = Path.home() / ".local/bin/gh"
    try:
        proc = subprocess.run(
            [str(gh), *args], input=stdin, env=env, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise LinearError(f"gh {args[0]} failed: {e}") from None
    if proc.returncode != 0:
        raise LinearError(f"gh {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


def edit_issue(
    url: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    add_labels: Sequence[str] = (),
    remove_labels: Sequence[str] = (),
    state: Optional[str] = None,
    gh: Gh = run_gh,
) -> dict:
    """Change one issue of a public repository with one PATCH. GitHub creates a label it lacks, so only existing labels are added.

    A private repository's issues are canonical in Linear, so the tool refuses them."""
    if not GITHUB_ISSUE_URL.fullmatch(url):
        raise LinearError(f"{url} is not a GitHub issue URL")
    owner, repo, _, number = url.removeprefix("https://github.com/").split("/")
    visibility = json.loads(gh(["api", f"repos/{owner}/{repo}"], None))["visibility"]
    if visibility != "public":
        raise LinearError(f"{owner}/{repo} is {visibility}; edit its issues in Linear")
    path = f"repos/{owner}/{repo}/issues/{number}"
    issue = json.loads(gh(["api", path], None))
    current = [label["name"] for label in issue["labels"]]
    labels = [n for n in current if role_key(n) not in {role_key(r) for r in remove_labels}]
    if add_labels:
        known = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{owner}/{repo}/labels?per_page=100"], None))
        names = sorted(label["name"] for page in known for label in page)
        for name in add_labels:
            match = [n for n in names if role_key(n) == role_key(name)]
            if len(match) > 1:
                raise LinearError(f"more than one label of {owner}/{repo} matches {name!r}: {', '.join(match)}")
            if not match:
                raise LinearError(f"{owner}/{repo} has no label named {name!r}; it has: {', '.join(names)}")
            if match[0] not in labels:
                labels.append(match[0])
    update: dict = {}
    if title is not None and title != issue["title"]:
        update["title"] = title
    if body is not None and body != (issue["body"] or ""):
        update["body"] = body
    if labels != current:
        update["labels"] = labels
    if state is not None:
        target = STATES.get(role_key(state))
        if target is None:
            raise LinearError(f"a GitHub issue has no status {state!r}; name Done, Canceled, or Todo")
        if target["state"] != issue["state"] or target.get("state_reason", issue["state_reason"]) != issue["state_reason"]:
            update.update(target)
    if update:
        issue = json.loads(gh(["api", "-X", "PATCH", path, "--input", "-"], json.dumps(update)))
    return {
        "url": url,
        "changed": bool(update),
        "title": issue["title"],
        "labels": sorted(label["name"] for label in issue["labels"]),
        "state": issue["state"],
        "state_reason": issue["state_reason"],
    }
