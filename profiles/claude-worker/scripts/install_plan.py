# Runs on the macOS system Python 3.9, because auto-deploy's launchd job does.
from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path
from typing import NamedTuple, Tuple, Union


class Step(IntEnum):
    SOURCE = 0
    BOARDS = 1
    CONFIG = 2
    PROFILE = 3
    PLUGINS = 4
    CRON = 5
    OWNED_REFS = 6
    ROUTES = 7
    WRAPPERS = 8


class Install(NamedTuple):
    step: Step
    argv: Tuple[str, ...]
    restarts_gateway: bool


class Manual(NamedTuple):
    action: str
    # True when the code a deploy installs refuses every run until a person does this.
    gates_runs: bool = False


class Advisory(NamedTuple):
    pass


Remedy = Union[Install, Manual, Advisory]
ADVISORY = Advisory()


class Drift(NamedTuple):
    kind: str
    where: str
    detail: str
    remedy: Remedy

    def __str__(self) -> str:
        return f"{self.kind} {self.where}: {self.detail}"

    def to_json(self) -> dict:
        return {
            "kind": self.kind, "where": str(self.where), "detail": self.detail, "line": str(self),
            "remedy": remedy_json(self.remedy),
        }


def remedy_json(remedy: Remedy) -> dict:
    if isinstance(remedy, Install):
        return {"type": "install", "step": remedy.step.name, "argv": list(remedy.argv), "restarts_gateway": remedy.restarts_gateway}
    if isinstance(remedy, Manual):
        return {"type": "manual", "action": remedy.action, "gates_runs": remedy.gates_runs}
    return {"type": "advisory"}


def drift_from_json(record: dict) -> Drift:
    try:
        remedy = record["remedy"]
        kind = remedy["type"]
        if kind == "install":
            parsed: Remedy = Install(Step[remedy["step"]], tuple(str(a) for a in remedy["argv"]), bool(remedy["restarts_gateway"]))
        elif kind == "manual":
            parsed = Manual(str(remedy["action"]), bool(remedy.get("gates_runs", False)))
        elif kind == "advisory":
            parsed = ADVISORY
        else:
            raise ValueError(f"unknown remedy type {kind!r}")
        return Drift(str(record["kind"]), str(record["where"]), str(record["detail"]), parsed)
    except (KeyError, TypeError) as error:
        raise ValueError(f"not a drift record: {error}") from None


class ManualLine(NamedTuple):
    line: str
    action: str
    gates_runs: bool = False


class Plan(NamedTuple):
    installs: Tuple[Install, ...]
    manual: Tuple[ManualLine, ...]

    @property
    def restarts_gateway(self) -> bool:
        return any(install.restarts_gateway for install in self.installs)


def plan(drifts) -> Plan:
    installs: list = []
    for drift in drifts:
        if isinstance(drift.remedy, Install) and drift.remedy not in installs:
            installs.append(drift.remedy)
    manual = tuple(ManualLine(str(d), d.remedy.action, d.remedy.gates_runs) for d in drifts if isinstance(d.remedy, Manual))
    return Plan(tuple(sorted(installs, key=lambda install: install.step)), manual)


class Homes(NamedTuple):
    hermes: str
    python: str
    root: Path
    worker: Path
    repo: Path

    def cli(self, home: Path) -> Tuple[str, ...]:
        return (self.hermes,) if Path(home) == self.root else (self.hermes, "-p", self.worker.name)

    def gateway_reads(self, home: Path) -> bool:
        # The gateway runs from the root home and reads its plugins and config only at startup.
        return Path(home) == self.root


def source_reset(repo: Path, branch: str, sha: str) -> Install:
    return Install(Step.SOURCE, ("git", "-C", str(repo), "checkout", "--quiet", "--force", "-B", branch, sha), False)


def board_create(homes: Homes, slug: str, name: str, workdir: str | None) -> Install:
    where = ("--default-workdir", workdir) if workdir else ()
    return Install(Step.BOARDS, (homes.hermes, "kanban", "boards", "create", slug, "--name", name, *where), False)


def board_workdir(homes: Homes, slug: str, workdir: str) -> Install:
    return Install(Step.BOARDS, (homes.hermes, "kanban", "boards", "set-default-workdir", slug, workdir), False)


def config_text(value) -> str:
    # hermes config set stores a string verbatim and parses the text of any other value.
    return value if isinstance(value, str) else json.dumps(value)


def config_set(homes: Homes, home: Path, key: str, value) -> Install:
    return Install(Step.CONFIG, (*homes.cli(home), "config", "set", key, config_text(value)), homes.gateway_reads(home))


def config_unset(homes: Homes, home: Path, key: str) -> Install:
    return Install(Step.CONFIG, (*homes.cli(home), "config", "unset", key), homes.gateway_reads(home))


def origin_head(path: str) -> Install:
    return Install(Step.CONFIG, ("git", "-C", path, "remote", "set-head", "origin", "-a"), False)


def profile_update(homes: Homes) -> Install:
    return Install(Step.PROFILE, (homes.hermes, "profile", "update", homes.worker.name, "-y"), False)


def plugin_install(homes: Homes, home: Path, name: str, *, reinstall: bool) -> Install:
    source = f"file://{homes.repo}#plugins/{name}"
    force = ("--force",) if reinstall else ()
    return Install(
        Step.PLUGINS, (*homes.cli(home), "plugins", "install", source, "--enable", "--yes-deps", *force),
        homes.gateway_reads(home),
    )


def plugin_enable(homes: Homes, home: Path, name: str) -> Install:
    return Install(Step.PLUGINS, (*homes.cli(home), "plugins", "enable", name), homes.gateway_reads(home))


def plugin_remove(homes: Homes, home: Path, name: str) -> Install:
    return Install(Step.PLUGINS, (*homes.cli(home), "plugins", "remove", name), homes.gateway_reads(home))


def schedule(minutes: int) -> str:
    return f"every {minutes // 60}h" if minutes % 60 == 0 else f"every {minutes}m"


def cron_command(homes: Homes, home: Path, *args: str) -> Install:
    return Install(Step.CRON, (*homes.cli(home), "cron", *args), False)


def refresh_owned_refs(homes: Homes) -> Install:
    script = homes.worker / "scripts" / "refresh-owned-refs"
    return Install(Step.OWNED_REFS, ("/usr/bin/env", f"HERMES_HOME={homes.worker}", homes.python, str(script)), False)


def route_set(homes: Homes, key: str, value) -> Install:
    return Install(Step.ROUTES, (homes.hermes, "config", "set", key, config_text(value)), True)


def wrapper_install(homes: Homes, name: str) -> Install:
    source = homes.repo / "skills" / "setup-factory" / "scripts" / name
    return Install(Step.WRAPPERS, ("install", "-m", "0644", str(source), str(homes.root / "scripts" / name)), False)

