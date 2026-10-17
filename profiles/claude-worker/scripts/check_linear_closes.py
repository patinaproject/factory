"""Fail a pull request whose body closes a synced GitHub issue but not its Linear issue.

Linear links a pull request to an issue only through a closing line that names the Linear issue, and a card
finds its pull request through that link or through the same line. The Linear issue of a GitHub issue is the
one that Linear's linkback comment on the GitHub issue names.

Reads the body from `PR_BODY` and the repository from `GITHUB_REPOSITORY`. Prints one line per missing closing
line and exits 1 when any is missing.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Callable

import card_branch

LINKBACK_AUTHOR = "linear-code[bot]"
LINKBACK_MARKER = "<!-- linear-linkback -->"
LINKBACK_ISSUE = re.compile(r"https://linear\.app/[\w-]+/issue/([A-Z][A-Z0-9]*-[1-9][0-9]*)")


def linear_issue(gh: Callable[..., Any], repo: str, number: str) -> str | None:
    comments = gh(f"repos/{repo}/issues/{number}/comments?per_page=100") or []
    for comment in comments:
        if comment["user"]["login"] == LINKBACK_AUTHOR and LINKBACK_MARKER in comment["body"]:
            match = LINKBACK_ISSUE.search(comment["body"])
            if match:
                return match.group(1)
    return None


def missing(gh: Callable[..., Any], repo: str, body: str) -> list[str]:
    refs = card_branch.closes(body, repo)
    problems = []
    for ref in sorted(ref for ref in refs if ref.startswith("#")):
        identifier = linear_issue(gh, repo, ref[1:])
        if identifier and identifier not in refs:
            problems.append(
                f"The body closes {ref}, which Linear links to {identifier}, but has no `Closes {identifier}` line. "
                f"Add `Closes {identifier}` so Linear links this pull request and a card adopts it."
            )
    return problems


def main() -> int:
    try:
        problems = missing(card_branch.run_gh, os.environ["GITHUB_REPOSITORY"], os.environ.get("PR_BODY", ""))
    except card_branch.GhFailed as failure:
        print(f"::error::{failure}")
        return 1
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print("Every synced GitHub issue this body closes has its Linear closing line.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
