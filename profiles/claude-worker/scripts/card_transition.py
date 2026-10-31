"""Move a Kanban card through the transitions that triage makes, in the order Hermes needs.

Usage: card_transition.py [--board <board>] <command> [<task id>] [options]

- `resume`: take a card out of `triage`. Hermes' `kanban specify` is the only command that leaves
  `triage`, and it rewrites the card's title and body and makes the card dispatchable. The card is
  unassigned first, because the dispatcher skips a card with no assignee, its kept title and body are
  restored, and only then is it assigned to `claude-worker`. That assignment is the handoff that lifts
  Hermes' hold on a card whose comments name a pull request. `specify` asks the model through the
  proxy, and Hermes strips the proxy token from the triage agent's commands, so `specify` alone gets
  the token, from the gateway's broker, in the variable `claude_session.auth_token_env` names.
- `triage-reply`: record a person's reply on a card on the `triage` board and resume the card. The reply's
  text arrives on standard input, so it is never part of a command line. The card gets one comment that starts
  `Linear reply:` and names each pull request as `<owner>/<name>#<number>`, whatever its status, unless the
  card's newest reply comment holds the same text, so a replayed turn writes it once. Then a `scheduled` card
  is released when the release rule says so, a `blocked` card is unassigned and unblocked, a `triage` card is
  resumed without the assignment, and any other status is `none`: the next run reads the comment.
- `park-waiting`: park every card on the board that waits for input the old way in `scheduled`, posting
  nothing. A card blocked with kind `needs_input` is scheduled, and a card in Hermes' `triage` status with
  that kind leaves it through `specify` first. Run it while dispatch is paused, so no worker claims a card
  between the two steps. It takes no task id and prints `{"parked": [task id, ...]}`.

The release rule: a `scheduled` card is released when it holds a reply comment past the `comments_seen`
count in the reason of its newest `scheduled` event. A park with no readable count is released by a reply
comment written after it. The rule reads a card's comments and events in the shape `hermes kanban show
--json` gives them, so `triage-reply` and `dispatch-triage`'s `sweep` feed it the same way. The release is
`assign none` and then `unblock`: unassigned while it cannot be dispatched, the card lands `ready` and
unassigned, and `dispatch-triage` alone assigns it within `triage.max_running`. A card the other caller
already released is not an error.

The login release rule: a `scheduled` card whose newest park reason starts with `result_line.GH_LOGIN_PARK`
waits for the worker's gh login, not for a reply, so `replied` never releases it. `sweep_login` releases it
once `<HERMES_HOME>/gh/token-expiry.json` has at least the 15 minutes `claude-session` needs, on every board
and for every such card still assigned to `claude-worker`, read from `show` just before the release, that no
`stopped` comment follows. A person's `stop`
unassigns the card and comments `stopped`, and either one keeps the card parked. On `triage` the release is
`assign none` and `unblock`, so `dispatch-triage` assigns it within its cap, and on any other board the card
lands `ready` and still assigned.

The deploy release rule is the same for a card whose newest park reason starts with
`result_line.INSTALL_SKEW_PARK`, which waits for a factory deploy: `sweep_deploy` releases it once the install
matches the commit auto-deploy recorded as deployed, compared the way `claude-session` compares it.

The brief release rule: a card whose newest park reason starts with `result_line.NEEDS_BRIEF_PARK` waits for its
issue's `<ID> Agent brief` Document. `sweep_brief` reads the issue's labels and Documents for each such card still
assigned to `claude-worker` that no `stopped` comment follows. It releases the card once the brief exists and the
issue carries `ready-for-agent`, and also once the issue no longer carries that label, so the card's next run
refuses and a person is told. Otherwise, unless a `Queued for triage as <task id>` comment follows the park, it
creates the issue's triage card as "Triage inbox" step 7 does, archiving a `done` one first, comments that line
on the parked card, and posts one `thought` on the card's agent session. A wait that already queued its triage
card posts nothing more, even once that card ends without a brief. A failed Linear read leaves the card parked
and is reported.

- `unblock --repo <r> --branch <b> --reason <text>`: unblock a card. When the branch has a pull
  request, Hermes holds the `ready` card for 24 hours after a comment that names it, and only the
  review reopen lifts the hold, so the card is unblocked, sent to review, and reopened.
- `reopen --reason <text>`: hand a `review` card back to its worker.
- `stop`: block a card whatever it is doing. Hermes blocks only a `ready` or `running` card, so a
  `review` card is reopened first. A `scheduled` card stays `scheduled`, is unassigned, and gets a `stopped`
  comment, which has no reply prefix, so the next reply releases it like any wait.
- `merged --pr <url> --linear-state <stateType or unreadable>`: finish a card whose pull request
  merged. A refused completion is a refused merge when the newest `pr_acceptance` event is for this
  pull request at its head and names a check that did not pass, or names no required checks. A
  merged pull request never runs its checks again, so the card then waits for a person and is
  archived once the Linear issue is completed. The card is never assigned once `specify` has
  succeeded, because an assignment lifts the `active_pr` hold and a worker would start on a merged
  pull request.

Every command prints one JSON line. It exits 0 with `{"action", "post", ...}`, or `park-waiting`'s line, or 1
with `{"error": reason}`. The actions:

| action | meaning | extra keys |
| --- | --- | --- |
| `none` | the card's status called for no change | `status` |
| `resumed` | the card left `triage` with its own title and body | `assigned` |
| `unblocked` | the card is ready for its worker again | `pull_request` |
| `reopened` | the `review` card went back to its worker | |
| `stopped` | the card is blocked, or was already, or waits unassigned in `scheduled` | `status` |
| `completed` | Hermes accepted the merged pull request and the card is `done` | |
| `commented` | the merge left the card as it was, with `reason` commented on it | `reason` |
| `archived` | a refused merge whose Linear issue is completed | |
| `held` | a refused merge waits for a person: a `ready` card is blocked, a `review` or `blocked` card stays | `reason` |

`post` names the Linear agent activity the caller still posts: `response` after `completed`,
`elicitation` with `reason` as its body after a `held` that Hermes' block hook did not post, and
null otherwise. A card comment names a pull request as `<owner>/<name>#<number>`, never by URL,
because Hermes treats a card whose comments hold a pull request URL as already having one.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Callable, NamedTuple

import factory_settings
import factory_source
import gh_login
import install_drift
import kanban_board
import merged_checks
import reply_inbox
from archive_card import Failed, archive, kanban_command, last_line, run, run_kanban

WORKER = "claude-worker"
TRIAGE_BOARD = "triage"
STOPPED = "stopped"
READY_LABEL = "ready-for-agent"
# The comment that records the triage card a brief wait queued, so a later sweep queues no second one.
TRIAGE_QUEUED = "Queued for triage as"
# Covers Hermes' _RESPAWN_GUARD_PR_URL_RE, which holds a `ready` card for a day after a comment that matches it.
PULL_URL = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)(?:[/#?][^\s).,]*)?", re.IGNORECASE)
NOT_REFUSED = frozenset({"success", "pending"})


def pull_ref(text: str) -> str:
    return PULL_URL.sub(lambda match: f"{match.group(1)}#{match.group(2)}", text)


def result(action: str, post: str | None = None, **extra) -> dict:
    return {"action": action, "post": post, **extra}


class SpecifyFailed(Failed):
    pass


class Card:
    """One card on one board, with the `hermes kanban` calls triage makes on it."""

    def __init__(self, board: str | None, task_id: str):
        self.kanban = kanban_command(board)
        self.id = task_id

    def show(self) -> dict:
        return json.loads(self.call("show", "--json"))

    def status(self) -> str:
        return self.show()["task"]["status"]

    def call(self, *args: str, env: dict | None = None) -> str:
        try:
            return run_kanban(*self.kanban, args[0], self.id, *args[1:], env=env)
        except Failed as failure:
            raise Failed(f"hermes kanban {args[0]} failed: {last_line(failure.stderr)}", failure.stderr) from failure

    def comment(self, text: str) -> str:
        text = pull_ref(text)
        self.call("comment", text)
        return text

    def archive(self) -> None:
        archive(self.kanban, self.id)


def plugin_module(plugin: str, name: str):
    """Module `name` of this home's copy of `plugin`, as `claude-session` imports it."""
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    path = str(Path(home) / "plugins" / plugin)
    if path not in sys.path:
        sys.path.insert(0, path)
    return __import__(name)


def specify_environment() -> dict:
    """This environment with the proxy token, which Hermes strips from the triage agent's commands."""
    broker_client = plugin_module("linear-agent-session", "broker_client")
    try:
        variable = factory_settings.token_variables(factory_settings.read(raw=False))[0]
    except factory_settings.SettingsError as error:
        raise SpecifyFailed(f"specify needs the proxy token: {error}") from None
    if not variable:
        raise SpecifyFailed("specify needs the proxy token, and claude_session.auth_token_env is not set")
    try:
        token = broker_client.request(broker_client.socket_path(os.environ), "proxy_token")["token"]
    except broker_client.BrokerError as error:
        raise SpecifyFailed(f"specify needs the proxy token: {error}") from None
    return {**os.environ, variable: token}


def leave_triage(card: Card) -> None:
    """Run `specify` with the card unassigned and restore its title and body; the card stays unassigned."""
    task = card.show()["task"]
    if task["status"] != "triage":
        raise Failed(f"card {card.id} is {task['status']}, not triage")
    env = specify_environment()
    card.call("assign", "none")
    try:
        card.call("specify", env=env)
    except Failed as failure:
        card.call("assign", WORKER)
        raise SpecifyFailed(f"{failure}; the card stays in triage") from failure
    restore = ("edit", "--title", task["title"], "--body", task["body"])
    try:
        card.call(*restore)
    except Failed:
        try:
            card.call(*restore)
        except Failed as failure:
            raise Failed(f"{failure}; the card is unassigned and holds the title and body specify wrote") from failure


def resume(card: Card, assign: bool = True) -> dict:
    leave_triage(card)
    if assign:
        card.call("assign", WORKER)
    return result("resumed", assigned=assign)


def parked_for(show: dict, *prefixes: str) -> bool:
    """Whether the card's newest park reason starts with one of `prefixes`."""
    parks = [event for event in show["events"] if event["kind"] == "scheduled"]
    reason = (parks[-1]["payload"] or {}).get("reason") if parks else None
    return isinstance(reason, str) and reason.startswith(prefixes)


def replied(show: dict) -> bool:
    """The release rule, for a `scheduled` card as `hermes kanban show --json` gives it."""
    result_line = plugin_module("blocker-order", "result_line")
    # A deploy can install these scripts before the plugin, so the installed result_line may predate MACHINE_PARKS.
    if parked_for(show, *getattr(result_line, "MACHINE_PARKS", (result_line.GH_LOGIN_PARK,))):
        return False
    parks = [event for event in show["events"] if event["kind"] == "scheduled"]
    # Hermes writes a `scheduled` event with every park; without one, any reply counts as after it.
    park = parks[-1] if parks else {"payload": None, "created_at": 0}
    _, seen = result_line.read_comments_seen((park["payload"] or {}).get("reason"))
    replies = [(at, comment) for at, comment in enumerate(show["comments"]) if reply_inbox.is_reply(comment["body"])]
    if seen is not None:
        return any(at >= seen for at, _ in replies)
    # Hermes stamps whole seconds, so a reply in the park's own second counts as after it.
    return any(comment["created_at"] >= park["created_at"] for _, comment in replies)


def release(card: Card) -> None:
    """`assign none`, then `unblock`; each step is a no-op once the other caller released the card."""
    for step in (("assign", "none"), ("unblock",)):
        try:
            card.call(*step)
        except Failed:
            if card.status() == "scheduled":
                raise
            return


def triage_reply(card: Card, text: str) -> dict:
    if not text.strip():
        raise Failed("triage-reply needs the reply's text on standard input")
    show = card.show()
    body = f"{reply_inbox.REPLY_PREFIX} {pull_ref(text.strip())}"
    replies = [comment["body"] for comment in show["comments"] if reply_inbox.is_reply(comment["body"])]
    if not replies or replies[-1].strip() != body:
        card.call("comment", body)
        show["comments"].append({"author": None, "body": body, "created_at": int(time.time())})
    status = show["task"]["status"]
    if status == "scheduled":
        if not replied(show):
            return result("none", status=status)
        release(card)
        return result("unblocked", pull_request=False)
    if status == "blocked":
        card.call("assign", "none")
        card.call("unblock")
        return result("unblocked", pull_request=False)
    if status == "triage":
        return resume(card, assign=False)
    return result("none", status=status)


def boards() -> list[dict]:
    return json.loads(run(*kanban_command(None), "boards", "list", "--json"))


def board_database(board: str | None) -> str:
    slug = board or "default"
    path = next((each["db_path"] for each in boards() if each["slug"] == slug), None)
    if path is None:
        raise Failed(f"Hermes has no board {slug}")
    return path


def sweep(board: str) -> dict:
    """Release every `scheduled` card the release rule says to; `dispatch-triage` runs it each minute."""
    released, failed = [], {}
    for task_id, show in sorted(kanban_board.scheduled(board_database(board)).items()):
        if not replied(show):
            continue
        try:
            release(Card(board, task_id))
        except Failed as failure:
            failed[task_id] = str(failure)
            continue
        released.append(task_id)
    return {"released": released, "failed": failed}


def wait_over(show: dict, prefix: str) -> bool:
    """A card parked with `prefix` that no person stopped after the park."""
    if not parked_for(show, prefix):
        return False
    park = [event for event in show["events"] if event["kind"] == "scheduled"][-1]
    return not any(comment["body"] == STOPPED and comment["created_at"] >= park["created_at"] for comment in show["comments"])


def release_parked(card: Card, board: str) -> bool:
    """False, releasing nothing, when the card is no longer assigned to the worker: a person unassigned it."""
    if card.show()["task"]["assignee"] != WORKER:
        return False
    if board != TRIAGE_BOARD:
        card.call("unblock")
        return True
    card.call("assign", "none")
    try:
        card.call("unblock")
    except Failed:
        if card.status() != "scheduled":
            return True
        # Left unassigned, the card would read as one a person unassigned and never be released.
        card.call("assign", WORKER)
        raise
    return True


def sweep_login() -> dict:
    """Release every card parked for the worker's gh login, on every board, once the login is fresh enough to
    start a run. `dispatch-triage` runs it each minute."""
    if not gh_login.is_fresh(Path(os.environ["HERMES_HOME"]) / "gh", time.time()):
        return {"released": [], "failed": {}}
    return sweep_parked(plugin_module("blocker-order", "result_line").GH_LOGIN_PARK, lambda: True)


def install_matches() -> bool:
    """claude-session's comparison: whether its skew check would no longer park a run. Once auto-deploy records a
    failed or blocked commit no deploy is coming, so a released card's run is blocked and a person is told."""
    home = Path(os.environ["HERMES_HOME"])
    state = install_drift.deploy_state(home)
    if state.get("failed") or state.get("blocked"):
        return True
    try:
        repo = factory_source.checkout(home)
        return not install_drift.skew(home, repo, install_drift.deployed(state))
    except install_drift.MissingCommit:
        return False
    except (OSError, factory_source.SourceError, install_drift.GitError):
        return True


def sweep_deploy() -> dict:
    """Release every card parked for a factory deploy, on every board, once the install matches the deployed
    commit. `dispatch-triage` runs it each minute."""
    prefix = getattr(plugin_module("blocker-order", "result_line"), "INSTALL_SKEW_PARK", None)
    if prefix is None:
        return {"released": [], "failed": {}}
    return sweep_parked(prefix, install_matches)


def sweep_parked(prefix: str, ready: Callable[[], bool]) -> dict:
    """Release each card parked with `prefix` once `ready`, which is asked only when such a card waits."""
    released, failed = [], {}
    answer = None
    for each in boards():
        slug, path = each["slug"], each["db_path"]
        if not os.path.exists(path):
            continue
        try:
            waiting = kanban_board.scheduled(path)
        except sqlite3.Error as failure:
            failed[slug] = f"cannot read the board: {failure}"
            continue
        for task_id, show in sorted(waiting.items()):
            if not wait_over(show, prefix):
                continue
            if answer is None:
                answer = ready()
            if not answer:
                return {"released": released, "failed": failed}
            try:
                if release_parked(Card(slug, task_id), slug):
                    released.append(task_id)
            except Failed as failure:
                failed[task_id] = str(failure)
    return {"released": released, "failed": failed}


def card_field(body: str, name: str) -> str:
    match = re.search(rf"^{name}:[ \t]*(\S+)[ \t]*$", body or "", re.MULTILINE)
    if match is None:
        raise Failed(f"the card's body has no {name} line")
    return match.group(1)


def queued_after_park(show: dict) -> bool:
    """Whether this wait already queued its triage card: a `TRIAGE_QUEUED` comment follows the newest park."""
    park = [event for event in show["events"] if event["kind"] == "scheduled"][-1]
    return any(comment["body"].startswith(TRIAGE_QUEUED) and comment["created_at"] >= park["created_at"]
               for comment in show["comments"])


def triage_flags(full_name: str) -> list[str]:
    """The repository's triage card flags, by the rule `templates/render.py` renders into the gateway's prompt."""
    repositories = factory_settings.read(raw=False).get("repositories") or []
    repo = next((each for each in repositories if each["full_name"].lower() == full_name.lower()), None)
    if repo is None:
        raise Failed(f"repository {full_name} is not configured")
    path = factory_source.checkout(Path(os.environ["HERMES_HOME"])) / "templates" / "render.py"
    spec = importlib.util.spec_from_file_location("factory_render", path)
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    return shlex.split(render.workspace_flag(repo))


def queue_triage(body: str, client) -> str:
    """Create the issue's triage card the way "Triage inbox" step 7 does, archiving a `done` one first, because
    its idempotency key would otherwise return that card. A live one is returned as it is."""
    issue_id = card_field(body, "linear_issue_id")
    triage = kanban_command(TRIAGE_BOARD)
    for each in json.loads(run_kanban(*triage, "list", "--json")):
        if each["status"] == "done" and f"linear_issue_id: {issue_id}" in (each["body"] or "").splitlines():
            archive(triage, each["id"])
    issue = client.get_issue(issue_id)
    repo = card_field(body, "repo")
    fields = (
        "card: triage",
        *(f"{name}: {card_field(body, name)}" for name in (
            "linear_issue_id", "linear_issue_identifier", "linear_issue_url", "linear_agent_session_id",
        )),
        f"repo: {repo}",
    )
    card_body = "```text\n" + "\n".join(fields) + "\n```\n"
    created = run_kanban(
        *triage, "create", issue["cardTitle"], "--priority", str(issue["cardPriority"]), *triage_flags(repo),
        "--idempotency-key", f"linear-triage:{issue_id}", "--body", card_body, "--json",
    )
    return json.loads(created)["id"]


def wait_for_brief(card: Card, board: str, show: dict, client, linear) -> tuple[str, str | None]:
    """The brief release rule for one parked card: `released`, `queued` with the triage card, or `waiting`."""
    task = card.show()["task"]
    if task["assignee"] != WORKER:
        return "waiting", None
    issue_id, identifier = card_field(task["body"], "linear_issue_id"), card_field(task["body"], "linear_issue_identifier")
    try:
        labels = client.issue_labels(issue_id)
        titles = [document["title"] for document in client.issue_documents(issue_id)["documents"]]
    except (*linear.CLIENT_ERRORS, OSError) as error:
        raise Failed(f"reading Linear issue {identifier} failed: {error}") from None
    ready = linear.role_key(READY_LABEL) in map(linear.role_key, labels)
    if not ready or f"{identifier} Agent brief" in titles:
        return ("released" if release_parked(card, board) else "waiting"), None
    if queued_after_park(show):
        return "waiting", None
    try:
        triage_id = queue_triage(task["body"], client)
    except (*linear.CLIENT_ERRORS, OSError) as error:
        raise Failed(f"reading Linear issue {identifier} failed: {error}") from None
    except (factory_settings.SettingsError, factory_source.SourceError, KeyError, ValueError) as error:
        raise Failed(f"queueing a triage card for {identifier} failed: {error}") from None
    card.call("comment", f"{TRIAGE_QUEUED} {triage_id}")
    note = f"No {identifier} Agent brief yet, so triage runs first. Queued for triage as {triage_id}."
    try:
        client.create_activity(card_field(task["body"], "linear_agent_session_id"), linear.activity_content("thought", note))
    except (*linear.CLIENT_ERRORS, OSError) as error:
        raise Failed(f"queued {triage_id}, but posting the note on Linear failed: {error}") from None
    return "queued", triage_id


def sweep_brief() -> dict:
    """Apply the brief release rule to every card parked for its issue's Agent brief, on every board.
    `dispatch-triage` runs it each minute. Linear is read only when such a card waits."""
    prefix = getattr(plugin_module("blocker-order", "result_line"), "NEEDS_BRIEF_PARK", None)
    released, queued, failed = [], {}, {}
    if prefix is None:
        return {"released": released, "queued": queued, "failed": failed}
    linear = client = None
    for each in boards():
        slug, path = each["slug"], each["db_path"]
        if not os.path.exists(path):
            continue
        try:
            waiting = kanban_board.scheduled(path)
        except sqlite3.Error as failure:
            failed[slug] = f"cannot read the board: {failure}"
            continue
        for task_id, show in sorted(waiting.items()):
            if not wait_over(show, prefix):
                continue
            try:
                if client is None:
                    linear = plugin_module("linear-agent-session", "linear")
                    client = linear.client_from_env()
                action, triage_id = wait_for_brief(Card(slug, task_id), slug, show, client, linear)
            except Failed as failure:
                failed[task_id] = str(failure)
                continue
            if action == "released":
                released.append(task_id)
            elif action == "queued":
                queued[task_id] = triage_id
    return {"released": released, "queued": queued, "failed": failed}


def park_waiting(board: str | None) -> dict:
    result_line = plugin_module("blocker-order", "result_line")
    parked = []
    for row in kanban_board.blocked_for_input(board_database(board)):
        card = Card(board, row["id"])
        status = card.status()
        if status not in ("blocked", "triage"):
            continue
        if status == "triage":
            leave_triage(card)
        # `hermes kanban schedule` comments its reason first, so the count excludes that comment.
        seen = len(card.show()["comments"])
        reason = f"{result_line.NEEDS_INPUT} waits for a reply, parked from {status} by park-waiting"
        card.call("schedule", result_line.mark_comments_seen(reason, seen))
        parked.append(row["id"])
    return {"parked": parked}


def has_pull_request(repo: str, branch: str) -> bool:
    try:
        proc = subprocess.run(["gh", "pr", "view", branch, "-R", repo, "--json", "state"], capture_output=True, text=True)
    except OSError as failure:
        raise Failed(f"gh could not run: {failure.strerror or failure}") from failure
    if proc.returncode == 0:
        return True
    if "no pull requests found" in proc.stderr:
        return False
    raise Failed(f"gh pr view failed: {last_line(proc.stderr)}")


def unblock(card: Card, repo: str, branch: str, reason: str) -> dict:
    pull_request = has_pull_request(repo, branch)
    card.call("unblock")
    if pull_request:
        card.call("request-review")
        card.call("reopen-review", "--reason", reason)
    return result("unblocked", pull_request=pull_request)


def reopen(card: Card, reason: str) -> dict:
    card.call("reopen-review", "--reason", reason)
    return result("reopened")


def stop(card: Card) -> dict:
    status = card.status()
    if status == "review":
        card.call("reopen-review", "--reason", STOPPED)
        status = card.status()
    if status == "scheduled":
        card.call("assign", "none")
        card.call("comment", STOPPED)
        return result("stopped", status=status)
    if status in ("ready", "running", "todo"):
        card.call("block", STOPPED)
        status = card.status()
    return result("stopped", status=status)


class Case(Enum):
    """Why Hermes refused a merged pull request: its card comment marker and the reason a person reads."""

    REAL = (
        "Merge refused by Hermes: required checks failed: {names}.",
        "Pull request {ref} merged, but required checks failed: {names}. Is the work done? If it is, mark the "
        "Linear issue Done, and the card is archived.",
    )
    SUPERSEDED = (
        "Merge refused by Hermes: Hermes counted failed runs that newer passing runs replaced: {names}.",
        "Pull request {ref} merged and its required checks passed on their newest runs. Hermes counted failed "
        "runs that newer passing runs replaced: {names}, so it cannot finish the card. Mark the Linear issue "
        "Done, and the card is archived.",
    )
    NO_REQUIRED_CHECKS = (
        "Merge refused by Hermes: the base branch has no required checks.",
        "Pull request {ref} merged, but its base branch has no required checks, so Hermes cannot finish the "
        "card. Is the work done? If it is, mark the Linear issue Done, and the card is archived.",
    )

    def __init__(self, marker: str, reason: str):
        self.marker = marker
        self.reason = reason


class Merge(NamedTuple):
    url: str
    ref: str
    linear_state: str


def read_pull_request(url: str) -> dict:
    try:
        return merged_checks.read_pull_request(url)
    except (merged_checks.Failed, ValueError):
        return merged_checks.read_pull_request(url)


def commented(card: Card, reason: str) -> dict:
    return result("commented", reason=card.comment(reason))


def refusal(card: Card, merge: Merge, error: str) -> dict:
    return commented(card, f"Pull request {merge.ref} merged, but Hermes refused to complete the card: {error}")


def refused_merge(card: Card, merge: Merge, error: str) -> dict:
    try:
        pull = read_pull_request(merge.url)
    except (merged_checks.Failed, ValueError) as failure:
        return commented(card, f"Pull request {merge.ref} merged, but its checks could not be read: {failure}")
    events = [event for event in card.show()["events"] if event["kind"] == "pr_acceptance"]
    receipt = events[-1]["payload"] if events else {}
    if receipt.get("pr_url") != merge.url or receipt.get("head_sha") != pull["headRefOid"]:
        return refusal(card, merge, error)
    checks = receipt.get("checks") or []
    refused = [check["name"] for check in checks if check["classification"] not in NOT_REFUSED]
    if receipt.get("classification") == "missing" and not checks:
        case, names = Case.NO_REQUIRED_CHECKS, []
    elif refused:
        sorted_checks = merged_checks.sort_checks(pull["statusCheckRollup"] or [], refused)
        case = Case.REAL if sorted_checks["real"] else Case.SUPERSEDED
        names = sorted_checks["real"] or sorted_checks["superseded"]
    else:
        return refusal(card, merge, error)
    marker = f"{case.marker.format(names=', '.join(names))} {merge.ref}"
    if merge.linear_state == "completed":
        card.comment(marker)
        card.archive()
        return result("archived")
    if merge.linear_state == "unreadable":
        marker += " The Linear issue could not be read, so the card was not archived."
    card.comment(marker)
    return hold(card, case.reason.format(ref=merge.ref, names=", ".join(names)))


def hold(card: Card, reason: str) -> dict:
    card.call("assign", "none")
    status = card.status()
    if status == "ready":
        try:
            card.call("block", reason, "--kind", "needs_input")
        except Failed:
            return result("held", "elicitation", reason=reason)
        return result("held", reason=reason)
    if status == "blocked":
        # Hermes types a blocked card's block only while it has no kind and refuses otherwise. Either way the
        # card stays blocked and Hermes posts nothing, and `show --json` does not report the kind.
        try:
            card.call("block", reason, "--kind", "needs_input")
        except Failed:
            pass
        return result("held", "elicitation", reason=reason)
    if status == "review":
        return result("held", "elicitation", reason=reason)
    raise Failed(f"card {card.id} is {status} after Hermes refused the merge")


def complete(card: Card, merge: Merge) -> dict:
    metadata = json.dumps({"published_pr": merge.url})
    try:
        card.call("complete", "--summary", f"Pull request {merge.ref} merged.", "--metadata", metadata)
    except Failed as failure:
        return refused_merge(card, merge, str(failure))
    return result("completed", "response")


def promote_then_complete(card: Card, merge: Merge) -> dict:
    try:
        card.call("promote", "pull", "request", "merged")
    except Failed as failure:
        return commented(card, f"Pull request {merge.ref} merged, but Hermes refused to promote the card: {failure}")
    return complete(card, merge)


def unpark_then_complete(card: Card, merge: Merge) -> dict:
    try:
        leave_triage(card)
    except SpecifyFailed as failure:
        return commented(card, f"Pull request {merge.ref} merged, but the card could not leave triage: {failure}")
    if card.status() == "todo":
        return promote_then_complete(card, merge)
    return complete(card, merge)


MERGED_BY_STATUS: dict[str, Callable[[Card, Merge], dict]] = {
    "review": complete,
    "blocked": complete,
    "ready": complete,
    "todo": promote_then_complete,
    "triage": unpark_then_complete,
}


def merged(card: Card, url: str, linear_state: str) -> dict:
    match = PULL_URL.fullmatch(url)
    if not match:
        raise Failed(f"{url} is not a GitHub pull request URL")
    status = card.status()
    if status not in MERGED_BY_STATUS:
        # A running card's worker completes it, and a done or archived card needs nothing.
        return result("none", status=status)
    return MERGED_BY_STATUS[status](card, Merge(url, pull_ref(url), linear_state))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root.add_argument("--board", help="the card's board, when it is not the current one")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("park-waiting")

    def command(name: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.add_argument("task_id")
        return sub

    command("resume")
    command("triage-reply")
    command("stop")
    sub = command("unblock")
    sub.add_argument("--repo", required=True)
    sub.add_argument("--branch", required=True)
    sub.add_argument("--reason", required=True)
    command("reopen").add_argument("--reason", required=True)
    sub = command("merged")
    sub.add_argument("--pr", required=True, help="the merged pull request's URL")
    sub.add_argument("--linear-state", required=True, help="the Linear issue's stateType, or unreadable")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    card = Card(args.board, args.task_id) if hasattr(args, "task_id") else None
    commands = {
        "park-waiting": lambda: park_waiting(args.board),
        "resume": lambda: resume(card),
        "triage-reply": lambda: triage_reply(card, sys.stdin.read()),
        "unblock": lambda: unblock(card, args.repo, args.branch, args.reason),
        "reopen": lambda: reopen(card, args.reason),
        "stop": lambda: stop(card),
        "merged": lambda: merged(card, args.pr, args.linear_state),
    }
    try:
        output = commands[args.command]()
    except (Failed, KeyError, ValueError, sqlite3.Error) as failure:
        print(json.dumps({"error": pull_ref(str(failure))}))
        return 1
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
