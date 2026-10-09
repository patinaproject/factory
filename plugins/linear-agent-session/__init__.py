"""Hermes plugin: Linear agent session acknowledgement hook and Linear agent tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from . import linear, tools

logger = logging.getLogger(__name__)

TOOLSET = "linear_agent_session"
REQUIRED_ENV = ["LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET"]
ACK_BODY = "Received. Triaging now."
# Linear marks a session unresponsive after 10 s without an activity; leave room for token minting.
ACK_TIMEOUT_SECONDS = 5.0


def register(ctx: Any) -> None:
    client: Optional[linear.LinearClient] = None

    def get_client() -> linear.LinearClient:
        nonlocal client
        if client is None:
            client = linear.client_from_env()
        return client

    ctx.register_tool(
        name="linear_agent_activity",
        toolset=TOOLSET,
        schema=tools.ACTIVITY_SCHEMA,
        handler=lambda args, **_: tools.run_activity(get_client, args),
        requires_env=REQUIRED_ENV,
        description=tools.ACTIVITY_SCHEMA["description"],
    )
    ctx.register_tool(
        name="linear_issue",
        toolset=TOOLSET,
        schema=tools.ISSUE_SCHEMA,
        handler=lambda args, **_: tools.run_issue(get_client, args),
        requires_env=REQUIRED_ENV,
        description=tools.ISSUE_SCHEMA["description"],
    )
    ctx.register_hook(
        "pre_gateway_dispatch",
        make_dispatch_hook(get_client, lambda: ctx.get_config("webhook_route", default="linear")),
    )


def make_dispatch_hook(
    get_client: Callable[[], linear.LinearClient], webhook_route: Callable[[], str]
) -> Callable[..., Any]:
    async def on_pre_gateway_dispatch(event: Any, **_: Any) -> None:
        try:
            session_id = _ack_session_id(event, webhook_route())
            if session_id is None:
                return None
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: get_client().create_activity(
                        session_id, {"type": "thought", "body": ACK_BODY}, ephemeral=True
                    ),
                ),
                timeout=ACK_TIMEOUT_SECONDS,
            )
        except Exception:
            # The acknowledgement is best effort and must never hold up or drop the triage run.
            logger.exception("linear-agent-session: agent session acknowledgement failed")
        return None

    return on_pre_gateway_dispatch


def _ack_session_id(event: Any, webhook_route: str) -> Optional[str]:
    if event.source.platform.value != "webhook" or event.source.user_id != f"webhook:{webhook_route}":
        return None
    payload = event.raw_message
    if not isinstance(payload, dict) or payload.get("type") != "AgentSessionEvent":
        return None
    action = payload.get("action")
    if action == "prompted" and (payload.get("agentActivity") or {}).get("signal") == "stop":
        return None
    if action not in ("created", "prompted"):
        return None
    return payload["agentSession"]["id"]
