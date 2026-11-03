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
- `implementation-reply`: record a person's reply on an implementation card, from standard input and with the same
  one `Linear reply:` comment as `triage-reply`, and release the card when it waits in `scheduled` for a reply. A
  card in any other status is `none`. A card whose newest park reason starts with one of
  `result_line.MACHINE_PARKS` waits for a machine, so it stays parked and the output is `none` with
  `waits_for` set to the park reason. Otherwise the release rule decides. A released card reads its `repo` and
  `branch` from its body, and `gh` is asked whether the branch has a pull request before any further write, so a
  `gh` failure leaves the card parked. Then it gets one `Released from scheduled for a Linear reply.` comment with
  the park reason, unless one written since the park is there, is assigned to `claude-worker` when a person's
  `stop` unassigned it, and is unblocked, sent to review, and reopened when the branch has a pull request.
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

The worktree and server-error release rules: a card whose newest park reason starts with
`result_line.WORKTREE_HELD_PARK` is released by `sweep_worktree` once a non-blocking `flock` on its worktree's
`claude-session.lock` succeeds. One that starts with `result_line.SERVER_ERROR_PARK` is released by
`sweep_server_error` 300 seconds after its newest `scheduled` event. Each card is judged alone, so a card that
is not ready never holds back another. A released card's agent session gets one `thought` naming the cleared
cause, and a `Resumed:` card comment records it; a post that fails is recorded in that comment, and the release stands.

The brief release rule: a card whose newest park reason starts with `result_line.NEEDS_BRIEF_PARK` waits for its
issue's `<ID> Agent brief` Document. `sweep_brief` reads the issue's labels and Documents for each such card still
assigned to `claude-worker` that no `stopped` comment follows. It releases the card once the brief exists and the
issue carries `ready-for-agent`, and also once the issue no longer carries that label, so the card's next run
refuses and a person is told. Otherwise, unless a `Queued for triage as <task id>` comment follows the park, it
creates the issue's triage card as "Start triage" step 4 does, archiving a `done` one first, comments that line
on the parked card, and posts one `thought` on the card's agent session. A wait that already queued its triage
card posts nothing more, even once that card ends without a brief. A failed Linear read leaves the card parked
and is reported.

The issue release rule: a card whose newest park reason starts with `result_line.ISSUE_PARK` and a GitHub issue
URL waits for that issue, which a worker filed or found for a flaky required check. `sweep_issue` handles each such
card still assigned to `claude-worker` that no `stopped` comment follows. It releases the card once the issue is
closed or not found, or once a `Linear reply:` comment is newer than the park, after commenting the reason on the
card for the resumed run to read. Otherwise, on its first pass, it comments `result_line.ISSUE_NOTED` and the URL on
the card and posts one `thought` naming the issue on the card's agent session. It reads the issue with `gh` and the
worker's `GH_CONFIG_DIR`, once per URL, only while such a card waits and only with a fresh gh login. A `gh` error,
timeout, or `OSError` leaves the card parked and is reported. `replied` never releases it.

- `unblock --repo <r> --branch <b> (--reason <text> | --comment <text>)`: unblock a card. When the branch has a pull
  request, Hermes holds the `ready` card for 24 hours after a comment that names it, and only the
  review reopen lifts the hold, so the card is unblocked, sent to review, and reopened.
- `reopen (--reason <text> | --comment <text>)`: hand a `review` card back to its worker. First `stale_failure.clear_after_review`
  clears the card's `last_failure_error` when its newest ended run is `review_requested` or `completed`, because
  Hermes holds a `ready` card as `blocker_auth` for an old quota or auth error that the reopen leaves in place.
  `--reason` makes Hermes write a `CHANGES REQUESTED: <text>` comment, which the worker reads as a request for
  changes. `--comment` posts the text as one plain card comment, then reopens with no reason, for a reply that
  requests no change.
- `stop`: block a card whatever it is doing. Hermes blocks only a `ready` or `running` card, so a
  `review` card is reopened first. A `scheduled` card stays `scheduled`, is unassigned, and gets a `stopped`
  comment, which has no reply prefix, so the next reply releases it like any wait.
- `merged --pr <url>`: finish a card whose pull request merged, whatever its check runs say. GitHub's
  state is read, not the webhook's: a pull request GitHub does not report `MERGED` changes nothing.
  Then `completion_contract.release` sets the card's contract to `local-only`, which Hermes' acceptance
  skips, unless the contract names another pull request. A card whose required checks did not all
  pass on the merged head gets one comment that names them. A running card is left to its worker,
  whose own completion Hermes then accepts, and any other card is completed by the steps its status
  needs. A card that is already `done` or `archived` gets no write and no comment. When a step after
  the write fails, the next run, or `finish-merged`, finds `local-only`, skips the write, and
  completes the card. The card is never assigned once `specify` has succeeded, because an assignment
  lifts the `active_pr` hold and a worker would start on a merged pull request.

Every command prints one JSON line. It exits 0 with `{"action", "post", ...}`, or `park-waiting`'s line, or 1
with `{"error": reason}`. The actions:

| action | meaning | extra keys |
| --- | --- | --- |
| `none` | the card's status called for no change | `status` |
| `resumed` | the card left `triage` with its own title and body | `assigned` |
| `unblocked` | the card is ready for its worker again | `pull_request` |
| `reopened` | the `review` card went back to its worker | |
| `stopped` | the card is blocked, or was already, or waits unassigned in `scheduled` | `status` |
| `completed` | the merged pull request's card is `done` | |
| `worker_completes` | the running card's contract is `local-only`, and its worker completes it | |
| `not_merged` | GitHub does not report the pull request `MERGED`, so nothing changed | `state` |
| `other_pull` | the card's contract names another pull request, so nothing changed | `contract` |

`post` names the Linear agent activity the caller still posts: `response` after `completed`, and
null otherwise. A card comment names a pull request as `<owner>/<name>#<number>`, never by URL,
because Hermes treats a card whose comments hold a pull request URL as already having one.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import re
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

import completion_contract
import factory_settings
import factory_source
import gh_login
import install_drift
import kanban_board
import merged_checks
import reply_inbox
import stale_failure
from archive_card import Failed, archive, kanban_command, last_line, run, run_kanban

WORKER = "claude-worker"
TRIAGE_BOARD = "triage"
STOPPED = "stopped"
WORKTREE_LOCK = "claude-session.lock"
SERVER_ERROR_WAIT_SECONDS = 300
# A card comment that starts with this records that a sweep resumed the card and told its agent session.
RESUMED_NOTED = "Resumed:"
RELEASED = "Released from scheduled for a Linear reply."
READY_LABEL = "ready-for-agent"
# The comment that records the triage card a brief wait queued, so a later sweep queues no second one.
TRIAGE_QUEUED = "Queued for triage as"
# Covers Hermes' _RESPAWN_GUARD_PR_URL_RE, which holds a `ready` card for a day after a comment that matches it.
PULL_URL = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)(?:[/#?][^\s).,]*)?", re.IGNORECASE)
# Starts the one comment that names the required checks a merged head did not pass.
CHECKS_NOTE = "Merged past required checks:"


def pull_ref(text: str) -> str:
    return PULL_URL.sub(lambda match: f"{match.group(1)}#{match.group(2)}", text)


def result(action: str, post: str | None = None, **extra) -> dict:
    return {"action": action, "post": post, **extra}


class SpecifyFailed(Failed):
    pass


class Card:
    """One card on one board, with the `hermes kanban` calls triage makes on it."""

    def __init__(self, board: str | None, task_id: str):
        self.board = board
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


def write_reply(card: Card, show: dict, text: str) -> None:
    body = f"{reply_inbox.REPLY_PREFIX} {pull_ref(text.strip())}"
    replies = [comment["body"] for comment in show["comments"] if reply_inbox.is_reply(comment["body"])]
    if not replies or replies[-1].strip() != body:
        card.call("comment", body)
        show["comments"].append({"author": None, "body": body, "created_at": int(time.time())})


def triage_reply(card: Card, text: str) -> dict:
    if not text.strip():
        raise Failed("triage-reply needs the reply's text on standard input")
    show = card.show()
    write_reply(card, show, text)
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


def newest_park(show: dict) -> tuple[str, int]:
    """The newest park's reason without its `comments_seen` marker, and when it was written."""
    parks = [event for event in show["events"] if event["kind"] == "scheduled"]
    park = parks[-1] if parks else {"payload": None, "created_at": 0}
    reason, _ = plugin_module("blocker-order", "result_line").read_comments_seen((park["payload"] or {}).get("reason"))
    return reason.strip(), park["created_at"]


def implementation_reply(card: Card, text: str) -> dict:
    if not text.strip():
        raise Failed("implementation-reply needs the reply's text on standard input")
    show = card.show()
    write_reply(card, show, text)
    status = show["task"]["status"]
    if status != "scheduled":
        return result("none", status=status)
    result_line = plugin_module("blocker-order", "result_line")
    reason, parked_at = newest_park(show)
    if parked_for(show, *getattr(result_line, "MACHINE_PARKS", (result_line.GH_LOGIN_PARK,))):
        return result("none", waits_for=reason)
    if not replied(show):
        return result("none", status=status)
    repo, branch = (card_field(show["task"]["body"], name) for name in ("repo", "branch"))
    pull_request = has_pull_request(repo, branch)
    if not any(comment["created_at"] >= parked_at and comment["body"].startswith(RELEASED)
               for comment in show["comments"]):
        card.comment(f"{RELEASED} The card waited for: {reason}")
    if show["task"]["assignee"] is None:
        card.call("assign", WORKER)
    card.call("unblock")
    if pull_request:
        card.call("request-review")
        card.call("reopen-review")
    return result("unblocked", pull_request=pull_request)


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


def worktree_free(card: Card) -> bool:
    """Whether no claude-session holds the card's worktree lock: a non-blocking `flock` succeeds and is dropped
    at once. The lock is where `claude-session` puts it, in the worktree's git dir. A worktree or lock that is
    gone is free, because the resumed run says what is wrong with it."""
    workspace = card.show()["task"].get("workspace_path")
    if not workspace or not os.path.isdir(workspace):
        return True
    proc = subprocess.run(["git", "-C", workspace, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True)
    if proc.returncode != 0:
        return True
    try:
        fd = os.open(Path(proc.stdout.strip()) / WORKTREE_LOCK, os.O_RDONLY)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def server_wait_over(show: dict) -> bool:
    """Five minutes since the newest park. Hermes stamps whole seconds."""
    park = [event for event in show["events"] if event["kind"] == "scheduled"][-1]
    return time.time() - park["created_at"] >= SERVER_ERROR_WAIT_SECONDS


def linear_poster() -> Callable[[str, str], None]:
    """A function that puts a thought on an agent session. The Linear client is made on the first post."""
    made = []

    def post(session_id: str, text: str) -> None:
        module = plugin_module("linear-agent-session", "linear")
        try:
            if not made:
                made.append(module.client_from_env())
            made[0].create_activity(session_id, module.activity_content("thought", text))
        except (*module.CLIENT_ERRORS, OSError) as error:
            raise Failed(f"posting the note on Linear failed: {error}") from None

    return post


def sweep_cause(prefix: str | None, over: Callable[[Card, dict], bool], cleared: str) -> dict:
    """Release each card parked with `prefix` whose own wait `over` says is over, on every board. A card that is
    not ready is skipped and the next is asked. A released card's agent session gets one thought that `cleared`
    names. A failed post never undoes the release: it is a card comment instead."""
    released, failed = [], {}
    if prefix is None:
        return {"released": released, "failed": failed}
    post = linear_poster()
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
            card = Card(slug, task_id)
            try:
                if not over(card, show) or not release_parked(card, slug):
                    continue
                released.append(task_id)
                note_resumed(card, cleared, post)
            except Failed as failure:
                failed[task_id] = str(failure)
    return {"released": released, "failed": failed}


def note_resumed(card: Card, cleared: str, post: Callable[[str, str], None]) -> None:
    """After a release, one thought on the card's agent session, and a card comment that records it."""
    note = f"{RESUMED_NOTED} {cleared}."
    try:
        session_id = card_field(card.show()["task"]["body"], "linear_agent_session_id")
        post(session_id, f"{cleared[0].upper()}{cleared[1:]}, so this card runs again.")
    except Failed as failure:
        note = f"{RESUMED_NOTED} {cleared}, but the note on the agent session was not posted: {failure}"
    try:
        card.comment(note)
    except Failed:
        pass


def sweep_worktree() -> dict:
    """Release every card parked for its worktree once no claude-session holds the card's lock. `dispatch-triage`
    runs it each minute."""
    prefix = getattr(plugin_module("blocker-order", "result_line"), "WORKTREE_HELD_PARK", None)
    return sweep_cause(prefix, lambda card, show: worktree_free(card), "the worktree is free")


def sweep_server_error() -> dict:
    """Release every card parked after a server error once five minutes have passed since its park. The resumed
    run is the check that the service answers. `dispatch-triage` runs it each minute."""
    prefix = getattr(plugin_module("blocker-order", "result_line"), "SERVER_ERROR_PARK", None)
    return sweep_cause(prefix, lambda card, show: server_wait_over(show), "the server-error wait is over")


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
    """Create the issue's triage card the way "Start triage" step 4 does, archiving a `done` one first, because
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
    note = f"No {identifier} Agent brief yet, so triage runs first. {TRIAGE_QUEUED} {triage_id}."
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


ISSUE_READ_SECONDS = 30
# What a closed issue's `stateReason` says about the wait; a reason GitHub adds later reads as plain "closed".
CLOSED_AS = {"COMPLETED": "completed", "NOT_PLANNED": "not planned"}


def read_issue(url: str) -> str | None:
    """Why the issue releases a card waiting for it, or None while it is open."""
    env = {**os.environ, "GH_CONFIG_DIR": str(Path(os.environ["HERMES_HOME"]) / "gh")}
    try:
        proc = subprocess.run(["gh", "issue", "view", url, "--json", "state,stateReason"],
                              capture_output=True, text=True, env=env, timeout=ISSUE_READ_SECONDS)
    except OSError as failure:
        raise Failed(f"gh could not run: {failure.strerror or failure}") from failure
    except subprocess.TimeoutExpired:
        raise Failed(f"gh issue view {url} timed out after {ISSUE_READ_SECONDS} seconds") from None
    if proc.returncode != 0:
        if "Could not resolve to" in proc.stderr or "HTTP 404" in proc.stderr:
            return "the issue was not found"
        raise Failed(f"gh issue view {url} failed: {last_line(proc.stderr)}")
    try:
        issue = json.loads(proc.stdout)
        state, why = issue["state"], issue.get("stateReason")
    except (ValueError, KeyError, TypeError):
        raise Failed(f"gh issue view {url} printed no issue state") from None
    if state != "CLOSED":
        return None
    return f"the issue closed as {CLOSED_AS[why]}" if why in CLOSED_AS else "the issue closed"


def wait_for_issue(card: Card, board: str, show: dict, url: str, closed: Callable[[str], str | None],
                   post: Callable[[str, str], None]) -> str:
    """The issue release rule for one parked card: `released` or `waiting`. `closed` gives the reason the issue
    releases the card, and `post` puts a thought on the card's agent session."""
    task = card.show()["task"]
    if task["assignee"] != WORKER:
        return "waiting"
    park = [event for event in show["events"] if event["kind"] == "scheduled"][-1]
    after = [comment["body"] for comment in show["comments"] if comment["created_at"] >= park["created_at"]]
    why = "a person replied" if any(reply_inbox.is_reply(body) for body in after) else closed(url)
    noted = f"{plugin_module('blocker-order', 'result_line').ISSUE_NOTED} {url}"
    if why is not None:
        # The note starts with the marker, so a run released on the first pass still sees it parked here once.
        note = f"{noted}, and {why}."
        if note not in after:
            card.comment(note)
        return "released" if release_parked(card, board) else "waiting"
    if not any(body.startswith(noted) for body in after):
        card.comment(f"{noted}. This card continues once the issue closes or a person replies.")
        post(card_field(task["body"], "linear_agent_session_id"),
             f"A required check keeps failing, so this card waits for {url} and continues when it closes.")
    return "waiting"


def sweep_issue() -> dict:
    """Apply the issue release rule to every card parked for a GitHub issue, on every board. `dispatch-triage`
    runs it each minute. GitHub is read only while such a card waits, once per issue, and only with a fresh gh
    login; Linear only for a card's first pass."""
    prefix = getattr(plugin_module("blocker-order", "result_line"), "ISSUE_PARK", None)
    released, failed = [], {}
    if prefix is None or not gh_login.is_fresh(Path(os.environ["HERMES_HOME"]) / "gh", time.time()):
        return {"released": released, "failed": failed}
    reads: dict[str, str | Failed | None] = {}
    post = linear_poster()

    def closed(url: str) -> str | None:
        if url not in reads:
            try:
                reads[url] = read_issue(url)
            except Failed as failure:
                reads[url] = failure
        if isinstance(reads[url], Failed):
            raise reads[url]
        return reads[url]

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
            reason = [event for event in show["events"] if event["kind"] == "scheduled"][-1]["payload"]["reason"]
            try:
                action = wait_for_issue(Card(slug, task_id), slug, show, reason[len(prefix):].strip(), closed, post)
            except Failed as failure:
                failed[task_id] = str(failure)
                continue
            if action == "released":
                released.append(task_id)
    return {"released": released, "failed": failed}


PULL_READ_SECONDS = 30
PULL_STATES = {"MERGED": "it merged", "CLOSED": "it closed without merging", "OPEN": None}


def read_pull_state(pull) -> str | None:
    """Why the pull request releases a card waiting for it, or None while it is open. Every `gh` error is a
    `Failed`, including `Could not resolve to`, which GitHub also returns for a repository the App cannot see."""
    env = {**os.environ, "GH_CONFIG_DIR": str(Path(os.environ["HERMES_HOME"]) / "gh")}
    command = ["gh", "pr", "view", str(pull.number), "--repo", pull.repo, "--json", "state"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=PULL_READ_SECONDS)
    except OSError as failure:
        raise Failed(f"gh could not run: {failure.strerror or failure}") from failure
    except subprocess.TimeoutExpired:
        raise Failed(f"gh pr view {pull} timed out after {PULL_READ_SECONDS} seconds") from None
    if proc.returncode != 0:
        raise Failed(f"gh pr view {pull} failed: {last_line(proc.stderr)}")
    try:
        state = json.loads(proc.stdout)["state"]
        return PULL_STATES[state]
    except (ValueError, KeyError, TypeError):
        raise Failed(f"gh pr view {pull} printed no pull request state") from None


def wait_for_pull_request(card: Card, board: str, show: dict, pull, closed: Callable[[object], str | None],
                          post: Callable[[str, str], None]) -> str:
    """The pull request release rule for one parked card: `released` or `waiting`. `closed` gives the reason the
    pull request releases the card, and `post` puts a thought on the card's agent session. Every text it writes
    names the pull request as `owner/name#number`, because Hermes holds a `ready` card for a day after a card
    comment that holds a pull request URL."""
    task = card.show()["task"]
    if task["assignee"] != WORKER:
        return "waiting"
    park = [event for event in show["events"] if event["kind"] == "scheduled"][-1]
    after = [comment["body"] for comment in show["comments"] if comment["created_at"] >= park["created_at"]]
    why = "a person replied" if any(reply_inbox.is_reply(body) for body in after) else closed(pull)
    noted = f"{plugin_module('blocker-order', 'result_line').PULL_REQUEST_NOTED} {pull}"
    if why is not None:
        note = f"{noted}, and {why}."
        if note not in after:
            card.comment(note)
        return "released" if release_parked(card, board) else "waiting"
    if not any(body.startswith((f"{noted}.", f"{noted},")) for body in after):
        card.comment(f"{noted}. This card continues once the pull request merges or closes, or a person replies.")
        post(card_field(task["body"], "linear_agent_session_id"),
             f"This card waits for {pull} to merge and continues when it merges or closes.")
    return "waiting"


def sweep_pull_request() -> dict:
    """Apply the pull request release rule to every card parked for a GitHub pull request, on every board.
    `dispatch-triage` runs it each minute. GitHub is read only while such a card waits, once per pull request,
    and only with a fresh gh login; Linear only for a card's first pass."""
    prefix = getattr(plugin_module("blocker-order", "result_line"), "PULL_REQUEST_PARK", None)
    released, failed = [], {}
    if prefix is None or not gh_login.is_fresh(Path(os.environ["HERMES_HOME"]) / "gh", time.time()):
        return {"released": released, "failed": failed}
    reads: dict = {}
    post = linear_poster()

    def closed(pull) -> str | None:
        if pull not in reads:
            try:
                reads[pull] = read_pull_state(pull)
            except Failed as failure:
                reads[pull] = failure
        if isinstance(reads[pull], Failed):
            raise reads[pull]
        return reads[pull]

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
            reason = [event for event in show["events"] if event["kind"] == "scheduled"][-1]["payload"]["reason"]
            pull = plugin_module("blocker-order", "result_line").park_pull_request(reason)
            if pull is None:
                failed[task_id] = f"the park reason names no pull request: {reason}"
                continue
            try:
                action = wait_for_pull_request(Card(slug, task_id), slug, show, pull, closed, post)
            except Failed as failure:
                failed[task_id] = str(failure)
                continue
            if action == "released":
                released.append(task_id)
    return {"released": released, "failed": failed}


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


def reopen_review(card: Card, reason: str | None, comment: str | None) -> None:
    """`--reason` is Hermes' `CHANGES REQUESTED` comment. `--comment` is posted plain first, so a worker that
    starts as soon as the card is `ready` already reads it, then the card reopens with no reason."""
    if reason is not None:
        card.call("reopen-review", "--reason", reason)
    else:
        card.call("comment", comment)
        card.call("reopen-review")


def unblock(card: Card, repo: str, branch: str, reason: str | None, comment: str | None) -> dict:
    pull_request = has_pull_request(repo, branch)
    card.call("unblock")
    if pull_request:
        card.call("request-review")
        reopen_review(card, reason, comment)
    return result("unblocked", pull_request=pull_request)


def reopen(card: Card, reason: str | None, comment: str | None) -> dict:
    stale_failure.clear_after_review(board_database(card.board), card.id)
    reopen_review(card, reason, comment)
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


class Merge(NamedTuple):
    url: str
    ref: str


def read_pull_request(url: str) -> dict:
    try:
        return merged_checks.read_pull_request(url)
    except (merged_checks.Failed, ValueError):
        pass
    try:
        return merged_checks.read_pull_request(url)
    except (merged_checks.Failed, ValueError) as failure:
        raise Failed(f"pull request {url} could not be read: {failure}") from failure


def note_checks(card: Card, pull: dict, merge: Merge) -> None:
    """Comment once which required checks of the merged head failed or have no run."""
    marker = f"{CHECKS_NOTE} {merge.ref}"
    if any(comment["body"].startswith(marker) for comment in card.show()["comments"]):
        return
    repo = PULL_URL.fullmatch(merge.url).group(1)
    try:
        names = merged_checks.required_checks(repo, pull["baseRefName"])
    except (merged_checks.Failed, ValueError) as failure:
        card.comment(f"{marker}: its required checks could not be read: {failure}")
        return
    real = merged_checks.sort_checks(pull["statusCheckRollup"] or [], names)["real"]
    if real:
        card.comment(f"{marker}: {', '.join(real)} failed or had no run on the merged head.")


def complete(card: Card, merge: Merge) -> dict:
    metadata = json.dumps({"published_pr": merge.url})
    card.call("complete", "--summary", f"Pull request {merge.ref} merged.", "--metadata", metadata)
    return result("completed", "response")


def promote_then_complete(card: Card, merge: Merge) -> dict:
    card.call("promote", "pull", "request", "merged")
    return complete(card, merge)


def unpark_then_complete(card: Card, merge: Merge) -> dict:
    leave_triage(card)
    if card.status() == "todo":
        return promote_then_complete(card, merge)
    return complete(card, merge)


def unschedule_then_complete(card: Card, merge: Merge) -> dict:
    # Unassigned, the `ready` card that `unblock` leaves cannot be claimed before `complete`.
    card.call("assign", "none")
    card.call("unblock")
    return complete(card, merge)


MERGED_BY_STATUS: dict[str, Callable[[Card, Merge], dict]] = {
    "review": complete,
    "blocked": complete,
    "ready": complete,
    "todo": promote_then_complete,
    "triage": unpark_then_complete,
    "scheduled": unschedule_then_complete,
}
# The worker of a running card completes it, and with `local-only` written Hermes accepts that completion.
WORKER_COMPLETES = "running"


def merged(card: Card, url: str) -> dict:
    if not PULL_URL.fullmatch(url):
        raise Failed(f"{url} is not a GitHub pull request URL")
    status = card.status()
    if status not in MERGED_BY_STATUS and status != WORKER_COMPLETES:
        return result("none", status=status)
    pull = read_pull_request(url)
    if pull["state"] != "MERGED":
        return result("not_merged", state=pull["state"])
    merge = Merge(pull["url"], pull_ref(pull["url"]))
    write, contract = completion_contract.release(board_database(card.board), card.id, merge.url)
    if write is completion_contract.Write.OTHER:
        return result("other_pull", contract=pull_ref(contract or ""))
    status = card.status()
    if write is completion_contract.Write.FINISHED or (status not in MERGED_BY_STATUS and status != WORKER_COMPLETES):
        return result("none", status=status)
    note_checks(card, pull, merge)
    if status == WORKER_COMPLETES:
        return result("worker_completes")
    try:
        return MERGED_BY_STATUS[status](card, merge)
    except Failed as failure:
        raise Failed(
            f"Pull request {merge.ref} merged and the card's completion_contract is local-only, but {failure}; "
            "the next finish-merged run completes it"
        ) from failure


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
    command("implementation-reply")
    command("stop")

    def handback(name: str) -> argparse.ArgumentParser:
        sub = command(name)
        why = sub.add_mutually_exclusive_group(required=True)
        why.add_argument("--reason")
        why.add_argument("--comment")
        return sub

    sub = handback("unblock")
    sub.add_argument("--repo", required=True)
    sub.add_argument("--branch", required=True)
    handback("reopen")
    command("merged").add_argument("--pr", required=True, help="the merged pull request's URL")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    card = Card(args.board, args.task_id) if hasattr(args, "task_id") else None
    commands = {
        "park-waiting": lambda: park_waiting(args.board),
        "resume": lambda: resume(card),
        "triage-reply": lambda: triage_reply(card, sys.stdin.read()),
        "implementation-reply": lambda: implementation_reply(card, sys.stdin.read()),
        "unblock": lambda: unblock(card, args.repo, args.branch, args.reason, args.comment),
        "reopen": lambda: reopen(card, args.reason, args.comment),
        "stop": lambda: stop(card),
        "merged": lambda: merged(card, args.pr),
    }
    try:
        output = commands[args.command]()
    except (Failed, completion_contract.Failed, KeyError, ValueError, sqlite3.Error) as failure:
        print(json.dumps({"error": pull_ref(str(failure))}))
        return 1
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
