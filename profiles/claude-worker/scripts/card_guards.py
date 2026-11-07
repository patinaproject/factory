"""The watcher runs under the macOS system Python 3.9 and loads this file by path, so it uses only the standard library.
"""
from __future__ import annotations

import json
import re

RUN_ERROR_LIMIT = 200
TOKEN_SHAPED = re.compile(r"[A-Za-z0-9_\-+=.]{24,}")


def mask_token_shapes(text: str) -> str:
    return TOKEN_SHAPED.sub(
        lambda m: "[redacted]" if re.search(r"\d", m[0]) and re.search(r"[A-Za-z]", m[0]) else m[0], text
    )


def guard_run_starts(events: list, reason: str) -> dict:
    runs = {}
    for event in events:
        if event["kind"] == "respawn_guarded" and guard_reason(event["payload"]) == reason:
            runs.setdefault(event["id"], event["created_at"])
        else:
            runs.pop(event["id"], None)
    return runs


def guard_reason(payload) -> str | None:
    try:
        data = json.loads(payload or "{}")
    except ValueError:
        return None
    return data.get("reason") if isinstance(data, dict) else None
