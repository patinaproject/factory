"""The versioned common result schema."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().with_name("result.schema.json")
SCHEMA_VERSION = 1
EXIT_CODES = {"ok": 0, "required_failure": 1, "incomplete": 3, "parity_invalid": 4, "cost_unknown": 5}
PLUMBING_NOTE = (
    "Smoke results validate harness plumbing with scripted reference and near-miss solutions. "
    "They are not evidence of agent reliability or model quality."
)


def load() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(result: dict) -> None:
    jsonschema.validate(result, load(), cls=jsonschema.Draft202012Validator)
