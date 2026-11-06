from __future__ import annotations

import re
from typing import Mapping, NamedTuple, Optional, Sequence

# One pattern for every URL that names a repository: the repo page, a file, an alert, a pull request.
# Linear stores a link as the person typed it, so scheme, `www.`, case, `.git`, a query, and a fragment all vary.
GITHUB_REPOSITORY_URL = re.compile(
    r"https?://(?:www\.)?github\.com/(?P<owner>[^/\s?#]+)/(?P<name>[^/\s?#]+?)(?:\.git)?(?:[/?#].*)?",
    re.IGNORECASE,
)
SYNCED_ISSUE_URL = re.compile(r"https://github\.com/(?P<owner>[^/\s]+)/(?P<name>[^/\s]+)/issues/[0-9]+")


class Resolution(NamedTuple):
    """The repository an issue's cards run in, or the two configured repositories its links name."""

    repository: Optional[str]
    conflict: Optional[tuple[str, str]]


def resolve_repository(repositories: Sequence[Mapping], attachment_urls: Sequence[str]) -> Resolution:
    """A synced GitHub issue link wins. Otherwise one `default` or `linked` repository link picks that repository,
    two conflict, and none means the `default` repository."""
    by_name = {repo["full_name"].lower(): repo for repo in repositories}
    for url in attachment_urls:
        match = SYNCED_ISSUE_URL.fullmatch(url)
        repo = match and by_name.get(f"{match['owner']}/{match['name']}".lower())
        if repo and repo["routing"] == "synced_github":
            return Resolution(repo["full_name"], None)
    named: dict[str, None] = {}
    for url in attachment_urls:
        match = GITHUB_REPOSITORY_URL.fullmatch(url)
        repo = match and by_name.get(f"{match['owner']}/{match['name']}".lower())
        if repo and repo["routing"] in ("default", "linked"):
            named[repo["full_name"]] = None
    if len(named) > 1:
        first, second, *_ = named
        return Resolution(None, (first, second))
    if named:
        return Resolution(next(iter(named)), None)
    default = next(repo for repo in repositories if repo["routing"] == "default")
    return Resolution(default["full_name"], None)
