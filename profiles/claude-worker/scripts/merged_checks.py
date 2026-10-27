"""Sort the checks Hermes refused a merged pull request over into real failures and superseded runs.

Hermes collects check runs with `filter=latest`, which keeps the latest run of
each check suite, not of each check name. A failed run that a newer run of the
same check replaced in another suite still counts against the pull request.
`card_transition.py merged` reads the pull request's `statusCheckRollup` with
`read_pull_request` and keeps the newest run of each check with `sort_checks`,
as the pr-ready-gate plugin does. `real` lists each named check whose newest run
did not pass, or that the rollup does not show. `superseded` lists each named
check whose newest run passed.
"""
from __future__ import annotations

import json
import subprocess

PASSING_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
UNSTARTED = "0001-01-01T00:00:00Z"


class Failed(Exception):
    pass


def passed(check: dict) -> bool:
    if "state" in check:
        return check["state"] == "SUCCESS"
    return check.get("status") == "COMPLETED" and check.get("conclusion") in PASSING_CONCLUSIONS


def run_order(check: dict) -> tuple[str, bool]:
    started = check.get("startedAt") or UNSTARTED
    return ("~" if started == UNSTARTED else started, not passed(check))


def newest_runs(rollup: list) -> dict[str, list[dict]]:
    """Maps each check name to the newest run of each of its workflows; a tie keeps the run that did not pass."""
    newest: dict[tuple, dict] = {}
    for check in rollup:
        key = (check.get("__typename"), check.get("workflowName"), check.get("name") or check.get("context"))
        if key not in newest or run_order(check) >= run_order(newest[key]):
            newest[key] = check
    by_name: dict[str, list[dict]] = {}
    for (_, _, name), check in newest.items():
        by_name.setdefault(name, []).append(check)
    return by_name


def sort_checks(rollup: list, names: list[str]) -> dict[str, list[str]]:
    by_name = newest_runs(rollup)
    names = list(dict.fromkeys(names))
    real = [name for name in names if not by_name.get(name) or not all(passed(run) for run in by_name[name])]
    return {"real": real, "superseded": [name for name in names if name not in real]}


def read_pull_request(url: str) -> dict:
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", url, "--json", "headRefOid,statusCheckRollup"], capture_output=True, text=True
        )
    except OSError as failure:
        raise Failed(f"gh could not run: {failure.strerror or failure}") from failure
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no error output"
        raise Failed(f"gh pr view failed: {detail}")
    return json.loads(proc.stdout)
