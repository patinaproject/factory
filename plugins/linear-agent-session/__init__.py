from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
from typing import Any, Callable, Mapping, Optional

from . import blocks, linear, missing_secret, replies, tools, triage_exit

logger = logging.getLogger(__name__)

TOOLSET = "linear_agent_session"
ACK_BODY = "Received. Triaging now."
# Linear marks a session unresponsive after 10 s without an activity; leave room for token minting.
ACK_TIMEOUT_SECONDS = 5.0
# Hermes does not bound pre_gateway_dispatch, and the webhook already answered 202, so a bulk move can wait its
# turn. This backstop only frees a delivery whose Kanban read or archive hangs; the delivery then goes to triage.
TRIAGE_EXIT_TIMEOUT_SECONDS = 300.0
# Each archive runs `hermes kanban` three times. Its own pool keeps a bulk move off the acknowledgements' pool.
TRIAGE_EXIT_WORKERS = 4
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
    empty = missing_secret.worker_empty_key_env(load_config_readonly(), os.environ)
    if empty is not None:
        logger.error(
            "linear-agent-session: %s is empty in this worker; a provider 401 or 403 requeues the card", empty
        )

    client: Optional[linear.LinearClient] = None

    def get_client() -> linear.LinearClient:
        nonlocal client
        if client is None:
            client = linear.client_from_env()
        return client

    posts = replies.PostRecord()
    ctx.register_tool(
        name="linear_agent_activity",
        toolset=TOOLSET,
        schema=tools.ACTIVITY_SCHEMA,
        handler=replies.make_activity_handler(posts, lambda args: tools.run_activity(get_client, args)),
        description=tools.ACTIVITY_SCHEMA["description"],
    )
    ctx.register_tool(
        name="linear_agent_activity_read",
        toolset=TOOLSET,
        schema=tools.ACTIVITY_READ_SCHEMA,
        handler=lambda args, **_: tools.run_activity_read(get_client, args),
        description=tools.ACTIVITY_READ_SCHEMA["description"],
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
    ctx.register_tool(
        name="linear_issue_to_todo",
        toolset=TOOLSET,
        schema=tools.TO_TODO_SCHEMA,
        handler=lambda args, **_: tools.run_to_todo(get_client, args),
        description=tools.TO_TODO_SCHEMA["description"],
    )
    ctx.register_hook(
        "pre_gateway_dispatch",
        make_dispatch_hook(
            get_client,
            lambda: ctx.get_config("webhook_route", default="linear"),
            load_config_readonly,
            triage_exit.handler(
                lambda: ctx.get_config("app_user_id", default=""),
                get_client,
                triage_exit.kanban_cards,
                triage_exit.gateway_home,
            ),
        ),
    )
    ctx.register_hook("kanban_task_blocked", blocks.make_blocked_hook(get_client))
    ctx.register_hook(
        "post_llm_call",
        replies.make_turn_hook(
            posts,
            get_client,
            replies.spawn_reask,
            lambda: replies.route_toolsets(load_config_readonly(), ctx.get_config("webhook_route", default="linear")),
        ),
    )
    ctx.register_hook(
        "transform_api_error_classification", missing_secret.make_classification_hook(load_config_readonly, os.environ)
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
    handle_triage_exit: Callable[[Any], triage_exit.Verdict],
) -> Callable[..., Any]:
    pool = concurrent.futures.ThreadPoolExecutor(TRIAGE_EXIT_WORKERS, thread_name_prefix="triage-exit")

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
        return await archive_triage_exit(event, route)

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

    async def archive_triage_exit(event: Any, route: str) -> Optional[dict[str, str]]:
        """Skip the turn only when the card was archived or left to its worker. Any doubt or error sends it to triage."""
        if route != webhook_route() or triage_exit.payload_passthrough(event.raw_message) is not None:
            return None
        try:
            loop = asyncio.get_running_loop()
            verdict = await asyncio.wait_for(
                loop.run_in_executor(pool, handle_triage_exit, event.raw_message), timeout=TRIAGE_EXIT_TIMEOUT_SECONDS
            )
        except Exception as failure:
            logger.warning(
                "linear-agent-session: sent a Triage exit to triage: %s",
                blocks.hermes_redact(f"{type(failure).__name__}: {failure}"),
            )
            return None
        if verdict.action == "failed":
            logger.warning("linear-agent-session: sent a Triage exit to triage: %s", blocks.hermes_redact(verdict.reason))
        if not verdict.handled:
            return None
        logger.info("linear-agent-session: %s; no triage turn", verdict.reason)
        return {"action": "skip", "reason": verdict.reason}

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
