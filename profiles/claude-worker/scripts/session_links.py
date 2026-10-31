from __future__ import annotations

import re

PULL_REQUEST_URL = re.compile(r"(https://github\.com/[^/\s]+/[^/\s]+/pull/([0-9]+))\b")
CREATE_COMMAND = "gh pr create"


def pull_link(url: str, prefix: str = "Pull request") -> dict:
    match = PULL_REQUEST_URL.fullmatch(url)
    if match is None:
        raise ValueError(f"{url!r} is not a pull request URL")
    return {"label": f"{prefix} #{match.group(2)}", "url": url}


def created_pull_url(event: dict) -> str | None:
    """The URL `gh pr create` printed, from the PostToolUse event of a Bash call that ran it."""
    if event.get("tool_name") != "Bash" or CREATE_COMMAND not in (event.get("tool_input") or {}).get("command", ""):
        return None
    response = event.get("tool_response")
    if not isinstance(response, dict) or response.get("interrupted") or not isinstance(response.get("stdout"), str):
        return None
    urls = [match.group(1) for match in PULL_REQUEST_URL.finditer(response["stdout"])]
    return urls[-1] if urls else None


def add(linear, client, session_id: str, links: list[dict]) -> dict:
    """The `session_links` of one add. A failure is reported, never raised."""
    try:
        added = client.add_external_urls(session_id, links)
    except (*linear.CLIENT_ERRORS, OSError) as error:
        return {"added": [], "action": "failed", "error": f"linking the agent session failed: {error}"}
    return {"added": added, "action": "linked" if added else "unchanged"}


def combine(total: dict, step: dict) -> dict:
    """Two adds as one report: the URLs of both, failed when either failed."""
    added = total["added"] + step["added"]
    failed = total if total["action"] == "failed" else step
    if failed["action"] == "failed":
        return {"added": added, "action": "failed", "error": failed["error"]}
    return {"added": added, "action": "linked" if added else "unchanged"}
