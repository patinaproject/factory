from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple


LIMIT = 3
PULL = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)")
MARKER = re.compile(
    r"code-review round ([0-9]+) pr ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+) "
    r"base ([0-9a-f]{40}) head ([0-9a-f]{40}) blocking ([0-9]+)"
)
FENCE = re.compile(r"```json\n(.*)\n```\s*$", re.DOTALL)
FIELDS = ("id", "axis", "severity", "location", "text", "state")
AXES = ("Standards", "Spec", "Evidence", "Bugs", "Security")
SEVERITIES = ("Important", "Nit", "Pre-existing")
STATES = ("open", "answered")


class Round(NamedTuple):
    number: int
    base: str
    head: str
    blocking: int
    findings: list | None


class Status(NamedTuple):
    rounds: list[Round]

    @property
    def last(self) -> Round | None:
        return self.rounds[-1] if self.rounds else None

    @property
    def exhausted(self) -> bool:
        return len(self.rounds) >= LIMIT and self.rounds[-1].blocking > 0


def linear_module():
    plugin = Path(os.environ["HERMES_HOME"]) / "plugins" / "linear-agent-session"
    sys.path.insert(0, str(plugin))
    return importlib.import_module("linear")



def blocking_count(findings: list[dict]) -> int:
    return sum(
        1
        for finding in findings
        if finding["state"] == "open"
        and (finding["severity"] == "Important" or (finding["axis"] == "Security" and finding["severity"] != "Pre-existing"))
    )


def parse_findings(data) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("the findings must be a JSON list of records")
    allowed = {"axis": AXES, "severity": SEVERITIES, "state": STATES}
    for at, record in enumerate(data):
        if not isinstance(record, dict) or set(record) != set(FIELDS):
            raise ValueError(f"record {at} must have exactly the fields {', '.join(FIELDS)}")
        for field in FIELDS:
            if not isinstance(record[field], str):
                raise ValueError(f"record {at}: {field} must be a string")
        for field, values in allowed.items():
            if record[field] not in values:
                raise ValueError(f"record {at}: {field} must be one of {', '.join(values)}")
    return data


def marker(number: int, pull: str, base: str, head: str, blocking: int) -> str:
    line = f"code-review round {number} pr {pull} base {base} head {head} blocking {blocking}"
    if not MARKER.fullmatch(line):
        raise ValueError(f"{line!r} is not a round marker; the shas must be 40 lowercase hex digits")
    return line


def round_body(line: str, findings: list[dict]) -> str:
    return f"{line}\n\n```json\n{json.dumps(findings, indent=2)}\n```"


def parse_round(body: str, pull: str) -> Round | None:
    first, _, rest = body.partition("\n")
    match = MARKER.fullmatch(first)
    if match is None or match[2].lower() != pull.lower():
        return None
    try:
        findings = parse_findings(json.loads(FENCE.search(rest.strip()).group(1)))
    except (AttributeError, ValueError):
        findings = None
    return Round(int(match[1]), match[3], match[4], int(match[5]), findings)


def read_status(client, session_id: str, pull: str, tick=lambda: None) -> Status:
    """Every round recorded for `pull` on any session of the card's issue, oldest first."""
    sessions = client.issue_session_ids(session_id)
    found = []
    for each in sessions if session_id in sessions else [*sessions, session_id]:
        tick()
        for activity in client.read_session(each)["activities"]:
            if activity["type"] != "thought":
                continue
            recorded = parse_round(activity["body"], pull)
            if recorded:
                found.append((activity.get("createdAt", ""), recorded))
    return Status([recorded for _, recorded in sorted(found, key=lambda item: item[0])])


def findings_path(pull: str) -> Path:
    return Path(tempfile.gettempdir()) / f"code-review-{re.sub(r'[^A-Za-z0-9]+', '-', pull)}-findings.json"
