from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Callable, Mapping, Optional

from .linear import LinearError

KEY_START = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def store_dir(environ: Mapping[str, str]) -> Optional[str]:
    if environ.get("HERMES_HOME"):
        return os.path.join(environ["HERMES_HOME"], "plugin-data", "linear-agent-session", "idempotency")
    return None


def check_key(key) -> str:
    if not isinstance(key, str) or not KEY_START.match(key):
        raise LinearError("pass idempotency_key, a string that starts with a UUID, such as <Activity id>:<item number>")
    return key


def once(directory: Optional[str], tool: str, key: str, args: Mapping, send: Callable[[], dict]) -> dict:
    """Run `send` at most once for `key`. A repeat returns the first result and sends nothing.

    The record is created exclusively before the request, so a concurrent or crashed call finds it and refuses
    instead of sending twice. A failed send deletes the record, so a failure is never cached."""
    check_key(key)
    if directory is None:
        raise LinearError("HERMES_HOME is not set, so there is no idempotency store")
    digest = hashlib.sha256(
        json.dumps([tool, {k: v for k, v in args.items() if k != "idempotency_key"}], sort_keys=True).encode()
    ).hexdigest()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, hashlib.sha256(key.encode()).hexdigest() + ".json")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _repeat(path, key, digest)
    with os.fdopen(fd, "w") as f:
        json.dump({"args_digest": digest, "state": "reserved"}, f)
    try:
        result = send()
    except BaseException:
        os.unlink(path)
        raise
    # The temporary name keeps a reader from ever seeing a half-written result.
    with open(path + ".tmp", "w") as f:
        json.dump({"args_digest": digest, "state": "done", "result": result}, f)
    os.replace(path + ".tmp", path)
    return result


def _repeat(path: str, key: str, digest: str) -> dict:
    try:
        with open(path) as f:
            record = json.load(f)
    except (FileNotFoundError, ValueError):
        record = {"state": "reserved", "args_digest": digest}
    if record["args_digest"] != digest:
        raise LinearError(f"idempotency_key {key!r} was used with different arguments; pass a new key")
    if record["state"] != "done":
        raise LinearError(f"a call with idempotency_key {key!r} is still running or did not finish; nothing was sent again")
    return record["result"]
