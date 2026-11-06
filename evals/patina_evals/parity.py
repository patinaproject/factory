"""Compare the trial adapter's effective configuration with the real worker's.

Every leaf that differs must match an entry in `parity/differences.json`, with
both values. An unlisted difference is incompatible, a dimension the adapter
cannot read is unknown, and either makes the comparison invalid.
"""

from __future__ import annotations

import json
from pathlib import Path

from patina_evals import adapter

DIMENSIONS = (
    "worker_prompt", "project_instructions", "tool_search", "agents_and_hooks", "skill_and_dependency_pins",
    "role_models", "effort", "resume", "environment",
)
DIFFERENCES = Path(__file__).resolve().parents[1] / "parity" / "differences.json"
ABSENT = "<absent>"


def leaves(value, prefix: str) -> dict:
    if isinstance(value, dict) and value:
        out = {}
        for key, child in value.items():
            out.update(leaves(child, f"{prefix}.{key}"))
        return out
    return {prefix: value}


def compare(worker: dict, trial: dict, differences: list[dict]) -> dict:
    listed = {d["path"]: d for d in differences}
    used, rows, problems = set(), [], []
    for name in DIMENSIONS:
        sides = worker.get(name), trial.get(name)
        if sides[0] is None or sides[1] is None:
            problems.append({"dimension": name, "kind": "unknown", "detail": "dimension missing"})
            rows.append({"dimension": name, "status": "unknown", "source": []})
            continue
        unknown = [repr(s["value"]) for s in sides if isinstance(s["value"], adapter.Unknown)]
        if unknown:
            problems.append({"dimension": name, "kind": "unknown", "detail": "; ".join(unknown)})
            rows.append({"dimension": name, "status": "unknown", "source": sides[0]["source"]})
            continue
        a, b = leaves(sides[0]["value"], name), leaves(sides[1]["value"], name)
        status = "equal"
        for path in sorted(set(a) | set(b)):
            left, right = a.get(path, ABSENT), b.get(path, ABSENT)
            if left == right:
                continue
            entry = listed.get(path)
            if entry and entry["worker"] == left and entry["trial"] == right:
                used.add(path)
                status = "intentional_difference" if status == "equal" else status
                continue
            status = "incompatible"
            problems.append({"dimension": name, "kind": "incompatible", "path": path, "worker": left, "trial": right})
        rows.append({"dimension": name, "status": status, "source": sides[0]["source"]})
    for path in sorted(set(listed) - used):
        problems.append({"dimension": path.split(".")[0], "kind": "stale_difference", "path": path})
    return {
        "schema_version": 1, "valid": not problems, "dimensions": rows, "problems": problems,
        "intentional_differences": [listed[p] for p in sorted(used)],
    }


def load_differences(path: Path = DIFFERENCES) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["differences"]


def check(repo: Path = adapter.REPO_ROOT, differences: Path = DIFFERENCES) -> dict:
    return compare(adapter.construction("worker", repo), adapter.construction("trial", repo), load_differences(differences))
