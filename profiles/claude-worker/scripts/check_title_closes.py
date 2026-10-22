"""Fail a pull request whose title would close an issue that its body does not close.

GitHub closes an issue when a squash commit message names it after a closing keyword, and the squash commit
takes the pull request title. The `fix` type is itself a closing keyword, so `fix: #177 ...` closes #177 even
when the body says `Part of #177`. The body's closing lines are the only record of what a pull request
completes, so every issue the title closes must also appear there.

Reads `PR_TITLE`, `PR_BODY`, `PR_HEAD_REF`, `PR_AUTHOR`, and `GITHUB_REPOSITORY`. Prints one line per issue the
body does not close and exits 1 when any is missing. Release, bump, and dependabot pull requests are skipped.
"""
from __future__ import annotations

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


def main() -> int:
    if exempt(os.environ.get("PR_HEAD_REF", ""), os.environ.get("PR_AUTHOR", "")):
        print("Release, bump, and dependabot pull requests skip the title closing check.")
        return 0
    problems = missing(os.environ.get("PR_TITLE", ""), os.environ.get("PR_BODY", ""), os.environ["GITHUB_REPOSITORY"])
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print("Every issue the title closes is closed by the body.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
