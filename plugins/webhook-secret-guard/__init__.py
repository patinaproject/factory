from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

REFERENCE = re.compile(r"^\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}$")


def unresolved_routes(config: Mapping[str, Any]) -> dict[str, str]:
    extra = ((config.get("platforms") or {}).get("webhook") or {}).get("extra") or {}
    global_secret = extra.get("secret", "")
    unresolved = {}
    for name, route in (extra.get("routes") or {}).items():
        secret = route.get("secret", global_secret)
        match = REFERENCE.match(secret) if isinstance(secret, str) else None
        if match:
            unresolved[name] = match.group(1)
    return unresolved


def delivery_route(event: Any) -> Optional[str]:
    source = event.source
    if source.platform.value != "webhook" or not source.user_id.startswith("webhook:"):
        return None
    return source.user_id.removeprefix("webhook:")


def make_hook(load_config: Callable[[], Mapping[str, Any]]) -> Callable[..., Optional[dict[str, str]]]:
    def reject_unresolved(event: Any, **_: Any) -> Optional[dict[str, str]]:
        route = delivery_route(event)
        if route is None:
            return None
        variable = unresolved_routes(load_config()).get(route)
        if variable is None:
            return None
        logger.error(
            "webhook-secret-guard: dropped a delivery on route %s: secret is the unresolved reference ${%s}",
            route,
            variable,
        )
        return {"action": "skip", "reason": f"route {route} secret is the unresolved reference ${{{variable}}}"}

    return reject_unresolved


def register(ctx: Any) -> None:
    from hermes_cli.config import load_config_readonly

    for route, variable in unresolved_routes(load_config_readonly()).items():
        logger.error(
            "webhook-secret-guard: route %s secret is the unresolved reference ${%s}; its deliveries will be dropped",
            route,
            variable,
        )
    ctx.register_hook("pre_gateway_dispatch", make_hook(load_config_readonly))
