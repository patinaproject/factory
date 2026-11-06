"""email-outcome: the mail-worker profile's tool allowlist hook and its `email` toolset.

Enable it only in the mail-worker profile. Its hook refuses every tool outside the mail run's allowlist, so in any other
home it would stop that home's own tools.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any

TOOLSET = "email"
INTAKE_LEDGER = Path("plugins") / "email-intake" / "ledger.py"
LINEAR_PLUGIN = Path("plugins") / "linear-agent-session"


def load_ledger(root: Path):
    spec = importlib.util.spec_from_file_location("email_outcome_ledger", root / INTAKE_LEDGER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_linear(root: Path):
    # A bare package skips linear-agent-session's __init__, which registers that plugin's own tools and hooks.
    name = "email_outcome_linear"
    package = types.ModuleType(name)
    package.__path__ = [str(root / LINEAR_PLUGIN)]
    sys.modules[name] = package
    return importlib.import_module(f"{name}.linear")


def settings() -> dict:
    from hermes_cli.config import load_config_readonly

    entries = (load_config_readonly().get("plugins") or {}).get("entries") or {}
    return (entries.get("email-outcome") or {}).get("settings") or {}


def register(ctx: Any) -> None:
    from agent.redact import redact_sensitive_text
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    from . import allowlist, outcome, secrets_in_mail

    root = kb.kanban_home()
    ledger = load_ledger(root)
    linear = load_linear(root)
    client = None

    def get_client():
        nonlocal client
        if client is None:
            client = linear.client_from_env()
        return client

    def card_id():
        return os.environ.get("HERMES_KANBAN_TASK") or None

    def complete(task_id: str, final: str) -> None:
        run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
        conn = kbc.connect()
        try:
            kb.complete_task(conn, task_id, result=final, summary=final, expected_run_id=int(run_id) if run_id else None)
        finally:
            conn.close()

    context = outcome.Context(
        ledger=ledger.Ledger(ledger.ledger_path(root)),
        ledger_module=ledger,
        client=get_client,
        card_id=card_id,
        complete=complete,
        secret_refusal=lambda text, source: secrets_in_mail.refusal(
            text, source, lambda value: redact_sensitive_text(value, force=True)
        ),
        team_key=str(settings().get("team_key") or ""),
    )
    ctx.register_hook("pre_tool_call", allowlist.make_hook(card_id, kb.task_attachments_dir))
    ctx.register_tool(
        name=outcome.SEARCH_SCHEMA["name"], toolset=TOOLSET, schema=outcome.SEARCH_SCHEMA,
        handler=outcome.search_handler(get_client), description=outcome.SEARCH_SCHEMA["description"],
    )
    ctx.register_tool(
        name=outcome.SCHEMA["name"], toolset=TOOLSET, schema=outcome.SCHEMA,
        handler=outcome.handler(context), description=outcome.SCHEMA["description"],
    )
