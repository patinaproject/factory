from __future__ import annotations

import re
from typing import Any, Callable, Optional

# Same set as Hermes' _RESPAWN_GUARD_PR_URL_RE (hermes_cli/kanban_db_dispatch.py), which holds a `ready` card for
# 24 hours after a comment that matches it.
PULL_URL = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.IGNORECASE)

WEBHOOK_PLATFORM = "webhook"


def refusal(tool_name: str, args: Any, platform: Callable[[], str]) -> Optional[str]:
    """The reason to refuse this tool call, or None to allow it."""
    if tool_name == "kanban_comment":
        body = args.get("body") if isinstance(args, dict) else None
        if not isinstance(body, str):
            return None
        urls = [(m.group(0), f"{m.group(1)}/{m.group(2)}#{m.group(3)}") for m in PULL_URL.finditer(body)]
        if not urls:
            return None
        listed = "\n".join(f"- {url} -> {short}" for url, short in urls)
        return (
            "Hermes holds a `ready` card for 24 hours after a comment that names a pull request URL, so the card "
            "would not start. Write each pull request as <owner>/<name>#<number> and call kanban_comment again:\n"
            f"{listed}"
        )
    if tool_name == "kanban_create" and platform() == WEBHOOK_PLATFORM:
        return (
            "A triage turn creates a card only with the shell command `hermes kanban create` (or "
            "`hermes kanban --board triage create` for a triage card), because the kanban_create tool cannot set "
            "the card's branch. Run the command that the prompt's card-creation step shows."
        )
    return None


def make_hook(platform: Callable[[], str]):
    def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
        message = refusal(tool_name, args, platform)
        return None if message is None else {"action": "block", "message": message}

    return on_pre_tool_call
