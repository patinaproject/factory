from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Mapping, Optional

from . import linear, tools

logger = logging.getLogger(__name__)

TOOLSET = "linear_agent_session"
ACK_BODY = "Received. Triaging now."
# Linear marks a session unresponsive after 10 s without an activity; leave room for token minting.
ACK_TIMEOUT_SECONDS = 5.0
# Hermes leaves a secret like this verbatim when its variable is unset, and anyone can sign with that text.
UNRESOLVED_REFERENCE = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}$")


def register(ctx: Any) -> None:
    from hermes_cli.config import load_config_readonly

    for route, variable in unresolved_routes(load_config_readonly()).items():
        logger.error(
            "linear-agent-session: route %s secret is the unresolved reference ${%s}; its deliveries will be dropped",
            route,
            variable,
        )

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
        description=tools.ACTIVITY_SCHEMA["description"],
    )
    ctx.register_tool(
        name="linear_issue",
        toolset=TOOLSET,
        schema=tools.ISSUE_SCHEMA,
        handler=lambda args, **_: tools.run_issue(get_client, args),
        description=tools.ISSUE_SCHEMA["description"],
    )
    ctx.register_tool(
        name="linear_agent_session_create",
        toolset=TOOLSET,
        schema=tools.SESSION_CREATE_SCHEMA,
        handler=lambda args, **_: tools.run_session_create(get_client, args),
        description=tools.SESSION_CREATE_SCHEMA["description"],
    )
    ctx.register_tool(
        name="linear_delegated_issues",
        toolset=TOOLSET,
        schema=tools.DELEGATED_ISSUES_SCHEMA,
        handler=lambda args, **_: tools.run_delegated_issues(get_client, args),
        description=tools.DELEGATED_ISSUES_SCHEMA["description"],
    )
    ctx.register_tool(
        name="linear_issue_undelegate",
        toolset=TOOLSET,
        schema=tools.UNDELEGATE_SCHEMA,
        handler=lambda args, **_: tools.run_undelegate(get_client, args),
        description=tools.UNDELEGATE_SCHEMA["description"],
    )
    ctx.register_hook(
        "pre_gateway_dispatch",
        make_dispatch_hook(
            get_client, lambda: ctx.get_config("webhook_route", default="linear"), load_config_readonly
        ),
    )


def unresolved_routes(config: Mapping[str, Any]) -> dict[str, str]:
    extra = ((config.get("platforms") or {}).get("webhook") or {}).get("extra") or {}
    global_secret = extra.get("secret", "")
    unresolved = {}
    for name, route in (extra.get("routes") or {}).items():
        secret = route.get("secret", global_secret)
        match = UNRESOLVED_REFERENCE.match(secret) if isinstance(secret, str) else None
        if match:
            unresolved[name] = match.group(1)
    return unresolved


def make_dispatch_hook(
    get_client: Callable[[], linear.LinearClient],
    webhook_route: Callable[[], str],
    load_config: Callable[[], Mapping[str, Any]],
) -> Callable[..., Any]:
    async def guard_then_acknowledge(event: Any, **_: Any) -> Optional[dict[str, str]]:
        route = _delivery_route(event)
        if route is None:
            return None
        variable = unresolved_routes(load_config()).get(route)
        if variable is not None:
            logger.error(
                "linear-agent-session: dropped a delivery on route %s: secret is the unresolved reference ${%s}",
                route,
                variable,
            )
            return {"action": "skip", "reason": f"route {route} secret is the unresolved reference ${{{variable}}}"}
        await acknowledge_best_effort(event, route)
        return None

    async def acknowledge_best_effort(event: Any, route: str) -> None:
        try:
            session_id = _ack_session_id(event, route, webhook_route())
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
            logger.exception("linear-agent-session: agent session acknowledgement failed")

    return guard_then_acknowledge


def _delivery_route(event: Any) -> Optional[str]:
    source = event.source
    if source.platform.value != "webhook" or not source.user_id.startswith("webhook:"):
        return None
    return source.user_id.removeprefix("webhook:")


def _ack_session_id(event: Any, route: str, webhook_route: str) -> Optional[str]:
    if route != webhook_route:
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
