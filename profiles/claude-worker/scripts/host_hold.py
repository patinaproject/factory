"""The factory's dispatch hold list, and the host-load gate that writes to it.

`dispatch-holds.json` in the root Hermes home (two levels above the worker profile home, as `install_drift` finds
`auto-deploy.json`) is `{"holds": {<holder>: {"reason": str, "until": ISO-8601 UTC}}, "applied": bool}`. Each holder
owns its own entry: `host_load` (this module's load gate), `auto-deploy`, and later `usage_limit`. While any entry
exists, `kanban.dispatch_profiles` is `[]` in the root home and Hermes' dispatcher claims no card. `applied` records
that the list set the key, so a key set while `applied` is false is a person's hold: the list neither overwrites nor unsets it.

`dispatch-holds.log` in the same home gets one JSON line for each entry an `add` creates and each entry a removal
deletes, which `held_seconds` reads back.

Tests of the scripts that import this module set `HOST_HOLD_LOADAVG` (`one,five,fifteen`, or `error`) and
`HOST_HOLD_CPUS`, because they cannot replace `os.getloadavg` in another process.

auto-deploy loads this file with importlib under the macOS system Python 3.9, so it uses only the standard library
and no 3.10 syntax.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

HOLDS_FILE = "dispatch-holds.json"
LOG_FILE = "dispatch-holds.log"
LOCK_FILE = "dispatch-holds.lock"
HOLD_KEY = "kanban.dispatch_profiles"
HOST_LOAD = "host_load"
AUTO_DEPLOY = "auto-deploy"
HOLD_MINUTES = 10
LOAD_PER_CPU = 1.5
WINDOW = timedelta(hours=24)
STAMP = "%Y-%m-%dT%H:%M:%SZ"
LOADAVG_ENV = "HOST_HOLD_LOADAVG"
CPUS_ENV = "HOST_HOLD_CPUS"
TEST_WORKERS_ENV = "PATINAPROJECT_TEST_WORKERS"


class HoldError(Exception):
    pass


class Unreadable(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(STAMP)


def parse(text: str) -> datetime:
    return datetime.strptime(text, STAMP).replace(tzinfo=timezone.utc)


def root_home(worker_home: Path) -> Path:
    return worker_home.parent.parent


def read_holds(root: Path) -> dict:
    try:
        data = json.loads((root / HOLDS_FILE).read_text())
    except FileNotFoundError:
        return {"holds": {}, "applied": False}
    except (OSError, ValueError) as error:
        raise HoldError(f"cannot read {root / HOLDS_FILE}: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("holds"), dict) or not isinstance(data.get("applied"), bool):
        raise HoldError(f"{root / HOLDS_FILE} is not a hold list")
    return data


def hermes_config(find_binary: Callable[[], str], root: Path) -> Callable[..., subprocess.CompletedProcess]:
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [find_binary(), "config", *args], env={**os.environ, "HERMES_HOME": str(root)}, capture_output=True, text=True,
        )
    return run


class HoldList:
    def __init__(self, root: Path, config: Callable[..., subprocess.CompletedProcess],
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.root = root
        self.config = config
        self.clock = clock

    def holders(self) -> Dict[str, dict]:
        return read_holds(self.root)["holds"]

    def add(self, holder: str, reason: str, until: datetime, readings: Optional[dict] = None) -> bool:
        with self.locked():
            data = read_holds(self.root)
            created = holder not in data["holds"]
            data["holds"][holder] = {"reason": reason, "until": stamp(until)}
            self.write(data)
            if created:
                self.log("add", holder, reason, readings)
            self.apply_locked(data)
        return created

    def remove(self, holder: str, readings: Optional[dict] = None, expired: bool = False) -> bool:
        with self.locked():
            data = read_holds(self.root)
            entry = data["holds"].pop(holder, None)
            if entry is not None:
                self.write(data)
                self.log("remove", holder, entry.get("reason"), readings, expired=expired)
            self.apply_locked(data)
        return entry is not None

    def expire(self) -> List[str]:
        now = self.clock()
        gone = [holder for holder, entry in self.holders().items() if parse(entry["until"]) <= now]
        for holder in gone:
            self.remove(holder, expired=True)
        return gone

    def apply(self) -> None:
        with self.locked():
            self.apply_locked(read_holds(self.root))

    def apply_locked(self, data: dict) -> None:
        if data["holds"] and not data["applied"]:
            if self.key_present():
                return
            self.run_config("set", HOLD_KEY, "[]")
            data["applied"] = True
            self.write(data)
        elif not data["holds"] and data["applied"]:
            self.run_config("unset", HOLD_KEY)
            data["applied"] = False
            self.write(data)

    def adopt(self) -> None:
        with self.locked():
            data = read_holds(self.root)
            if not data["applied"]:
                data["applied"] = True
                self.write(data)

    def key_present(self) -> bool:
        proc = self.config("get", HOLD_KEY, "--json")
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout) is not None
            except ValueError:
                raise HoldError(f"hermes config get {HOLD_KEY} did not return JSON") from None
        if "not set" in f"{proc.stdout}{proc.stderr}":
            return False
        raise HoldError(f"hermes config get {HOLD_KEY} exited {proc.returncode}: {proc.stderr.strip()}")

    def run_config(self, *args: str) -> None:
        proc = self.config(*args)
        if proc.returncode != 0 and "not set" not in f"{proc.stdout}{proc.stderr}":
            raise HoldError(f"hermes config {' '.join(args[:2])} exited {proc.returncode}: {proc.stderr.strip()}")

    def locked(self):
        return _Lock(self.root / LOCK_FILE)

    def write(self, data: dict) -> None:
        path = self.root / HOLDS_FILE
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(temp, path)

    def log(self, event: str, holder: str, reason: Optional[str], readings: Optional[dict], **extra) -> None:
        line = {"time": stamp(self.clock()), "event": event, "holder": holder, "reason": reason, **extra}
        if readings:
            line["load"] = readings
        with open(self.root / LOG_FILE, "a") as file:
            file.write(json.dumps(line) + "\n")


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a")
        fcntl.flock(self.file, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        self.file.close()


class Reading(NamedTuple):
    one: float
    five: float
    threshold: float

    @property
    def over(self) -> bool:
        return self.one > self.threshold or self.five > self.threshold

    def describe(self) -> str:
        return f"load average {self.one:g} (1 min) and {self.five:g} (5 min) against a threshold of {self.threshold:g}"

    def readings(self) -> dict:
        return {"one": self.one, "five": self.five, "threshold": self.threshold}


def load_average() -> Tuple[float, float, float]:
    injected = os.environ.get(LOADAVG_ENV)
    if injected == "error":
        raise OSError("load average unavailable")
    if injected:
        one, five, fifteen = (float(each) for each in injected.split(","))
        return one, five, fifteen
    return os.getloadavg()


def cpu_count() -> Optional[int]:
    injected = os.environ.get(CPUS_ENV)
    return int(injected) if injected else os.cpu_count()


def read_load(configured: Optional[float], reader: Callable = load_average, cpus: Callable = cpu_count) -> Reading:
    try:
        one, five, _ = reader()
        count = cpus()
    except OSError as error:
        raise Unreadable(f"cannot read the load average: {error}") from None
    if configured is None:
        if count is None:
            raise Unreadable("os.cpu_count() is unknown, so there is no default load threshold")
        configured = LOAD_PER_CPU * count
    return Reading(one, five, configured)


def observe(holds: HoldList, configured: Optional[float], reader: Callable = load_average,
            cpus: Callable = cpu_count) -> Optional[Reading]:
    try:
        return read_load(configured, reader, cpus)
    except Unreadable as error:
        with holds.locked():
            holds.log("load-unreadable", HOST_LOAD, str(error), None)
        return None


def hold_for_load(holds: HoldList, reading: Reading) -> None:
    until = holds.clock() + timedelta(minutes=HOLD_MINUTES)
    holds.add(HOST_LOAD, reading.describe(), until, reading.readings())


def refresh(holds: HoldList, configured: Optional[float], reader: Callable = load_average,
            cpus: Callable = cpu_count) -> Optional[Reading]:
    reading = observe(holds, configured, reader, cpus)
    if reading is not None and reading.over:
        hold_for_load(holds, reading)
    else:
        holds.remove(HOST_LOAD, reading.readings() if reading else None)
    return reading


def test_workers(cpus: int, runs: int) -> int:
    return max(1, cpus // max(runs, 1))


def log_events(root: Path) -> List[dict]:
    try:
        lines = (root / LOG_FILE).read_text().splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def held_seconds(root: Path, holder: str, now: datetime, window: timedelta = WINDOW) -> float:
    start = now - window
    total = 0.0
    since: Optional[datetime] = None
    for event in log_events(root):
        if event.get("holder") != holder or event.get("event") not in ("add", "remove"):
            continue
        try:
            moment = parse(event["time"])
        except (KeyError, ValueError):
            continue
        if event["event"] == "add":
            since = moment
        elif since is not None:
            if moment > start:
                total += (moment - max(since, start)).total_seconds()
            since = None
    if since is not None:
        total += (now - max(since, start)).total_seconds()
    return total


def overdue(root: Path, now: datetime) -> List[Tuple[str, str]]:
    return [
        (holder, entry["until"]) for holder, entry in sorted(read_holds(root)["holds"].items())
        if parse(entry["until"]) <= now
    ]
