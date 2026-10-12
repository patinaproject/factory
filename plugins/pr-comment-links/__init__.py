"""Keep GitHub pull request URLs out of Kanban card comments.

Hermes' dispatcher holds a ``ready`` card for 24 hours after any comment that
contains ``https://github.com/<owner>/<name>/pull/<number>`` (``respawn_guarded``
with reason ``active_pr``), unless a later event hands the card to another
profile. The factory's triage agents and workers comment on cards through the
``kanban_comment`` tool, so this hook rewrites each such URL to
``<owner>/<name>#<number>`` before the comment is stored.
"""

from __future__ import annotations

import re
from typing import Any, Optional

COMMENT_TOOL = "kanban_comment"

# The dispatcher matches ``https?://github.com/<owner>/<name>/pull/<number>`` without regard to
# case. The tail keeps a comment or review anchor, such as ``#issuecomment-12``, as plain text.
PULL_REQUEST_URL = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<name>[^/\s]+)/pull/(?P<number>\d+)"
    r"(?P<tail>(?:[/#](?:[\w.\-/#]*[\w\-/#])?)?)",
    re.IGNORECASE,
)


def _reference(match: re.Match) -> str:
    reference = f"{match['owner']}/{match['name']}#{match['number']}"
    tail = match["tail"].strip("/#")
    return f"{reference} ({tail})" if tail else reference


def name_pull_requests(body: str) -> str:
    """Return ``body`` with every GitHub pull request URL written as ``owner/name#number``."""
    return PULL_REQUEST_URL.sub(_reference, body)


def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
    if tool_name != COMMENT_TOOL or not isinstance(args, dict):
        return None
    body = args.get("body")
    if not isinstance(body, str):
        return None
    named = name_pull_requests(body)
    if named == body:
        return None
    return {"action": "modify", "args": {"body": named}}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
