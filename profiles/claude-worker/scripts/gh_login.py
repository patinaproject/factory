"""The worker's gh login: how often it is renewed, where its expiry is saved, and how much must be left to start a run.

`refresh-gh-app-login` writes the expiry and `claude-session` reads it. Installation tokens last one hour and the
broker mints a new one once less than `REFRESH_MARGIN` in plugins/factory-uploads/github_app.py is left, so a run
needs `MIN_REMAINING_SECONDS` plus one refresh interval to stay under that margin.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

EXPIRY_FILE = "token-expiry.json"
REFRESH_INTERVAL_SECONDS = 15 * 60
MIN_REMAINING_SECONDS = 15 * 60
LOCK_FILE = "login.lock"


def write_expiry(config_dir: Path, expires_at: float) -> None:
    """Save the expiry beside hosts.yml, never the token, atomically and readable only by the owner."""
    fd, temp = tempfile.mkstemp(dir=config_dir, prefix=EXPIRY_FILE + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"expires_at": expires_at}, handle)
        os.replace(temp, config_dir / EXPIRY_FILE)
    except BaseException:
        os.unlink(temp)
        raise


def seconds_left(config_dir: Path, now: float) -> float:
    """Seconds until the saved login expires. Raises ValueError that names the problem when the file is unusable."""
    path = config_dir / EXPIRY_FILE
    try:
        expires_at = json.loads(path.read_text())["expires_at"]
    except OSError as error:
        raise ValueError(f"{path} cannot be read ({error.__class__.__name__})") from None
    except (ValueError, KeyError, TypeError):
        raise ValueError(f"{path} does not hold an expires_at") from None
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        raise ValueError(f"{path} does not hold an expires_at")
    return expires_at - now


def is_fresh(config_dir: Path, now: float) -> bool:
    """True when the saved login has `MIN_REMAINING_SECONDS` left, the margin `claude-session` needs to start a run."""
    try:
        return seconds_left(config_dir, now) >= MIN_REMAINING_SECONDS
    except ValueError:
        return False
