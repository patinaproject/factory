"""The GitHub App events the factory needs beyond the `github` route's own."""
from __future__ import annotations

import json
import os
import subprocess

# push starts auto-deploy.
REQUIRED_EVENTS = {"push": "Push"}


class AppLookupError(Exception):
    pass


def fetch_app(slug: str) -> dict:
    proc = subprocess.run([os.environ.get("GH_BIN", "gh"), "api", f"apps/{slug}"], capture_output=True, text=True)
    if proc.returncode != 0:
        lines = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
        raise AppLookupError(f"gh api apps/{slug} exited {proc.returncode}" + (f": {lines[-1]}" if lines else ""))
    try:
        app = json.loads(proc.stdout)
    except ValueError:
        raise AppLookupError(f"gh api apps/{slug} did not print JSON") from None
    if not isinstance(app, dict):
        raise AppLookupError(f"gh api apps/{slug} did not print an object")
    return app


def missing_events(app: dict) -> list[str]:
    """The label GitHub's App settings show for each required event the App does not subscribe to."""
    events = set(app.get("events") or [])
    return [label for event, label in REQUIRED_EVENTS.items() if event not in events]
