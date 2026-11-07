"""The mail worker's tool allowlist. Hermes forces the whole `kanban` toolset onto every card run and `file` can write,
so this hook is what keeps a run that reads untrusted mail to reading its own files and writing its one outcome."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

SEARCH_TOOL = "email_issue_search"
OUTCOME_TOOL = "email_record_outcome"
READ_TOOLS = ("read_file", "search_files")
OWN_CARD_TOOLS = ("kanban_show", "kanban_heartbeat", "kanban_block")
ALLOWED = frozenset({*READ_TOOLS, *OWN_CARD_TOOLS, SEARCH_TOOL, OUTCOME_TOOL})


def inside(path: Any, directory: Path) -> bool:
    if not isinstance(path, str) or not path:
        return False
    target = os.path.realpath(os.path.expanduser(path))
    if not os.path.isabs(os.path.expanduser(path)):
        return False
    root = os.path.realpath(directory)
    return os.path.commonpath([target, root]) == root


def refusal(tool_name: str, args: Any, card_id: Optional[str], attachments_dir: Callable[[str], Path]) -> Optional[str]:
    args = args if isinstance(args, dict) else {}
    if tool_name not in ALLOWED:
        return f"{tool_name} is not available to a mail run. It can read its email's files and record one outcome."
    if not card_id:
        return "a mail run must run as a Kanban card"
    if tool_name in READ_TOOLS:
        directory = attachments_dir(card_id)
        if not inside(args.get("path"), directory):
            return f"{tool_name} reads only absolute paths inside this card's email directory, {directory}"
    if tool_name in OWN_CARD_TOOLS and args.get("task_id") not in (None, "", card_id):
        return f"{tool_name} acts only on this card, {card_id}"
    return None


def make_hook(card_id: Callable[[], Optional[str]], attachments_dir: Callable[[str], Path]):
    def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict]:
        message = refusal(tool_name, args, card_id(), attachments_dir)
        return None if message is None else {"action": "block", "message": message}

    return on_pre_tool_call
