"""Decide which branch a card works on, and whether the factory may publish to it.

One rule, two callers. Triage runs this file to pick a card's `branch` and
`takeover_pr`. claude-session imports it to check that pick on every run.

A branch is the factory's own when GitHub's repository activity records the App
as the actor of the branch's newest `branch_creation` entry. Commit authors and
pull request authors play no part, because a person can write either.

As a command, it prints one JSON line and exits 0 with `{"branch", "takeover_pr"}`,
exits 2 with `{"blocked": reason}`, or exits 1 with `{"error": reason}`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from enum import Enum
from typing import Any, Callable, Mapping, NamedTuple
from urllib.parse import quote

TAKEOVER_SUFFIX = "-takeover"
PULL_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)$")
SERVER_ERROR = re.compile(r"\bHTTP 5[0-9][0-9]\b")
WRITE_OPTIONS = ("-X", "--method", "-f", "--raw-field", "-F", "--field", "--input")
RETRY_DELAYS = (1, 2)


class Owner(Enum):
    FACTORY = "factory"
    PERSON = "person"


class Source(Enum):
    FRESH = "fresh"
    REMOTE_BRANCH = "remote_branch"
    TAKEOVER_PR = "takeover_pr"
    REFUSE = "refuse"


class Pull(NamedTuple):
    url: str
    state: str
    head: str
    cross_repository: bool
    updated_at: str


class CardBranch(NamedTuple):
    branch: str
    takeover_pr: str | None


class Blocked(NamedTuple):
    reason: str


class GhFailed(Exception):
    pass


class BranchNotFound(GhFailed):
    pass


class GhServerError(GhFailed):
    """GitHub kept answering with a 5xx, so the caller can wait and try again."""


def is_app(login: str, app: str) -> bool:
    """True for the App's two forms GitHub shows: the `x[bot]` login and the `app/x` pull request author.

    `app` is the configured `[bot]` login. The bare name `x` is an ordinary account that a person can register.
    """
    bot = app.lower()
    return login.lower() in (bot, "app/" + bot.removesuffix("[bot]"))


# Keyed on (takeover pull request given, owner of origin/<branch> or None when it is missing).
ADOPTION = {
    (False, None): Source.FRESH,
    (False, Owner.FACTORY): Source.REMOTE_BRANCH,
    (False, Owner.PERSON): Source.REFUSE,
    (True, None): Source.TAKEOVER_PR,
    (True, Owner.FACTORY): Source.REMOTE_BRANCH,
    (True, Owner.PERSON): Source.REFUSE,
}


def adoption(takeover: bool, owner: Owner | None) -> Source:
    return ADOPTION[(takeover, owner)]


def refusal(branch: str, app: str) -> str:
    said = (
        f"origin/{branch} was not created by {app} according to GitHub's branch_creation record "
        "(another account created it, or GitHub has no record yet), so the factory cannot publish to it."
    )
    if branch.endswith(TAKEOVER_SUFFIX):
        return f"{said} A person must rename or delete it, then the card can run again."
    return f"{said} Triage picks {branch}{TAKEOVER_SUFFIX} for this state; give the card that branch."


def choose(git_branch: str, pulls: list[Pull], owners: Mapping[str, Owner | None]) -> CardBranch | Blocked:
    """Pick the card's branch and takeover pull request.

    `owners` holds the owner of `git_branch`, of `git_branch` plus the takeover suffix, and of the head of
    each open pull request in the repository. A missing entry means the branch does not exist.
    """
    takeover_branch = git_branch + TAKEOVER_SUFFIX
    open_pulls = sorted((pull for pull in pulls if pull.state == "OPEN"), key=lambda pull: pull.updated_at, reverse=True)
    own = [pull for pull in open_pulls if not pull.cross_repository and owners.get(pull.head) is Owner.FACTORY]
    if own:
        return CardBranch(own[0].head, None)
    takeover_pr = next((pull.url for pull in open_pulls), None)

    if owners.get(git_branch) is Owner.FACTORY:
        return CardBranch(git_branch, takeover_pr)
    if owners.get(takeover_branch) is Owner.FACTORY:
        return CardBranch(takeover_branch, takeover_pr)
    if owners.get(git_branch) is None:
        return CardBranch(git_branch, takeover_pr)
    # Product call: with no pull request to take over, work stops here. Skip this branch to work beside the person's branch instead.
    if takeover_pr is None:
        return Blocked(
            f"A person created origin/{git_branch} and no open pull request exists for the issue. "
            f"Open a pull request from {git_branch} so the factory takes it over, or rename or delete the branch."
        )
    if owners.get(takeover_branch) is None:
        return CardBranch(takeover_branch, takeover_pr)
    return Blocked(
        f"A person created both origin/{git_branch} and origin/{takeover_branch}, and no pull request "
        "from the factory exists. Rename or delete one of them."
    )


def run_gh(*args: str) -> Any:
    """Call `gh api`, retrying a server error only when the call reads, so a write is never sent twice."""
    gh = os.environ.get("GH_BIN", "gh")
    delays = () if any(arg.startswith(WRITE_OPTIONS) for arg in args) else RETRY_DELAYS
    for attempt in range(len(delays) + 1):
        try:
            proc = subprocess.run([gh, "api", *args], capture_output=True, text=True)
        except OSError as failure:
            raise GhFailed(f"{gh} could not run: {failure.strerror or failure}") from failure
        if proc.returncode == 0:
            return json.loads(proc.stdout) if proc.stdout.strip() else None
        detail = proc.stderr.strip().splitlines()[0] if proc.stderr.strip() else "no error output"
        if "Branch not found" in proc.stderr:
            raise BranchNotFound(detail)
        if not SERVER_ERROR.search(proc.stderr):
            raise GhFailed(f"gh api {args[0]} failed: {detail}")
        if attempt == len(delays):
            raise GhServerError(f"gh api {args[0]} failed: {detail}")
        time.sleep(delays[attempt])


def owner(gh: Callable[..., Any], repo: str, branch: str, app: str) -> Owner | None:
    """None when the branch is missing. Otherwise who created it, where an unproven creator counts as a person."""
    ref = quote(branch, safe="/")
    try:
        gh(f"repos/{repo}/branches/{ref}")
    except BranchNotFound:
        return None
    records = gh(f"repos/{repo}/activity?ref=refs/heads/{ref}&activity_type=branch_creation&per_page=1")
    if records and is_app(records[0]["actor"]["login"], app):
        return Owner.FACTORY
    return Owner.PERSON


def read_pulls(gh: Callable[..., Any], repo: str, urls: list[str]) -> list[Pull]:
    pulls = []
    for url in urls:
        match = PULL_URL.match(url)
        if not match or match.group(1).lower() != repo.lower():
            raise GhFailed(f"--pull {url} is not a pull request URL in {repo}")
        data = gh(f"repos/{repo}/pulls/{match.group(2)}")
        head_repo = (data["head"].get("repo") or {}).get("full_name", "")
        state = "OPEN" if data["state"] == "open" else "MERGED" if data.get("merged") else "CLOSED"
        pulls.append(Pull(url, state, data["head"]["ref"], head_repo.lower() != repo.lower(), data["updated_at"]))
    return pulls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", required=True, help="the App's login, such as example-app[bot]")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--git-branch", required=True, help="the issue's gitBranchName")
    parser.add_argument("--pull", action="append", default=[], help="a pull request URL linked to the issue")
    args = parser.parse_args(argv)
    try:
        pulls = read_pulls(run_gh, args.repo, args.pull)
        names = {args.git_branch, args.git_branch + TAKEOVER_SUFFIX}
        names.update(pull.head for pull in pulls if pull.state == "OPEN" and not pull.cross_repository)
        owners = {name: owner(run_gh, args.repo, name, args.app) for name in sorted(names)}
        choice = choose(args.git_branch, pulls, owners)
    except GhFailed as failure:
        print(json.dumps({"error": str(failure)}))
        return 1
    if isinstance(choice, Blocked):
        print(json.dumps({"blocked": choice.reason}))
        return 2
    print(json.dumps({"branch": choice.branch, "takeover_pr": choice.takeover_pr}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
