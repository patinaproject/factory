"""The gateway half of the email route: create each email's card and fetch its stored objects.

It holds the route's signing secret, so it runs only in the gateway. A caller names a message_id and nothing else:
the object keys come from that message's ledger row, the host from settings, and the destination from the card.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socketserver
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple, Optional

try:
    from . import ledger as ledger_module
except ImportError:
    import ledger as ledger_module

logger = logging.getLogger("email_intake")

CHUNK = 64 * 1024
FETCH_TIMEOUT = 30
MAX_REQUEST = 4096


class FetchError(Exception):
    pass


class Board(NamedTuple):
    """The Kanban operations the service needs; the gateway adapter binds them to Hermes's board."""

    create_card: Callable[[ledger_module.Delivery], str]
    attachments_dir: Callable[[str], Path]
    unblock: Callable[[str], None]
    comment: Callable[[str, str], None]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the signed headers to a host or key that the ledger never named."""

    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect).open


def signature(secret: str, timestamp: str, path: str) -> str:
    return hmac.new(secret.encode(), f"GET.{timestamp}.{path}".encode(), hashlib.sha256).hexdigest()


def object_url(host: str, key: str) -> str:
    return f"https://{host}/{urllib.parse.quote(key)}"


def signed_get(url: str, secret: str, opener=OPENER):
    path = urllib.parse.urlsplit(url).path
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        headers={"X-Webhook-Timestamp": timestamp, "X-Webhook-Signature-V2": signature(secret, timestamp, path)},
    )
    return opener(request, timeout=FETCH_TIMEOUT)


def _verified(path: Path, item: ledger_module.StoredObject) -> bool:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return path.stat().st_size == item.size and (item.sha256 is None or digest.hexdigest() == item.sha256)


def fetch_object(item: ledger_module.StoredObject, url: str, secret: str, destination: Path, opener) -> None:
    target = destination / item.file
    if target.exists():
        if _verified(target, item):
            return
        raise FetchError(f"{item.file} ({item.key}) is already on disk and does not match the listed object")
    partial = destination / f".{item.file}.partial"
    digest = hashlib.sha256()
    size = 0
    try:
        with signed_get(url, secret, opener) as response:
            with open(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
                for block in iter(lambda: response.read(CHUNK), b""):
                    size += len(block)
                    if size > item.size:
                        raise FetchError(f"{item.file} ({item.key}) is larger than the listed {item.size} bytes")
                    digest.update(block)
                    handle.write(block)
    except FetchError:
        partial.unlink(missing_ok=True)
        raise
    except Exception as error:
        partial.unlink(missing_ok=True)
        raise FetchError(f"{item.file} ({item.key}) could not be fetched: {type(error).__name__}") from None
    if size != item.size:
        partial.unlink(missing_ok=True)
        raise FetchError(f"{item.file} ({item.key}) is {size} bytes, not the listed {item.size}")
    if item.sha256 is not None and digest.hexdigest() != item.sha256:
        partial.unlink(missing_ok=True)
        raise FetchError(f"{item.file} ({item.key}) has SHA-256 {digest.hexdigest()}, not the listed {item.sha256}")
    os.link(partial, target)
    partial.unlink()


def email_document(delivery: ledger_module.Delivery) -> bytes:
    files = [{"file": item.file, "size": item.size, "sha256": item.sha256} for item in delivery.objects]
    return json.dumps(
        {
            "message_id": delivery.message_id,
            "headers": delivery.headers,
            "text": delivery.text,
            "text_truncated": delivery.truncated,
            "files": files,
            "attachments_truncated": delivery.attachments_truncated,
        },
        indent=2,
        sort_keys=True,
    ).encode()


def write_email(destination: Path, delivery: ledger_module.Delivery) -> None:
    target = destination / ledger_module.EMAIL_FILE
    data = email_document(delivery)
    if target.exists():
        if target.read_bytes() != data:
            raise FetchError(f"{ledger_module.EMAIL_FILE} is already on disk with other content")
        return
    with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)


class Intake:
    """Moves each ledger row through its states. One instance per gateway serializes card creation."""

    def __init__(self, ledger: ledger_module.Ledger, board: Board, host: str, secret: Callable[[], Optional[str]], opener=None):
        self.ledger = ledger
        self.board = board
        self.host = host
        self.secret = secret
        self.opener = opener or OPENER
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()

    def deliver(self, message_id: str) -> dict:
        """Create the card for a claimed row, then fetch its objects. Idempotent: a later call resumes or does nothing."""
        message_id = ledger_module.valid_message_id(message_id)
        with self._lock:
            row = self.ledger.get(message_id)
            if row is None:
                return {"ok": False, "error": "unknown message_id"}
            if row.state == ledger_module.CLAIMED:
                card_id = self.board.create_card(row.delivery)
                self.ledger.move(message_id, ledger_module.CLAIMED, ledger_module.CARDED, card_id=card_id)
                row = self.ledger.get(message_id)
            if row.state != ledger_module.CARDED:
                return {"ok": True, "state": row.state, "card_id": row.card_id}
            self.ledger.move(message_id, ledger_module.CARDED, ledger_module.FETCHING)
        return {"ok": True, "state": ledger_module.FETCHING, "card_id": row.card_id}

    def fetch(self, message_id: str) -> str:
        with self._fetch_lock:
            return self._fetch(message_id)

    def _fetch(self, message_id: str) -> str:
        row = self.ledger.get(message_id)
        if row is None or row.state != ledger_module.FETCHING:
            raise FetchError("the message is not waiting for a fetch")
        secret = self.secret()
        destination = self.board.attachments_dir(row.card_id)
        try:
            if not secret:
                raise FetchError("the email route's signing secret is not set in the gateway")
            destination.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_email(destination, row.delivery)
            for item in row.delivery.objects:
                fetch_object(item, object_url(self.host, item.key), secret, destination, self.opener)
        except FetchError as error:
            reason = f"Attachment fetch failed: {error}"
            self.ledger.move(message_id, ledger_module.FETCHING, ledger_module.FAILED, reason=reason)
            self.board.comment(row.card_id, reason)
            logger.error("email-intake: %s: %s", row.card_id, reason)
            return ledger_module.FAILED
        self.ledger.move(message_id, ledger_module.FETCHING, ledger_module.READY)
        self.board.unblock(row.card_id)
        return ledger_module.READY

    def deliver_and_fetch(self, message_id: str) -> dict:
        reply = self.deliver(message_id)
        if reply.get("state") == ledger_module.FETCHING:
            threading.Thread(target=self._fetch_logged, args=(message_id,), daemon=True).start()
        return reply

    def _fetch_logged(self, message_id: str) -> None:
        try:
            self.fetch(message_id)
        except Exception:
            logger.exception("email-intake: fetch for a ledger row stopped")

    def resume(self) -> list:
        """After a gateway restart, finish every row that a restart interrupted."""
        resumed = []
        for row in self.ledger.rows():
            if row.state in (ledger_module.CLAIMED, ledger_module.CARDED):
                self.deliver(row.message_id)
            if self.ledger.get(row.message_id).state == ledger_module.FETCHING:
                self._fetch_logged(row.message_id)
                resumed.append(row.message_id)
        return resumed


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            request = json.loads(self.rfile.readline(MAX_REQUEST))
            if not isinstance(request, dict) or request.get("op") != "deliver":
                raise ValueError("the only operation is deliver")
            reply = self.server.intake.deliver_and_fetch(request.get("message_id"))
        except Exception as error:
            reply = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, intake: Intake):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(old)
        self.intake = intake
        self.path = path

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self.path.unlink(missing_ok=True)
