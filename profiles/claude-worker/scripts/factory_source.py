"""Find the factory checkout a worker home was installed from.

The worker home's `distribution.yaml` names the profile directory that Hermes
installed, so its `source:` line leads back to the checkout. check-install
compares the install with that checkout, and claude-session gives triage cards
the playbook that the checkout vendors, so every repository a card triages gets
the factory's own playbook.
"""
from __future__ import annotations

import re
from pathlib import Path

TRIAGE_PLAYBOOK = Path(".agents/skills/patina-mode/playbooks/triage.md")


class SourceError(Exception):
    pass


def checkout(home: Path) -> Path:
    distribution = home / "distribution.yaml"
    if not distribution.is_file():
        raise SourceError(f"{distribution} does not exist")
    for line in distribution.read_text().splitlines():
        match = re.match(r"^source:\s*['\"]?(.+?)['\"]?\s*$", line)
        if match:
            return Path(match.group(1)).parents[1]
    raise SourceError(f"{distribution} has no source: line")
