from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Union

from merged_checks import newest_runs, passed

REVIEWS_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviews(last:100){nodes{state submittedAt author{login} commit{oid}}}"
    "reviewRequests(first:100){nodes{requestedReviewer{... on User{login} ... on Bot{login}}}}"
    "reviewThreads(first:100){nodes{isResolved comments(first:1){nodes{author{login}}}}}}}}"
)


@dataclass(frozen=True)
class Handback:
    reason: str


@dataclass(frozen=True)
class Request:
    logins: tuple[str, ...]


@dataclass(frozen=True)
class Nothing:
    pass


Outcome = Union[Handback, Request, Nothing]


def latest_reviews(reviews: list) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    submitted = (r for r in reviews if r["state"] != "PENDING" and r.get("author"))
    for review in sorted(submitted, key=lambda r: r["submittedAt"]):
        latest[review["author"]["login"]] = review
    return latest


def failed(check: dict) -> bool:
    if passed(check):
        return False
    return check.get("state") not in ("PENDING", "EXPECTED") and check.get("status", "COMPLETED") == "COMPLETED"


def decide(pull: dict, reviews: list, requested: list[str], thread_authors: list[str], required_contexts: list[str]) -> Outcome:
    requesters = {login: r for login, r in latest_reviews(reviews).items() if r["state"] == "CHANGES_REQUESTED"}
    if not requesters:
        return Nothing()
    head = pull["headRefOid"]
    current = [login for login, r in requesters.items() if (r.get("commit") or {}).get("oid") == head]
    if current:
        return Handback(f"{', '.join(current)} requested changes on the current head")
    runs = newest_runs(pull.get("statusCheckRollup") or [])
    failing = [name for name in required_contexts if any(failed(run) for run in runs.get(name, []))]
    if failing:
        return Handback(f"required checks failed: {', '.join(failing)}")
    if any(not runs.get(name) or not all(passed(run) for run in runs[name]) for name in required_contexts):
        return Nothing()
    waiting = [login for login in requesters if login not in requested]
    open_threads = [login for login in waiting if login in thread_authors]
    if open_threads:
        return Handback(f"unresolved review threads from {', '.join(open_threads)}")
    return Request(tuple(waiting)) if waiting else Nothing()


def required_contexts(run: Callable[..., str], gh: str, repo: str, base: str) -> list[str]:
    rules = json.loads(run(gh, "api", f"repos/{repo}/rules/branches/{urllib.parse.quote(base, safe='')}?per_page=100"))
    if not isinstance(rules, list):
        raise ValueError(f"the branch rules of {repo}:{base} are not a list")
    return [
        check["context"]
        for rule in rules
        if rule.get("type") == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    ]


def read_reviews(run: Callable[..., str], gh: str, repo: str, number: int) -> tuple[list, list[str], list[str]]:
    owner, name = repo.split("/", 1)
    out = run(gh, "api", "graphql", "-f", f"query={REVIEWS_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}")
    try:
        data = json.loads(out)["data"]["repository"]["pullRequest"]
        reviews = data["reviews"]["nodes"]
        requested = [
            node["requestedReviewer"]["login"]
            for node in data["reviewRequests"]["nodes"]
            if (node.get("requestedReviewer") or {}).get("login")
        ]
        authors = [
            comment["author"]["login"]
            for thread in data["reviewThreads"]["nodes"]
            if not thread["isResolved"]
            for comment in thread["comments"]["nodes"][:1]
            if comment.get("author")
        ]
    except (KeyError, TypeError) as failure:
        raise ValueError(f"the reviews of {repo}#{number} could not be read: {failure!r}") from failure
    return reviews, requested, authors
