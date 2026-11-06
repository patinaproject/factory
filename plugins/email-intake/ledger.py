"""The email ledger: one row per forwarded email, the only record that decides whether an email was seen.

The route script, the fetch service in the gateway, and the mail worker's outcome tool all import this module, so
the delivery shape, the fetch states, and the outcome kinds have one definition. It uses only the standard library
because the route script runs outside Hermes's import path.
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple, Optional

DIRECTORY = "email-intake"
LEDGER = "ledger.db"
SOCKET = "fetch.sock"
IDEMPOTENCY_PREFIX = "email:"
MAX_MESSAGE_ID = 255
# A Message-ID such as <abc.123@mail.example.com>, or the 64-hex raw SHA-256. No slash, backslash, space, or quote,
# so a value can never name a path or break out of a shell word.
MESSAGE_ID = re.compile(r"^[A-Za-z0-9<>@._+=%~!#$&*^{}|?-]+$")
MESSAGE_ID_SOURCES = ("header", "raw-sha256")
OBJECT_KEY = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
RAW_FILE = "message.eml"
EMAIL_FILE = "email.json"
MAIL_PROFILE = "mail-worker"
HEADER_FIELDS = ("from", "reply_to", "x_original_sender", "to", "cc", "subject", "date", "authentication_results")

CLAIMED, CARDED, FETCHING, READY, FAILED = "claimed", "carded", "fetching", "ready", "failed"
TRANSITIONS = {
    CLAIMED: (CARDED, FAILED),
    CARDED: (FETCHING,),
    FETCHING: (READY, FAILED),
    READY: (),
    FAILED: (),
}

NEW_ISSUE, DUPLICATE, NO_ACTION = "new-issue", "duplicate", "no-action"
OUTCOME_KINDS = (NEW_ISSUE, DUPLICATE, NO_ACTION)
ISSUE_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9]{0,9}-[0-9]+$")


class DeliveryError(ValueError):
    pass


class StoredObject(NamedTuple):
    key: str
    size: int
    file: str
    sha256: Optional[str]


class Delivery(NamedTuple):
    message_id: str
    message_id_source: str
    headers: dict
    text: str
    truncated: bool
    objects: tuple
    attachments_truncated: bool

    def to_json(self) -> str:
        return json.dumps({**self._asdict(), "objects": [o._asdict() for o in self.objects]}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Delivery":
        data = json.loads(text)
        return cls(**{**data, "objects": tuple(StoredObject(**o) for o in data["objects"])})


def valid_message_id(value) -> str:
    if not isinstance(value, str) or not value:
        raise DeliveryError("message_id is missing")
    if len(value) > MAX_MESSAGE_ID:
        raise DeliveryError(f"message_id is {len(value)} characters, more than {MAX_MESSAGE_ID}")
    if not MESSAGE_ID.match(value):
        raise DeliveryError("message_id has a character outside the safe set")
    return value


def _size(value, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DeliveryError(f"{where}.size must be a non-negative integer")
    return value


def _key(value, where: str) -> str:
    if not isinstance(value, str) or not OBJECT_KEY.match(value) or ".." in value.split("/"):
        raise DeliveryError(f"{where}.key is not a safe object key")
    return value


def attachment_file(index: int, filename) -> str:
    name = SAFE_NAME.sub("_", filename if isinstance(filename, str) else "").strip("._") or "attachment"
    return f"{index + 1:02d}-{name[:100]}"


def parse_delivery(payload) -> Delivery:
    """Validate a Worker body. A field the factory acts on that is wrong rejects the whole delivery."""
    if not isinstance(payload, dict):
        raise DeliveryError("the body is not a JSON object")
    message_id = valid_message_id(payload.get("message_id"))
    source = payload.get("message_id_source")
    if source not in MESSAGE_ID_SOURCES:
        raise DeliveryError(f"message_id_source must be one of {', '.join(MESSAGE_ID_SOURCES)}")
    raw = payload.get("raw")
    if not isinstance(raw, dict):
        raise DeliveryError("raw is missing")
    objects = [StoredObject(_key(raw.get("key"), "raw"), _size(raw.get("size"), "raw"), RAW_FILE, None)]
    attachments = payload.get("attachments") or []
    if not isinstance(attachments, list):
        raise DeliveryError("attachments must be a list")
    for index, item in enumerate(attachments):
        where = f"attachments[{index}]"
        if not isinstance(item, dict):
            raise DeliveryError(f"{where} must be an object")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256.match(digest.lower()):
            raise DeliveryError(f"{where}.sha256 must be 64 hex digits")
        objects.append(
            StoredObject(
                _key(item.get("key"), where), _size(item.get("size"), where),
                attachment_file(index, item.get("filename")), digest.lower(),
            )
        )
    text = payload.get("text")
    return Delivery(
        message_id,
        source,
        {name: payload.get(name) for name in HEADER_FIELDS if payload.get(name) not in (None, "", [])},
        text if isinstance(text, str) else "",
        payload.get("truncated") is True,
        tuple(objects),
        payload.get("attachments_truncated") is True,
    )


def format_outcome(kind: str, value: str) -> str:
    if kind not in OUTCOME_KINDS:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOME_KINDS)}")
    value = (value or "").strip()
    if kind in (NEW_ISSUE, DUPLICATE) and not ISSUE_IDENTIFIER.match(value):
        raise ValueError(f"{kind} needs a Linear issue identifier such as PAT-123")
    if kind == NO_ACTION and (not value or "\n" in value):
        raise ValueError("no-action needs a one-line reason")
    return f"{kind} {value}"


class Row(NamedTuple):
    message_id: str
    state: str
    card_id: Optional[str]
    reason: Optional[str]
    outcome: Optional[str]
    delivery: Delivery


SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT NOT NULL UNIQUE,
    received_at REAL NOT NULL,
    delivery TEXT NOT NULL,
    state TEXT NOT NULL,
    card_id TEXT UNIQUE,
    reason TEXT,
    outcome TEXT
)
"""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def claim(self, delivery: Delivery) -> bool:
        """True only for the first delivery of a message_id. The insert is the atomic claim."""
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO emails (message_id, received_at, delivery, state) VALUES (?, ?, ?, ?)",
                (delivery.message_id, time.time(), delivery.to_json(), CLAIMED),
            )
            return cursor.rowcount == 1

    def get(self, message_id: str) -> Optional[Row]:
        with self._connect() as conn:
            found = conn.execute(
                "SELECT message_id, state, card_id, reason, outcome, delivery FROM emails WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return None if found is None else Row(*found[:5], Delivery.from_json(found[5]))

    def by_card(self, card_id: str) -> Optional[Row]:
        with self._connect() as conn:
            found = conn.execute("SELECT message_id FROM emails WHERE card_id = ?", (card_id,)).fetchone()
        return None if found is None else self.get(found[0])

    def move(self, message_id: str, start: str, end: str, *, reason: Optional[str] = None, card_id: Optional[str] = None) -> bool:
        """Compare-and-set one state transition, so two actors never both win the same step."""
        if end not in TRANSITIONS[start]:
            raise ValueError(f"no transition from {start} to {end}")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE emails SET state = ?, reason = COALESCE(?, reason), card_id = COALESCE(?, card_id)"
                " WHERE message_id = ? AND state = ?",
                (end, reason, card_id, message_id, start),
            )
            return cursor.rowcount == 1

    def record_outcome(self, card_id: str, outcome: str) -> bool:
        """Write the outcome once. A second call for the same card changes nothing and returns False."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE emails SET outcome = ? WHERE card_id = ? AND state = ? AND outcome IS NULL",
                (outcome, card_id, READY),
            )
            return cursor.rowcount == 1

    def rows(self) -> list:
        with self._connect() as conn:
            ids = [found[0] for found in conn.execute("SELECT message_id FROM emails ORDER BY received_at")]
        return [self.get(message_id) for message_id in ids]


def ledger_path(root: Path) -> Path:
    return Path(root) / DIRECTORY / LEDGER


def socket_path(root: Path) -> Path:
    return Path(root) / DIRECTORY / SOCKET
