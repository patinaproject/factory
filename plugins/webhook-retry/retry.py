"""Rerun webhook deliveries whose agent turn failed on a usage limit."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_store as store  # noqa: E402

REPLAY_PREAMBLE = """\
This is a replay of a webhook delivery that arrived at {delivered_at} and could not run then because {cause}.

"""
CAUSES = {
    "usage_limit": "the model's usage limit was reached",
    "paused": "Hermes was paused",
    "unadmitted": "it never reached an agent turn, because a restart, drain, or concurrency cap dropped it",
    "missed": "its webhook never reached this gateway, so the retry job found the reply in Linear",
}
# The shape of `triage_intent.render`, which this script cannot import from the plugin.
ERROR_BLOCK = (
    "## Computed triage intent\n\n<triage_intent>\n"
    + json.dumps({"action": "error", "error": "the replay could not compute the triage intent"}, indent=2, sort_keys=True)
    + "\n</triage_intent>\n\n"
)
TRIAGE_INTENT_TIMEOUT_SECONDS = 120
# Above the check's own worst case: three `hermes` reads of up to 60 s each, the live issue read, and a 120 s archive.
TRIAGE_EXIT_TIMEOUT_SECONDS = 360
TEAMMATE_REPLY_TIMEOUT_SECONDS = 120
# The sweep pages Linear and computes one intent, with its Hermes reads, for each prompt it keeps.
MISSED_PROMPTS_TIMEOUT_SECONDS = 600
TEMPLATE_KEY = re.compile(r"\{([a-zA-Z0-9_.]+)\}")
SECRET_PATTERNS = (
    re.compile(r"lin_(?:api|oauth)_\w+"),
    re.compile(r"gh[pousr]_\w+"),
    re.compile(r"github_pat_\w+"),
    re.compile(r"sk-\w+"),
    re.compile(r"Bearer \S+"),
)


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def render_prompt(template: str, payload: dict, event_type: str, route: str) -> str:
    """Hermes' `WebhookAdapter._render_prompt`, which this script cannot import."""
    if not template:
        truncated = json.dumps(payload, indent=2)[:4000]
        return f"Webhook event '{event_type}' on route '{route}':\n\n```json\n{truncated}\n```"

    def resolve(match: re.Match) -> str:
        key = match.group(1)
        if key == "__raw__":
            return json.dumps(payload, indent=2)[:4000]
        if key == "event_type":
            return event_type
        value: Any = payload
        for part in key.split("."):
            if not isinstance(value, dict):
                return f"{{{key}}}"
            value = value.get(part, f"{{{key}}}")
        return json.dumps(value, indent=2)[:2000] if isinstance(value, (dict, list)) else str(value)

    return TEMPLATE_KEY.sub(resolve, template)


def find_hermes() -> str:
    candidates = [os.environ.get("HERMES_BIN"), shutil.which("hermes"), str(Path.home() / ".local" / "bin" / "hermes")]
    for candidate in filter(None, candidates):
        if os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit("retry-failed-webhooks: hermes is not on PATH or in ~/.local/bin; set HERMES_BIN")


def route_setting(hermes: str, home: Path, route: str, key: str) -> Any:
    proc = subprocess.run(
        [hermes, "config", "get", f"platforms.webhook.extra.routes.{route}.{key}", "--json"],
        env={**os.environ, "HERMES_HOME": str(home)}, capture_output=True, text=True,
    )
    try:
        return json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        return None


def route_list(hermes: str, home: Path, route: str, key: str) -> str:
    value = route_setting(hermes, home, route, key)
    return ",".join(value) if isinstance(value, list) else (value or "")


class Run:
    def __init__(self, home: Path, directory: Path) -> None:
        self.home = home
        self.directory = directory

    def say(self, line: str) -> None:
        print(line)
        self.log(line)

    def entries(self) -> dict[str, dict[str, Any]]:
        with store.file_lock(self.directory, "queue.lock"):
            return store.load(self.directory)

    def update(self, key: str, event: str, **fields: Any) -> bool:
        changed = []

        def change(entries: dict[str, dict[str, Any]]) -> bool:
            changed.append(store.apply(entries, key, event, **fields))
            return changed[0]

        store.mutate(self.directory, change)
        return changed[0]

    def mark_reported(self, key: str) -> None:
        def change(entries: dict[str, dict[str, Any]]) -> bool:
            if key not in entries:
                return False
            entries[key]["reported"] = True
            return True

        store.mutate(self.directory, change)

    def prune_skip_markers(self, moment: dt.datetime) -> None:
        for marker in (self.directory / store.SKIPPED_DIRECTORY).glob("*"):
            if moment - dt.datetime.fromtimestamp(marker.stat().st_mtime, dt.timezone.utc) > store.PENDING_AGE:
                marker.unlink()

    def apply_caps(self) -> None:
        moment = store.now()
        self.prune_skip_markers(moment)
        for key, entry in self.entries().items():
            if entry["state"] == "running" and self.update(key, "interrupted", last_error="a retry was interrupted"):
                self.say(f"interrupted {entry['route']} {entry['delivery']}: not retried again, it may have run")
                continue
            age = moment - store.parse_stamp(entry["delivered_at"])
            state = entry["state"]
            marker = store.skip_marker(self.directory, key)
            if state == "pending" and entry.get("admitted") is False and marker.exists() and self.update(key, "skipped"):
                marker.unlink()
                continue
            # An entry recorded before admission was tracked has no `admitted`, and its turn may have run.
            if state == "pending" and entry.get("admitted") is False and age >= store.UNSTARTED_AGE:
                if self.update(key, "unadmitted", reason="unadmitted", next_try_at=entry["delivered_at"]):
                    state = "queued"
            if state == "pending" and age > store.PENDING_AGE:
                self.update(key, "expire")
            elif state == "queued" and (age > store.GIVE_UP_AGE or entry["attempts"] >= store.MAX_ATTEMPTS):
                reason = "older than 48 hours" if age > store.GIVE_UP_AGE else f"{entry['attempts']} attempts"
                if self.update(key, "give_up"):
                    self.say(f"gave up {entry['route']} {entry['delivery']}: {reason}")

    def log(self, line: str) -> None:
        with open(self.directory / "retry.log", "a") as log:
            log.write(f"{store.stamp(store.now())} {line}\n")
        os.chmod(self.directory / "retry.log", 0o600)

    def sweep_missed(self, hermes_finder: Callable[[], str]) -> None:
        """Queue each person's session reply that linear-agent-session's sweep found in Linear and no webhook delivered.

        A failed sweep is logged and skipped, so it never holds back the replay of what is already queued."""
        cli = self.home / "plugins" / "linear-agent-session" / "cli.py"
        if store.paused(self.home) or not cli.is_file():
            return
        try:
            hermes = hermes_finder()
        except SystemExit as failure:
            # The sweep claims what it prints, so it must not run when the prompt template cannot be read.
            self.log(f"missed prompts: no sweep: {failure}")
            return
        try:
            proc = subprocess.run(
                [sys.executable, str(cli), "missed-prompts"], capture_output=True, text=True,
                timeout=MISSED_PROMPTS_TIMEOUT_SECONDS, env={**os.environ, "HERMES_HOME": str(self.home)},
            )
        except (subprocess.TimeoutExpired, OSError) as failure:
            self.log(f"missed prompts: the sweep did not finish: {type(failure).__name__}")
            return
        if proc.returncode != 0:
            self.log(f"missed prompts: the sweep failed ({proc.returncode}): {redact(proc.stderr)[-store.ERROR_LIMIT:]}")
            return
        try:
            found = json.loads(proc.stdout)
            route, prompts = found["route"], found["prompts"]
            entries = [self.missed_entry(hermes, route, payload) for payload in prompts]
        except (ValueError, KeyError, TypeError) as failure:
            self.log(f"missed prompts: the sweep printed output this job cannot read: {type(failure).__name__}")
            return

        def change(queue: dict[str, dict[str, Any]]) -> bool:
            return any([store.apply(queue, key, "missed", **fields) for key, fields in entries])

        store.mutate(self.directory, change)

    def missed_entry(self, hermes: str, route: str, payload: dict) -> tuple[str, dict[str, Any]]:
        profile = self.home.name if self.home.parent.name == "profiles" else "default"
        delivery = "missed-" + payload["agentActivity"]["id"]
        delivered_at = store.stamp(dt.datetime.fromisoformat(payload["createdAt"].replace("Z", "+00:00")))
        template = route_setting(hermes, self.home, route, "prompt")
        key = store.encode_key(profile, route, delivery)
        return key, {
            "key": key, "profile": profile, "route": route, "delivery": delivery, "delivered_at": delivered_at,
            "summary": {"event": "AgentSessionEvent", "action": "prompted", "subject": payload["agentSession"]["issue"]["identifier"]},
            "prompt": render_prompt(template if isinstance(template, str) else "", payload, "", route), "payload": payload, "attempts": 0, "last_error": "",
            "next_try_at": delivered_at, "reported": False, "admitted": False, "reason": "missed",
        }

    def announce(self) -> None:
        for key, entry in self.entries().items():
            if entry["state"] == "queued" and not entry["reported"]:
                summary = entry["summary"]
                self.say(
                    f"queued {entry['route']} {entry['delivery']} {summary['event']} {summary['action']} "
                    f"{summary['subject']}: {store.reason_of(entry)} (next try {entry['next_try_at']})"
                )
                self.mark_reported(key)

    def due(self, everything: bool) -> list[str]:
        moment = store.now()
        queued = [(key, entry) for key, entry in self.entries().items() if entry["state"] == "queued"]
        queued.sort(key=lambda pair: pair[1]["delivered_at"])
        due = []
        waiting = set()
        for key, entry in queued:
            subject = entry["summary"]["subject"]
            # A later delivery waits behind an earlier one of its subject, such as a Done update behind a "Try again" reply.
            if not everything and (store.parse_stamp(entry["next_try_at"]) or moment) > moment:
                waiting.add(subject)
            if subject not in waiting:
                due.append(key)
        return due

    def cli_answer(self, entry: dict[str, Any], command: str, timeout: int) -> Optional[dict]:
        cli = self.home / "plugins" / "linear-agent-session" / "cli.py"
        if not cli.is_file():
            return None
        with tempfile.TemporaryDirectory(prefix="webhook-retry-") as scratch:
            payload_file = Path(scratch) / "payload.json"
            payload_file.write_text(json.dumps(entry["payload"]))
            os.chmod(payload_file, 0o600)
            try:
                proc = subprocess.run(
                    [sys.executable, str(cli), command, "--payload-file", str(payload_file)],
                    capture_output=True, text=True, timeout=timeout,
                    env={**os.environ, "HERMES_HOME": str(self.home)},
                )
            except subprocess.TimeoutExpired:
                return None
        try:
            answer = json.loads(proc.stdout)
        except ValueError:
            return None
        return answer if proc.returncode == 0 and isinstance(answer, dict) else None

    def triage_exit_handled(self, entry: dict[str, Any]) -> bool:
        """Run linear-agent-session's triage-exit check. Anything but a clean `handled` answer replays the turn."""
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "Issue":
            return False
        answer = self.cli_answer(entry, "triage-exit", TRIAGE_EXIT_TIMEOUT_SECONDS)
        return answer is not None and answer.get("handled") is True

    def addressed_to_teammate(self, entry: dict[str, Any]) -> bool:
        """Run linear-agent-session's teammate-reply check. Anything but a clean `addressed` answer replays the turn."""
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "AgentSessionEvent":
            return False
        answer = self.cli_answer(entry, "teammate-reply", TEAMMATE_REPLY_TIMEOUT_SECONDS)
        return answer is not None and answer.get("addressed") is True

    def comment_skipped(self, entry: dict[str, Any]) -> bool:
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "Comment":
            return False
        # The hook decided an unadmitted comment already: it skipped it, or recorded its id. A replay later could
        # release a card that parked after the comment was written.
        if entry.get("reason") == "unadmitted":
            return True
        answer = self.cli_answer(entry, "comment-skip", TRIAGE_INTENT_TIMEOUT_SECONDS)
        return answer is not None and answer.get("skip") is True

    def intent_block(self, entry: dict[str, Any]) -> str:
        """The block the dispatch hook adds after the event section, recomputed for the stored delivery time because
        a replay skips the hook, and led by the blank line that separates it from the stored prompt.

        A Linear delivery whose CLI fails or times out replays with the `error` intent, so the prompt still ends
        with a block. Other deliveries have none.
        """
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return ""
        cli = self.home / "plugins" / "linear-agent-session" / "cli.py"
        with tempfile.TemporaryDirectory(prefix="webhook-retry-") as scratch:
            payload_file = Path(scratch) / "payload.json"
            payload_file.write_text(json.dumps(payload))
            os.chmod(payload_file, 0o600)
            try:
                proc = subprocess.run(
                    [sys.executable, str(cli), "triage-intent", "--payload-file", str(payload_file), "--now", entry["delivered_at"]],
                    capture_output=True, text=True, timeout=TRIAGE_INTENT_TIMEOUT_SECONDS,
                    env={**os.environ, "HERMES_HOME": str(self.home)},
                )
            except (subprocess.TimeoutExpired, OSError):
                return "\n\n" + ERROR_BLOCK
        return "\n\n" + (proc.stdout if proc.returncode == 0 and "<triage_intent>" in proc.stdout else ERROR_BLOCK)

    def replay(self, hermes: str, entry: dict[str, Any]) -> tuple[int, str, Optional[dict]]:
        toolsets = route_list(hermes, self.home, entry["route"], "toolsets")
        if not toolsets:
            return 1, f"route {entry['route']} has no toolsets in the config", None
        skills = route_list(hermes, self.home, entry["route"], "skills")
        with tempfile.TemporaryDirectory(prefix="webhook-retry-") as scratch:
            query = Path(scratch) / "query.txt"
            report = Path(scratch) / "report.json"
            query.write_text(
                REPLAY_PREAMBLE.format(delivered_at=entry["delivered_at"], cause=CAUSES[store.reason_of(entry)])
                + entry["prompt"] + self.intent_block(entry)
            )
            os.chmod(query, 0o600)
            command = [
                hermes, "chat", "-Q", "--oneshot", "--query-file", str(query), "-t", toolsets, "--source", "webhook-retry",
            ]
            if skills:
                command += ["-s", skills]
            proc = subprocess.run(
                command, capture_output=True, text=True,
                env={**os.environ, "HERMES_HOME": str(self.home), store.REPORT_VARIABLE: str(report)},
            )
            try:
                usage = json.loads(report.read_text())
            except (OSError, ValueError):
                usage = None
        return proc.returncode, proc.stderr, usage

    def retry(self, everything: bool) -> None:
        retried = 0
        deadline = store.now() + store.RUN_BUDGET
        due = self.due(everything)
        hermes = find_hermes() if due else ""
        for key in due:
            if store.now() >= deadline:
                break
            if store.paused(self.home):
                self.say("Hermes is paused: the queued deliveries wait for hermes resume")
                return
            entry = self.entries().get(key)
            if entry is None or not self.update(key, "retry_started"):
                continue
            who = f"{entry['route']} {entry['delivery']}"
            if self.triage_exit_handled(entry):
                retried += 1
                self.update(key, "retry_succeeded")
                self.say(f"retried {who}: handled the Triage exit without a turn")
                continue
            if self.addressed_to_teammate(entry):
                retried += 1
                self.update(key, "retry_succeeded")
                self.say(f"retried {who}: skipped a reply addressed to a teammate")
                continue
            if self.comment_skipped(entry):
                retried += 1
                self.update(key, "retry_succeeded")
                self.say(f"retried {who}: skipped a comment that resumes no card")
                continue
            code, stderr, usage = self.replay(hermes, entry)
            retried += 1
            if code == 0:
                self.update(key, "retry_succeeded")
                self.say(f"retried {who}: succeeded")
            elif usage is not None and usage.get("reason") in store.USAGE_LIMIT_REASONS:
                attempts = entry["attempts"] + 1
                reset = usage.get("reset_seconds")
                delay = reset if reset is not None else store.backoff_seconds(attempts)
                next_try = store.stamp(store.now() + dt.timedelta(seconds=delay))
                self.update(
                    key, "retry_usage_limit", attempts=attempts, next_try_at=next_try, reason="usage_limit",
                    last_error=store.truncate(usage.get("message", ""), store.ERROR_LIMIT),
                )
                self.say(f"retried {who}: rate-limited again, next try {next_try}")
                return
            else:
                self.update(key, "retry_failed", last_error=redact(stderr)[-store.ERROR_LIMIT:])
                self.say(f"retried {who}: failed ({code})")
        if retried and not any(e["state"] == "queued" for e in self.entries().values()):
            self.say("webhook retry queue drained")


def list_entries(directory: Path) -> int:
    with store.file_lock(directory, "queue.lock"):
        entries = store.load(directory)
    if not entries:
        print("no webhook deliveries are waiting")
        return 0
    for entry in sorted(entries.values(), key=lambda e: e["delivered_at"]):
        summary = entry["summary"]
        print(" ".join([
            entry["state"], entry["route"], entry["delivery"], summary["event"] or "-", summary["action"] or "-",
            summary["subject"] or "-", entry["delivered_at"], str(entry["attempts"]), entry["next_try_at"] or "-",
            "-" if entry["state"] == "pending" else store.reason_of(entry),
        ]))
    return 0


def drop(directory: Path, name: str) -> int:
    removed = []

    def change(entries: dict[str, dict[str, Any]]) -> bool:
        for key, entry in list(entries.items()):
            if name in (key, entry["delivery"]):
                del entries[key]
                removed.append(entry)
        return bool(removed)

    store.mutate(directory, change)
    if not removed:
        print(f"retry-failed-webhooks: no webhook delivery matches {name}", file=sys.stderr)
        return 1
    for entry in removed:
        print(f"dropped {entry['route']} {entry['delivery']}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--now", action="store_true", help="treat every queued delivery as due")
    mode.add_argument("--list", action="store_true", help="list the deliveries in the queue")
    mode.add_argument("--drop", metavar="DELIVERY", help="remove a delivery from the queue")
    args = parser.parse_args(argv)
    home_text = os.environ.get("HERMES_HOME")
    if not home_text:
        print("retry-failed-webhooks: HERMES_HOME is not set", file=sys.stderr)
        return 2
    home = Path(home_text)
    directory = store.queue_dir(home)
    if args.list:
        return list_entries(directory)
    if args.drop:
        return drop(directory, args.drop)
    with store.file_lock(directory, "run.lock", blocking=False) as acquired:
        if not acquired:
            return 0
        run = Run(home, directory)
        run.apply_caps()
        run.sweep_missed(find_hermes)
        run.announce()
        run.retry(args.now)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
