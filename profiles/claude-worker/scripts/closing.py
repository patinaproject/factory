"""Decide which issues a pull request body closes. Every closing-reference check asks this module.

Code, HTML comments, struck-out text, and blockquotes do not render, so a reference in them closes nothing.
References are written `#n` for an issue of the repository, `owner/name#n` in lower case for another
repository's issue, and a Linear identifier in upper case.

As a command, it is the pull request workflow's body check: it reads `PR_BODY` and `GITHUB_REPOSITORY` and
exits 1 unless the body closes an issue of the repository.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Iterable

ISSUE_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)$")
IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9]*-[1-9][0-9]*$")
NUMBER = re.compile(r"^#([1-9][0-9]*)$")
# A reference may be the text of a Markdown link to a GitHub or Linear issue, as in `Closes [PAT-9](<its URL>)`,
# which GitHub and Linear both close on.
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ \t]*:?[ \t]+(?P<link>\[)?"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>[1-9][0-9]*)\b"
    r"|https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_number>[1-9][0-9]*)\b"
    r"|(?P<linear>https://linear\.app/[\w-]+/issue/)?(?P<identifier>[A-Z][A-Z0-9]*-[1-9][0-9]*)\b)"
    r"(?(link)\]\((?:https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9][0-9]*"
    r"|https://linear\.app/[\w-]+/issue/[A-Z][A-Z0-9]*-[1-9][0-9]*(?:/[^\s)]*)?)\))",
    re.IGNORECASE,
)
# The `fix` commit type is itself a closing keyword, so a squash commit titled `fix: #5 ...` closes #5.
FIX_TYPE = re.compile(r"^fix(?:\([^)]*\))?!?:[ \t]*#(?P<number>[1-9][0-9]*)\b", re.IGNORECASE)
HIDDEN = re.compile(r"<!--.*?-->|^(```|~~~).*?^\1|`[^`\n]*`|~~.*?~~|^[ \t]*>.*?$", re.DOTALL | re.MULTILINE)


def github_reference(issue_repo: str, number: str, repo: str) -> str:
    return f"#{number}" if issue_repo.lower() == repo.lower() else f"{issue_repo.lower()}#{number}"


def reference(text: str, repo: str) -> str:
    issue = ISSUE_URL.match(text)
    if issue and issue.group(1).lower() == repo.lower():
        return "#" + issue.group(2)
    if NUMBER.match(text) or IDENTIFIER.match(text.upper()):
        return text.upper()
    raise ValueError(f"{text} is not a Linear identifier, #n, or an issue URL in {repo}")


def references(body: str | None, repo: str) -> set[str]:
    found = set()
    for match in CLOSING.finditer(HIDDEN.sub("", body or "")):
        if match["identifier"]:
            found.add(match["identifier"].upper())
        else:
            issue_repo = match["repo"] or match["url_repo"] or repo
            found.add(github_reference(issue_repo, match["number"] or match["url_number"], repo))
    return found


def closes(body: str | None, repo: str) -> set[str]:
    """The references that `body` closes, leaving out other repositories' issues."""
    return {ref for ref in references(body, repo) if "/" not in ref}


def message_closes(message: str, repo: str) -> set[str]:
    """The issues of `repo` that a commit message or pull request title closes when it merges."""
    found = {ref for ref in closes(message, repo) if ref.startswith("#")}
    typed = FIX_TYPE.match(message)
    if typed:
        found.add("#" + typed["number"])
    return found


def might_close(message: str) -> bool:
    return bool(FIX_TYPE.match(message) or CLOSING.search(message))


def missing(body: str | None, repo: str, wanted: Iterable[str]) -> list[str]:
    found = references(body, repo)
    return [ref for ref in wanted if ref not in found]


def with_url(body: str, identifier: str, url: str) -> str:
    hidden = [match.span() for match in HIDDEN.finditer(body)]
    parts, last = [], 0
    for match in CLOSING.finditer(body):
        start, end = match.span("identifier")
        if match["linear"] or match["link"] or (match["identifier"] or "").upper() != identifier.upper():
            continue
        if any(left <= start < right for left, right in hidden):
            continue
        parts += [body[last:start], url]
        last = end
    return "".join(parts) + body[last:]


def main() -> int:
    body, repo = os.environ.get("PR_BODY", ""), os.environ["GITHUB_REPOSITORY"]
    if not body.strip():
        print('::error::PR body is empty. Add "Closes #N".')
        return 1
    if any(ref.startswith("#") for ref in closes(body, repo)):
        print("GitHub issue-closing reference found.")
        return 0
    print(
        '::error::PR body must contain "Closes #N", "Fixes #N", or "Resolves #N" outside code blocks, '
        "HTML comments, strikethrough, and blockquotes."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
