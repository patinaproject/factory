"""The gateway platform adapter that hosts the upload broker.

Hermes loads this module only inside the gateway. The adapter is not a chat
platform: it receives no messages, `send` always refuses, and it declares no
home channel, so it is never a cron or `send_message` target.
"""

from __future__ import annotations

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from hermes_constants import get_hermes_home

from .broker import Broker

PLATFORM = "factory_uploads"


class FactoryUploadsAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform(PLATFORM))
        repositories = (getattr(config, "extra", None) or {}).get("repositories") or []
        self.broker = Broker(
            get_hermes_home(),
            list(repositories),
            lambda: get_scoped_secret("GITHUB_UPLOAD_TOKEN"),
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
