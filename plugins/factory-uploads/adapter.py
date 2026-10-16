"""The gateway platform adapter that hosts the broker.

Hermes loads this module only inside the gateway. The adapter is not a chat
platform: it receives no messages, `send` always refuses, and it declares no
home channel, so it is never a cron or `send_message` target.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from typing import Callable

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from hermes_cli.config import load_config_readonly
from hermes_constants import get_hermes_home

from .broker import Broker

PLATFORM = "factory_uploads"
LINEAR_PLUGIN = Path("plugins") / "linear-agent-session" / "linear.py"
LINEAR_CREDENTIALS = ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET")


def proxy_token_env(config: dict) -> str:
    entries = (config.get("plugins") or {}).get("entries") or {}
    settings = (entries.get("linear-agent-session") or {}).get("settings") or {}
    return (settings.get("claude_session") or {}).get("auth_token_env") or ""


class GatewayCredentials:
    """Linear revokes every client credentials token of the app when a request asks for another scope set."""

    def __init__(self, home: Path, get_secret: Callable[[str], str | None], proxy_env: str):
        self._home = Path(home)
        self._get_secret = get_secret
        self._proxy_env = proxy_env
        self._lock = threading.Lock()
        self._client = None
        self._client_for: tuple | None = None

    def _app_credentials(self) -> tuple | None:
        values = tuple(self._get_secret(name) for name in LINEAR_CREDENTIALS)
        return values if all(values) else None

    def linear_ready(self) -> bool:
        return self._app_credentials() is not None

    def linear_token(self) -> dict | None:
        credentials = self._app_credentials()
        if credentials is None:
            return None
        with self._lock:
            if self._client_for != credentials:
                linear = self._linear_module()
                cache = linear.token_cache_path({"HERMES_HOME": str(self._home)})
                self._client = linear.LinearClient(*credentials, token_cache_path=cache)
                self._client_for = credentials
            return self._client.issue_token()

    def proxy_token(self) -> str | None:
        return self._get_secret(self._proxy_env) if self._proxy_env else None

    def _linear_module(self):
        spec = importlib.util.spec_from_file_location("factory_uploads_linear", self._home / LINEAR_PLUGIN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class FactoryUploadsAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        repositories = (getattr(config, "extra", None) or {}).get("repositories") or []
        home = get_hermes_home()
        self.broker = Broker(
            home,
            list(repositories),
            lambda: get_scoped_secret("GITHUB_UPLOAD_TOKEN"),
            credentials=GatewayCredentials(home, get_scoped_secret, proxy_token_env(load_config_readonly())),
        )

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not await self.broker.start():
            self._set_fatal_error(
                "already_listening", f"{self.broker.socket_path} already answers.", retryable=False
            )
            return False
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        await self.broker.stop()
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="factory_uploads is not a chat platform")

    async def get_chat_info(self, chat_id):
        return {"name": PLATFORM, "type": "service"}


def check_requirements() -> bool:
    return True


def validate_config(config) -> bool:
    return True


def register(ctx) -> None:
    ctx.register_platform(
        name=PLATFORM,
        label="Factory uploads",
        adapter_factory=FactoryUploadsAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
    )
