"""Read a merged pull request from GitHub and sort its required checks into real failures and superseded runs.

`card_transition.py merged` reads the pull request's state, URL, head, base, and
`statusCheckRollup` with `read_pull_request`, and the checks the base branch's
rules require with `required_checks`. `sort_checks` keeps the newest run of each
check, as the pr-ready-gate plugin does. `real` lists each named check whose
newest run did not pass, or that the rollup does not show. `superseded` lists
each named check whose newest run passed.
"""
from __future__ import annotations

import json
import subprocess
from urllib.parse import quote

PASSING_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
UNSTARTED = "0001-01-01T00:00:00Z"


class Failed(Exception):
    pass


def passed(check: dict) -> bool:
    if "state" in check:
        return check["state"] == "SUCCESS"
    return check.get("status") == "COMPLETED" and check.get("conclusion") in PASSING_CONCLUSIONS


def failed(check: dict) -> bool:
    """A finished run that did not pass, as opposed to one still pending."""
    if passed(check):
        return False
    if "state" in check:
        return check["state"] not in ("PENDING", "EXPECTED")
    return check.get("status") == "COMPLETED"


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


PULL_FIELDS = "state,url,headRefOid,baseRefName,statusCheckRollup"


def gh(*args: str) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    except OSError as failure:
        raise Failed(f"gh could not run: {failure.strerror or failure}") from failure
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no error output"
        raise Failed(f"gh {' '.join(args[:2])} failed: {detail}")
    return proc.stdout


def read_pull_request(url: str) -> dict:
    return json.loads(gh("pr", "view", url, "--json", PULL_FIELDS))


def required_checks(repo: str, base: str) -> list[str]:
    """The check names that the rules of the `base` branch require."""
    rules = json.loads(gh("api", f"repos/{repo}/rules/branches/{quote(base, safe='')}?per_page=100"))
    return sorted({
        required["context"]
        for rule in rules
        if rule.get("type") == "required_status_checks"
        for required in (rule.get("parameters") or {}).get("required_status_checks") or []
    })
