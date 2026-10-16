"""The gateway's broker: a Unix socket that turns image bytes into attachment URLs
and hands the worker the two credentials it may hold.

It imports nothing from Hermes, so its tests run without a gateway. The
adapter owns its lifecycle. The broker never opens a path a caller names,
never returns the upload token, and never writes a token to a log.

The worker home holds no factory secret of its own. It asks here for a Linear
access token, minted with the gateway's Linear app credentials, which never
leave the gateway, and for the proxy token that its own agent loop and Claude
Code send to the model proxy.

Wire protocol, one request per connection:

    {"op": "upload", "repo": "owner/repo", "name": "a.png", "size": 123}\\n<size raw bytes>
    {"op": "linear_token"}\\n
    {"op": "proxy_token"}\\n
    {"op": "status"}\\n

Every reply is one JSON line.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Callable, Protocol

from . import upload_token

logger = logging.getLogger(__name__)

SOCKET_NAME = "factory-uploads.sock"
STATE_DIR = "factory-uploads"
HOURLY_QUOTA = 60
QUOTA_WINDOW = 3600
REQUEST_DEADLINE = 70
MAX_CONNECTIONS = 4
MAX_HEADER = 4096
MAX_NAME = 200

FIXED_MESSAGES = {
    "unavailable": "The upload token is not available in the gateway.",
    "rejected": "The upload token was rejected (expired or revoked).",
    "no-access": "The upload token lacks write on the repository.",
    "error": "The upload failed. The gateway log has the detail.",
}
CREDENTIAL_MESSAGES = {
    "linear_token": "The gateway holds no Linear app credentials.",
    "proxy_token": "The gateway holds no proxy token.",
}
LINEAR_FAILED = "The gateway could not issue a Linear token. The gateway log has the detail."
BUSY = "The upload broker is busy. Try again in a moment."
BAD_REQUEST = "The request is not a valid upload request."
NOT_ALLOWED = "That repository is not on the upload allowlist."
OVER_QUOTA = f"The upload limit of {HOURLY_QUOTA} files an hour is used up."


class Refusal(Exception):
    """A request the broker answers with a fixed message, before any upload."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


class Credentials(Protocol):
    def linear_ready(self) -> bool: ...

    def linear_token(self) -> dict | None:
        """{"access_token", "expires_at"} Linear accepts now, or None without app credentials. May block."""

    def proxy_token(self) -> str | None: ...


class NoCredentials:
    def linear_ready(self) -> bool:
        return False

    def linear_token(self) -> dict | None:
        return None

    def proxy_token(self) -> str | None:
        return None


def peer_pid(sock: socket.socket) -> int | None:
    if sys.platform == "darwin":
        return sock.getsockopt(0, 2)  # SOL_LOCAL, LOCAL_PEERPID
    if sys.platform.startswith("linux"):
        import struct

        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
    return None


class Broker:
    def __init__(
        self,
        home: Path,
        repositories: list[str],
        get_token: Callable[[], str | None],
        *,
        resolve_repository_id: Callable[[str], int] = upload_token.repository_id,
        upload: Callable = upload_token.upload,
        base_url: str = upload_token.UPLOAD_BASE,
        clock: Callable[[], float] = time.time,
        credentials: Credentials = NoCredentials(),
    ):
        self.home = Path(home)
        self.socket_path = self.home / SOCKET_NAME
        self.state_dir = self.home / STATE_DIR
        self.repositories = frozenset(repositories)
        self._get_token = get_token
        self._resolve_repository_id = resolve_repository_id
        self._upload = upload
        self._base_url = base_url
        self._clock = clock
        self._credentials = credentials
        self._repository_ids: dict[str, int] = {}
        self._recent: list[float] = []
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[asyncio.Task, asyncio.StreamWriter] = {}

    async def start(self) -> bool:
        """Listen on the socket. False when another gateway already listens on it."""
        if await self._answers():
            return False
        self.socket_path.unlink(missing_ok=True)
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        return True

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            # Since Python 3.12, wait_closed waits for every open client, so end them first.
            for task, writer in list(self._clients.items()):
                task.cancel()
                writer.close()
            await asyncio.gather(*self._clients, return_exceptions=True)
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    async def _answers(self) -> bool:
        if not self.socket_path.exists():
            return False
        try:
            _, writer = await asyncio.open_unix_connection(str(self.socket_path))
        except OSError:
            return False
        writer.close()
        await writer.wait_closed()
        return True

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if len(self._clients) >= MAX_CONNECTIONS:
            reply = self._failure("refused", BUSY)
        else:
            self._clients[task] = writer
            try:
                reply = await asyncio.wait_for(self._reply(reader, writer), REQUEST_DEADLINE)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                reply = None
            except Exception:
                logger.exception("factory-uploads: unexpected error while serving a request")
                reply = self._failure("error", FIXED_MESSAGES["error"])
            finally:
                self._clients.pop(task, None)
        if reply is not None:
            with contextlib.suppress(ConnectionError):
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        writer.close()

    async def _reply(self, reader, writer) -> dict:
        try:
            request = json.loads(await reader.readline())
            op = request["op"]
        except (ValueError, KeyError, TypeError):
            return self._failure("refused", BAD_REQUEST)
        if op == "status":
            return self._status()
        pid = peer_pid(writer.get_extra_info("socket"))
        if op in CREDENTIAL_MESSAGES:
            return await self._credential_request(op, pid)
        if op != "upload":
            return self._failure("refused", BAD_REQUEST)
        return await self._upload_request(request, reader, pid)

    async def _credential_request(self, op: str, pid: int | None) -> dict:
        if op == "proxy_token":
            token, expires_at = self._credentials.proxy_token(), None
        else:
            try:
                issued = await asyncio.to_thread(self._credentials.linear_token)
            except Exception as error:
                logger.warning("factory-uploads: Linear token for pid %s failed: %s", pid, error)
                self._audit({"op": op, "pid": pid, "state": "error"})
                return self._failure("error", LINEAR_FAILED)
            token, expires_at = (issued["access_token"], issued["expires_at"]) if issued else (None, None)
        if not token:
            self._audit({"op": op, "pid": pid, "state": "unavailable"})
            return self._failure("unavailable", CREDENTIAL_MESSAGES[op])
        self._audit({"op": op, "pid": pid, "state": "ok"})
        return {"ok": True, "token": token, "expires_at": expires_at}

    async def _upload_request(self, request: dict, reader, pid: int | None) -> dict:
        repo, name, size = request.get("repo"), request.get("name"), request.get("size")
        entry = {"op": "upload", "repo": repo, "name": name, "size": size, "pid": pid}
        try:
            self._check_shape(repo, name, size)
            data = await reader.readexactly(size)
            if repo not in self.repositories:
                raise Refusal("refused", NOT_ALLOWED)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            upload_token.image_type(data)
            url, expires_at = await self._send(repo, name, data)
        except Refusal as refusal:
            return self._finish(entry, refusal.state, str(refusal))
        except upload_token.UploadError as error:
            logger.warning("factory-uploads: %s upload %s: %s", repo, error.state, error)
            message = str(error) if error.state == "refused" else FIXED_MESSAGES[error.state]
            return self._finish(entry, error.state, message)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            raise
        except Exception:
            logger.exception("factory-uploads: %s upload failed unexpectedly", repo)
            return self._finish(entry, "error", FIXED_MESSAGES["error"])
        return self._finish(entry, "ok", url=url, expires_at=expires_at)

    @staticmethod
    def _check_shape(repo, name, size) -> None:
        if not (isinstance(repo, str) and isinstance(name, str) and isinstance(size, int)):
            raise Refusal("refused", BAD_REQUEST)
        if isinstance(size, bool) or not 0 < size <= upload_token.MAX_BYTES:
            raise Refusal("refused", BAD_REQUEST)
        if not name or len(name) > MAX_NAME or "\x00" in name:
            raise Refusal("refused", BAD_REQUEST)

    async def _send(self, repo: str, name: str, data: bytes) -> tuple[str, str | None]:
        token = self._get_token()
        if not token:
            raise upload_token.UploadError("unavailable", FIXED_MESSAGES["unavailable"])
        now = self._clock()
        self._recent = [t for t in self._recent if now - t < QUOTA_WINDOW]
        if len(self._recent) >= HOURLY_QUOTA:
            raise Refusal("refused", OVER_QUOTA)
        self._recent.append(now)
        if repo not in self._repository_ids:
            self._repository_ids[repo] = await asyncio.to_thread(self._resolve_repository_id, repo)
        url, expires_at, _ = await asyncio.to_thread(
            self._upload, name, data, self._repository_ids[repo], token, self._base_url
        )
        return url, expires_at

    def _finish(self, entry: dict, state: str, message: str = "", url: str = "", expires_at=None) -> dict:
        self._audit({**entry, "state": state, "url": url or None})
        self._record_status(state, entry.get("repo"), expires_at)
        if state != "ok":
            return self._failure(state, message)
        return {"ok": True, "url": url}

    @staticmethod
    def _failure(state: str, message: str) -> dict:
        return {"ok": False, "state": state, "message": message}

    def _status(self) -> dict:
        record = self._read_status()
        return {
            "ok": True,
            "token_present": bool(self._get_token()),
            "linear_ready": self._credentials.linear_ready(),
            "proxy_token_present": bool(self._credentials.proxy_token()),
            "last_state": record.get("state"),
            "expires_at": record.get("expires_at"),
            "checked_at": record.get("checked_at"),
        }

    def _read_status(self) -> dict:
        path = self.state_dir / "status.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _record_status(self, state: str, repo, expires_at) -> None:
        if state == "refused":
            return
        previous = self._read_status()
        record = upload_token.status_record(state, str(repo), expires_at or previous.get("expires_at"))
        upload_token.write_status(self.state_dir / "status.json", record)

    def _audit(self, entry: dict) -> None:
        line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())), **entry}
        fd = os.open(self.state_dir / "log.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(line) + "\n")
