#!/usr/bin/env python3
"""Stand-ins for the commands auto-deploy runs, chosen by the name the test copies this file to.

`hermes` keeps its config key, running cards, and pause in `$FAKE_DEPLOY/hermes.json` and
`$HERMES_HOME/ESTOP`. `check-install` prints `$FAKE_DEPLOY/check_install.json`'s drift, less each
Install drift whose command already ran, unless the drift is `sticky`. `restart-gateway` and
`check-settings` exit as `$FAKE_DEPLOY/tools.json` says. Every call is appended to
`$FAKE_DEPLOY/calls.jsonl` with the pause, the hold, and the source checkout's state at that moment.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

FAKE = Path(os.environ["FAKE_DEPLOY"])
NAME = Path(sys.argv[0]).name


def load(name: str, default):
    path = FAKE / name
    return json.loads(path.read_text()) if path.exists() else default


def source_state() -> dict:
    source = os.environ.get("FAKE_SOURCE")
    if not source:
        return {}
    head = subprocess.run(["git", "-C", source, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", source, "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout
    return {"source_head": head, "dirty": bool(dirty.strip())}


def log(argv: list, hermes: dict) -> None:
    root = Path(hermes["root"])
    entry = {
        "tool": NAME, "argv": argv, "paused": (root / "ESTOP").exists(), "held": hermes["key"],
        "running": sum(len(seq[0]) for seq in hermes["running"].values()), **source_state(),
    }
    with open(FAKE / "calls.jsonl", "a") as handle:
        handle.write(json.dumps(entry) + "\n")


def calls() -> list:
    path = FAKE / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def hermes() -> int:
    state = load("hermes.json", None)
    argv = sys.argv[1:]
    log(argv, state)
    home = Path(os.environ["HERMES_HOME"])
    if argv[:1] == ["-p"]:
        home, argv = home / "profiles" / argv[1], argv[2:]
    out, code = "", 0
    if argv[:3] == ["kanban", "boards", "list"]:
        out = json.dumps([{"slug": slug, "archived": False} for slug in state["running"]])
    elif argv[:2] == ["kanban", "--board"] and argv[3] == "list":
        seq = state["running"][argv[2]]
        out = json.dumps(seq.pop(0) if len(seq) > 1 else seq[0])
    elif argv[:2] == ["kanban", "diagnostics"]:
        scope = "none (fail-closed: present)" if state["key"] else "any"
        out = json.dumps([{"task_id": None, "dispatch_profiles": scope, "diagnostics": []}])
    elif argv == ["config", "set", "kanban.dispatch_profiles", "[]"]:
        state["key"] = True
    elif argv == ["config", "unset", "kanban.dispatch_profiles"]:
        if state["key"]:
            state["key"] = False
        else:
            out, code = "Config key not set: kanban.dispatch_profiles", 1
    elif argv[:1] == ["pause"]:
        (home / "ESTOP").write_text(json.dumps({"reason": argv[2] if len(argv) > 2 else None, "engaged_at": "now"}))
    elif argv == ["resume"]:
        if (home / "ESTOP").exists():
            (home / "ESTOP").unlink()
    for rule in state.get("fail", []):
        if argv == rule["argv"]:
            code = rule.get("code", 1)
            sys.stderr.write(rule.get("stderr", "") + "\n")
    for hook in state.get("on", []):
        if argv == hook["argv"] and not hook.get("done"):
            hook["done"] = True
            subprocess.run(hook["sh"], shell=True, check=True)
    (FAKE / "hermes.json").write_text(json.dumps(state))
    if out:
        print(out)
    return code


def check_install() -> int:
    if "--print-source" in sys.argv:
        print(os.environ["FAKE_SOURCE"])
        return 0
    log([*sys.argv[1:]], load("hermes.json", None))
    config = load("check_install.json", {"drifts": []})
    if "exit" in config:
        sys.stderr.write(config.get("stderr", "") + "\n")
        return config["exit"]
    ran = [call["argv"] for call in calls() if call["tool"] == "hermes"]
    drifts = []
    for drift in config["drifts"]:
        remedy = drift["remedy"]
        if remedy["type"] == "install" and not drift.get("sticky") and remedy["argv"][1:] in ran:
            continue
        drifts.append({**drift, "line": f"{drift['kind']} {drift['where']}: {drift['detail']}"})
    print(json.dumps(drifts))
    return 1 if drifts else 0


def restart_gateway() -> int:
    log(sys.argv[1:], load("hermes.json", None))
    hermes_bin = sys.argv[sys.argv.index("--hermes") + 1]
    subprocess.run([hermes_bin, "gateway", "restart"], check=True)
    return load("tools.json", {}).get("restart-gateway", 0)


def check_settings() -> int:
    log(sys.argv[1:], load("hermes.json", None))
    code = load("tools.json", {}).get("check-settings", 0)
    if code:
        print("claude-worker: claude_session.model differs")
    return code


if __name__ == "__main__":
    sys.exit({"hermes": hermes, "check-install": check_install, "restart-gateway": restart_gateway,
              "check-settings": check_settings}[NAME]())
