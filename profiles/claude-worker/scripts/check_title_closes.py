"""Fail a pull request whose title or commit messages would close an issue that its body does not close.

GitHub closes an issue when a squash commit message names it after a closing keyword, and the squash commit
takes the pull request title and the commit messages below it. The `fix` type is itself a closing keyword, so `fix: #177 ...` closes #177 even
when the body says `Part of #177`.

Reads `PR_TITLE`, `PR_BODY`, `PR_HEAD_REF`, `PR_AUTHOR`, `GITHUB_REPOSITORY`, and `PR_COMMITS`, one JSON object
`{"sha", "message"}` per line. Prints one line per issue the body does not close, naming the commit that closes it,
and exits 1 when any is missing. Release, bump, and dependabot pull requests are skipped.
"""
from __future__ import annotations

import json
import os
import re
import sys

import card_branch

FIX_TYPE = re.compile(r"^fix(?:\([^)]*\))?!?:[ \t]*#(?P<number>[1-9][0-9]*)\b", re.IGNORECASE)
EXEMPT_HEAD_PREFIXES = ("release-please--", "bot/bump-")
EXEMPT_AUTHORS = ("dependabot[bot]",)


def exempt(head_ref: str, author: str) -> bool:
    return head_ref.startswith(EXEMPT_HEAD_PREFIXES) or author in EXEMPT_AUTHORS


def title_closes(title: str, repo: str) -> set[str]:
    found = {ref for ref in card_branch.closes(title, repo) if ref.startswith("#")}
    typed = FIX_TYPE.match(title)
    if typed:
        found.add("#" + typed["number"])
    return found


def missing(title: str, body: str, repo: str) -> list[str]:
    in_body = card_branch.closes(body, repo)
    return [
        f"The title closes {ref} when it merges, but the body has no `Closes {ref}` line. "
        f"Retitle to the issue this pull request completes (a dedicated sub-issue), or use another type "
        f"such as `feat` or `refactor` and keep `Part of {ref}` in the body."
        for ref in sorted(title_closes(title, repo) - in_body, key=lambda ref: int(ref[1:]))
    ]


def missing_from_commits(commits: list[dict[str, str]], body: str, repo: str) -> list[str]:
    in_body = card_branch.closes(body, repo)
    return [
        f"Commit {commit['sha']} closes {ref} when it merges, but the body has no `Closes {ref}` line. "
        f"Reword the commit so it does not close {ref} (for example `feat` or `refactor` instead of `fix`), "
        f"or close {ref} in the body."
        for commit in commits
        for ref in sorted(title_closes(commit["message"], repo) - in_body, key=lambda ref: int(ref[1:]))
    ]


def main() -> int:
    if exempt(os.environ.get("PR_HEAD_REF", ""), os.environ.get("PR_AUTHOR", "")):
        print("Release, bump, and dependabot pull requests skip the title closing check.")
        return 0
    title, body, repo = os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""), os.environ["GITHUB_REPOSITORY"]
    commits = [json.loads(line) for line in os.environ.get("PR_COMMITS", "").splitlines() if line.strip()]
    problems = missing(title, body, repo) + missing_from_commits(commits, body, repo)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print("Every issue the title and commits close is closed by the body.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
