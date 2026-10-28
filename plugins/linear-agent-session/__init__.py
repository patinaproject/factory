from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
from typing import Any, Callable, Mapping, Optional

from . import blocks, linear, missing_secret, replies, tools, triage_exit, triage_intent

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
TRIAGE_INTENT_TIMEOUT_SECONDS = 60.0
# Hermes leaves a secret like this verbatim when its variable is unset, and anyone can sign with that text.
UNRESOLVED_REFERENCE = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}$")
# Linear writes a mention in an agent activity body as `<user id="<user id>" notify>name</user>`.
MENTION = re.compile(r"<user\b[^>]*\bid=\"([^\"]+)\"")
TEAMMATE_REPLY = "the session reply mentions teammates and not the agent"


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
    ctx.register_tool(
        name="linear_issue_edit",
        toolset=TOOLSET,
        schema=tools.EDIT_SCHEMA,
        handler=lambda args, **_: tools.run_edit(get_client, args),
        description=tools.EDIT_SCHEMA["description"],
    )
    ctx.register_tool(
        name="linear_issue_search",
        toolset=TOOLSET,
        schema=tools.SEARCH_SCHEMA,
        handler=lambda args, **_: tools.run_search(get_client, args),
        description=tools.SEARCH_SCHEMA["description"],
    )
    ctx.register_tool(
        name="github_issue_edit",
        toolset=TOOLSET,
        schema=tools.GITHUB_EDIT_SCHEMA,
        handler=lambda args, **_: tools.run_github_edit(args),
        description=tools.GITHUB_EDIT_SCHEMA["description"],
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
            lambda payload: triage_intent.compute(
                payload,
                app_user_id=lambda: ctx.get_config("app_user_id", default=""),
                client=get_client,
                cards=triage_exit.kanban_cards,
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


def paused() -> bool:
    from agent.estop import paused_reply

    return paused_reply() is not None


def make_dispatch_hook(
    get_client: Callable[[], linear.LinearClient],
    webhook_route: Callable[[], str],
    load_config: Callable[[], Mapping[str, Any]],
    handle_triage_exit: Callable[[Any], triage_exit.Verdict],
    compute_intent: Optional[Callable[[Any], triage_intent.Intent]] = None,
) -> Callable[..., Any]:
    pool = concurrent.futures.ThreadPoolExecutor(TRIAGE_EXIT_WORKERS, thread_name_prefix="triage-exit")
    # A bulk move can hold every triage-exit worker for minutes, and an intent must not wait behind it.
    intent_pool = concurrent.futures.ThreadPoolExecutor(TRIAGE_EXIT_WORKERS, thread_name_prefix="triage-intent")

    async def guard_then_acknowledge(event: Any, **_: Any) -> Optional[dict[str, str]]:
        if paused():
            return None
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
        if route == webhook_route() and addressed_to_teammate(event.raw_message):
            logger.info("linear-agent-session: %s; no acknowledgement or triage turn", TEAMMATE_REPLY)
            return {"action": "skip", "reason": TEAMMATE_REPLY}
        await acknowledge_best_effort(event, route)
        skipped = await archive_triage_exit(event, route)
        if skipped is not None or route != webhook_route() or compute_intent is None:
            return skipped
        return await prepend_intent(event)

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

    def compute_twice(payload: Any) -> triage_intent.Intent:
        """An `error` intent ends the turn, and an `Issue` event has no session to say so on, so one failed read gets a second try."""
        intent = compute_intent(payload)
        return compute_intent(payload) if intent.action == "error" else intent

    async def prepend_intent(event: Any) -> dict[str, str]:
        try:
            loop = asyncio.get_running_loop()
            intent = await asyncio.wait_for(
                loop.run_in_executor(intent_pool, compute_twice, event.raw_message), timeout=TRIAGE_INTENT_TIMEOUT_SECONDS
            )
        except Exception as failure:
            message = (
                f"the intent took longer than {TRIAGE_INTENT_TIMEOUT_SECONDS:.0f} s"
                if isinstance(failure, asyncio.TimeoutError)
                else blocks.hermes_redact(f"{type(failure).__name__}: {failure}")
            )
            logger.warning("linear-agent-session: no triage intent: %s", message)
            intent = triage_intent.error_intent(message)
        return {"action": "rewrite", "text": triage_intent.render(intent) + event.text}

    return guard_then_acknowledge


def _delivery_route(event: Any) -> Optional[str]:
    source = event.source
    if source.platform.value != "webhook" or not source.user_id.startswith("webhook:"):
        return None
    return source.user_id.removeprefix("webhook:")


def addressed_to_teammate(payload: Any) -> bool:
    """A session reply that mentions people but not the agent app is one teammate talking to another."""
    if not isinstance(payload, dict) or payload.get("type") != "AgentSessionEvent" or payload.get("action") != "prompted":
        return False
    activity = payload.get("agentActivity") or {}
    body = (activity.get("content") or {}).get("body")
    app_user_id = payload.get("appUserId")
    if activity.get("signal") == "stop" or not isinstance(body, str) or not app_user_id:
        return False
    mentioned = set(MENTION.findall(body))
    return bool(mentioned) and app_user_id not in mentioned


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
