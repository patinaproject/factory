from __future__ import annotations

import json
import subprocess
from typing import Callable, Optional, Sequence

from .linear import GITHUB_ISSUE_URL, LinearError, role_key

GH_TIMEOUT_SECONDS = 30

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
    try:
        proc = subprocess.run(
            ["gh", *args], input=stdin, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS
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
    """Change one GitHub issue with one PATCH. GitHub creates a label it lacks, so only existing labels are added."""
    if not GITHUB_ISSUE_URL.fullmatch(url):
        raise LinearError(f"{url} is not a GitHub issue URL")
    owner, repo, _, number = url.removeprefix("https://github.com/").split("/")
    path = f"repos/{owner}/{repo}/issues/{number}"
    issue = json.loads(gh(["api", path], None))
    current = [label["name"] for label in issue["labels"]]
    labels = [n for n in current if role_key(n) not in {role_key(r) for r in remove_labels}]
    if add_labels:
        known = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{owner}/{repo}/labels?per_page=100"], None))
        names = sorted(label["name"] for page in known for label in page)
        for name in add_labels:
            match = [n for n in names if role_key(n) == role_key(name)]
            if len(match) != 1:
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
