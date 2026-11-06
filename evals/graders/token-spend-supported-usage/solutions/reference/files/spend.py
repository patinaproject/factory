"""Price one Claude usage record against a per-million-token rate card."""

from __future__ import annotations

import json
import sys

RATES = {
    "demo-model": {"input": 3.0, "cache_read": 0.3, "cache_write_5m": 3.75, "cache_write_1h": 6.0, "output": 15.0},
}


def usage_tokens(usage: dict) -> dict:
    written = usage.get("cache_creation_input_tokens", 0)
    one_hour = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
    return {
        "input": usage["input_tokens"],
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_write_5m": written - one_hour,
        "cache_write_1h": one_hour,
        "output": usage["output_tokens"],
    }


def cost(usage: dict, rate: dict) -> float:
    tokens = usage_tokens(usage)
    return round(sum(tokens[category] * rate[category] for category in tokens) / 1_000_000, 6)


def main() -> int:
    record = json.load(sys.stdin)
    print(json.dumps({"cost_usd": cost(record["usage"], RATES[record["model"]])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
