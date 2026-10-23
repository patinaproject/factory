"""The one client of the gateway's broker, the `factory-uploads` socket server."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from typing import Callable, Mapping, NamedTuple, Optional

SOCKET_NAME = "factory-uploads.sock"
TIMEOUT_SECONDS = 30
REPLY_LIMIT = 65536
RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0)


class BrokerError(Exception):
    """The broker refused the request, or lost its answer after it may have acted on it."""


class BrokerUnavailable(BrokerError):
    """The broker was busy, unreachable, or slow, which a later run can outlast."""

    def __init__(self, message: str, *, retry: bool = True) -> None:
        super().__init__(message)
        self.retry = retry


class Status(NamedTuple):
    """The broker's answer to `{"op": "status"}`, which names no secret value."""

    token_present: bool
    linear_ready: bool
    proxy_token_present: bool
    mcp_tokens_present: dict
    github_app_ready: bool
    last_state: object
    expires_at: object
    checked_at: object


def socket_path(environ: Mapping[str, str]) -> str:
    """The root home's socket, from the root home or any profile under it."""
    home = os.path.realpath(environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes"))
    parent = os.path.dirname(home)
    root = os.path.dirname(parent) if os.path.basename(parent) == "profiles" else home
    return os.path.join(root, SOCKET_NAME)


def request(
    path: str, op: str, *, timeout: float = TIMEOUT_SECONDS, payload: bytes = b"", sleep: Optional[Callable] = None, **fields
) -> dict:
    """Send one request line, then `payload`, and return the broker's `ok` reply."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return _attempt(path, {"op": op, **fields}, payload, timeout)
        except BrokerUnavailable as error:
            if delay is None or not error.retry:
                raise
            (sleep or time.sleep)(delay)


def status(path: str, *, timeout: float = TIMEOUT_SECONDS) -> Status:
    reply = request(path, "status", timeout=timeout)
    present = reply.get("mcp_tokens_present")
    return Status(
        token_present=reply.get("token_present") is True,
        linear_ready=reply.get("linear_ready") is True,
        proxy_token_present=reply.get("proxy_token_present") is True,
        mcp_tokens_present={server: value is True for server, value in present.items()} if isinstance(present, dict) else {},
        github_app_ready=reply.get("github_app_ready") is True,
        last_state=reply.get("last_state"),
        expires_at=reply.get("expires_at"),
        checked_at=reply.get("checked_at"),
    )


def _attempt(path: str, message: dict, payload: bytes, timeout: float) -> dict:
    line = json.dumps(message).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        try:
            client.connect(path)
            reply = _upload(client, line + payload) if payload else _ask(client, line)
        except TimeoutError:
            raise BrokerUnavailable(f"the gateway's broker at {path} did not answer in {timeout}s", retry=False) from None
        except OSError as error:
            raise BrokerUnavailable(f"the gateway's broker at {path} did not answer: {error.strerror or error}") from None
    try:
        document = json.loads(reply)
    except ValueError:
        if payload:
            raise BrokerError(
                f"the gateway's broker at {path} closed the connection before it answered, so it may have acted on {message['op']}"
            ) from None
        raise BrokerUnavailable(f"the gateway's broker at {path} closed the connection without an answer") from None
    if isinstance(document, dict) and document.get("state") == "busy":
        raise BrokerUnavailable(f"the gateway's broker is busy and refused {message['op']}")
    if not isinstance(document, dict) or document.get("ok") is not True:
        reason = document.get("message") if isinstance(document, dict) else None
        raise BrokerError(f"the gateway's broker refused {message['op']}: {reason or 'no reason given'}")
    return document


def _ask(client: socket.socket, line: bytes) -> bytes:
    client.sendall(line)
    return _read(client)


def _upload(client: socket.socket, data: bytes) -> bytes:
    """Send an upload the broker may act on, so a broken connection leaves it unanswered rather than sent again.

    The broker may refuse before it reads every byte, so a failed send still reads the answer.
    """
    with contextlib.suppress(OSError):
        client.sendall(data)
    with contextlib.suppress(OSError):
        return _read(client)
    return b""


def _read(client: socket.socket) -> bytes:
    reply = b""
    while not reply.endswith(b"\n") and len(reply) < REPLY_LIMIT:
        chunk = client.recv(4096)
        if not chunk:
            break
        reply += chunk
    return reply
