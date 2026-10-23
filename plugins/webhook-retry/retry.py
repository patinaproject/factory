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
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import queue_store as store  # noqa: E402

REPLAY_PREAMBLE = """\
This is a replay of a webhook delivery that arrived at {delivered_at} and could not run then because the model's usage limit was reached.
Treat {delivered_at} as the current time. Wherever a step compares with the current time, such as a freshness check, use {delivered_at} and not `date -u`.

"""
# Above the check's own worst case: three `hermes` reads of up to 60 s each, the live issue read, and a 120 s archive.
TRIAGE_EXIT_TIMEOUT_SECONDS = 360
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


def find_hermes() -> str:
    candidates = [os.environ.get("HERMES_BIN"), shutil.which("hermes"), str(Path.home() / ".local" / "bin" / "hermes")]
    for candidate in filter(None, candidates):
        if os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit("retry-failed-webhooks: hermes is not on PATH or in ~/.local/bin; set HERMES_BIN")


def route_toolsets(hermes: str, home: Path, route: str) -> str:
    proc = subprocess.run(
        [hermes, "config", "get", f"platforms.webhook.extra.routes.{route}.toolsets", "--json"],
        env={**os.environ, "HERMES_HOME": str(home)}, capture_output=True, text=True,
    )
    try:
        value = json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        return ""
    return ",".join(value) if isinstance(value, list) else (value or "")


class Run:
    def __init__(self, home: Path, directory: Path) -> None:
        self.home = home
        self.directory = directory

    def say(self, line: str) -> None:
        print(line)
        with open(self.directory / "retry.log", "a") as log:
            log.write(f"{store.stamp(store.now())} {line}\n")
        os.chmod(self.directory / "retry.log", 0o600)

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

    def apply_caps(self) -> None:
        moment = store.now()
        for key, entry in self.entries().items():
            if entry["state"] == "running" and self.update(key, "interrupted", last_error="a retry was interrupted"):
                self.say(f"interrupted {entry['route']} {entry['delivery']}: not retried again, it may have run")
                continue
            age = moment - store.parse_stamp(entry["delivered_at"])
            if entry["state"] == "pending" and age > store.PENDING_AGE:
                self.update(key, "expire")
            elif entry["state"] == "queued" and (age > store.GIVE_UP_AGE or entry["attempts"] >= store.MAX_ATTEMPTS):
                reason = "older than 48 hours" if age > store.GIVE_UP_AGE else f"{entry['attempts']} attempts"
                if self.update(key, "give_up"):
                    self.say(f"gave up {entry['route']} {entry['delivery']}: {reason}")

    def announce(self) -> None:
        for key, entry in self.entries().items():
            if entry["state"] == "queued" and not entry["reported"]:
                summary = entry["summary"]
                self.say(
                    f"queued {entry['route']} {entry['delivery']} {summary['event']} {summary['action']} "
                    f"{summary['subject']}: waiting for usage (next try {entry['next_try_at']})"
                )
                self.mark_reported(key)

    def due(self, everything: bool) -> list[str]:
        moment = store.now()
        queued = [(key, entry) for key, entry in self.entries().items() if entry["state"] == "queued"]
        queued.sort(key=lambda pair: pair[1]["delivered_at"])
        due = []
        for key, entry in queued:
            # A later delivery waits behind an earlier one, such as a Done update behind a "Try again" reply.
            if not everything and (store.parse_stamp(entry["next_try_at"]) or moment) > moment:
                break
            due.append(key)
        return due

    def triage_exit_handled(self, entry: dict[str, Any]) -> bool:
        """Run linear-agent-session's triage-exit check. Anything but a clean `handled` answer replays the turn."""
        cli = self.home / "plugins" / "linear-agent-session" / "cli.py"
        if not isinstance(entry.get("payload"), dict) or not cli.is_file():
            return False
        with tempfile.TemporaryDirectory(prefix="webhook-retry-") as scratch:
            payload = Path(scratch) / "payload.json"
            payload.write_text(json.dumps(entry["payload"]))
            os.chmod(payload, 0o600)
            try:
                proc = subprocess.run(
                    [sys.executable, str(cli), "triage-exit", "--payload-file", str(payload)],
                    capture_output=True, text=True, timeout=TRIAGE_EXIT_TIMEOUT_SECONDS,
                    env={**os.environ, "HERMES_HOME": str(self.home)},
                )
            except subprocess.TimeoutExpired:
                return False
        try:
            answer = json.loads(proc.stdout)
        except ValueError:
            return False
        return proc.returncode == 0 and isinstance(answer, dict) and answer.get("handled") is True

    def replay(self, hermes: str, entry: dict[str, Any]) -> tuple[int, str, Optional[dict]]:
        toolsets = route_toolsets(hermes, self.home, entry["route"])
        if not toolsets:
            return 1, f"route {entry['route']} has no toolsets in the config", None
        with tempfile.TemporaryDirectory(prefix="webhook-retry-") as scratch:
            query = Path(scratch) / "query.txt"
            report = Path(scratch) / "report.json"
            query.write_text(REPLAY_PREAMBLE.format(delivered_at=entry["delivered_at"]) + entry["prompt"])
            os.chmod(query, 0o600)
            command = [
                hermes, "chat", "-Q", "--oneshot", "--query-file", str(query), "-t", toolsets, "--source", "webhook-retry",
            ]
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
            entry = self.entries().get(key)
            if entry is None or not self.update(key, "retry_started"):
                continue
            who = f"{entry['route']} {entry['delivery']}"
            if self.triage_exit_handled(entry):
                retried += 1
                self.update(key, "retry_succeeded")
                self.say(f"retried {who}: handled the Triage exit without a turn")
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
                    key, "retry_usage_limit", attempts=attempts, next_try_at=next_try,
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
        run.announce()
        run.retry(args.now)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
