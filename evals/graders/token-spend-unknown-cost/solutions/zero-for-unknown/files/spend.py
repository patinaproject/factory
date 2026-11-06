"""Price one Claude usage record against a per-million-token rate card."""

from __future__ import annotations

import json
import sys

RATES = {
    "demo-model": {"input": 3.0, "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6.0, "output": 15.0},
}


def usage_tokens(usage: dict) -> dict:
    written = usage.get("cache_creation_input_tokens") or 0
    one_hour = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    return {
        "input": usage["input_tokens"],
        "cache_read": usage.get("cache_read_input_tokens") or 0,
        "cache_write_5m": written - one_hour,
        "cache_write_1h": one_hour,
        "output": usage["output_tokens"],
    }


def unknown(flag: str) -> dict:
    return {"cost_usd": 0.0, "complete": False, "flags": [flag]}


def report(record: dict) -> dict:
    usage = record.get("usage")
    if not isinstance(usage, dict) or usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return unknown("missing_usage")
    rate = RATES.get(record.get("model"))
    if rate is None:
        return unknown(f"unpriced_model:{record.get('model')}")
    tokens = usage_tokens(usage)
    return {"cost_usd": round(sum(tokens[c] * rate[c] for c in tokens) / 1_000_000, 6), "complete": True, "flags": []}


def main() -> int:
    print(json.dumps(report(json.load(sys.stdin))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
