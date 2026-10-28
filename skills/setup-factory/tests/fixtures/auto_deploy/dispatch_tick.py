import json
import sys

sys.dont_write_bytecode = True
checkout, site_packages, hold_json, release_json = sys.argv[1:5]
sys.path[:0] = [checkout, site_packages]

import os  # noqa: E402
from pathlib import Path  # noqa: E402

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_boards as kboards  # noqa: E402
from hermes_cli import kanban_db_connect as kc  # noqa: E402
from hermes_cli import kanban_db_dispatch as kd  # noqa: E402
from hermes_cli.config import set_config_value, unset_config_value  # noqa: E402

WORKER = "claude-worker"
BOARDS = (None, "triage")
profile = Path(os.environ["HERMES_HOME"]) / "profiles" / WORKER
profile.mkdir(parents=True, exist_ok=True)
(profile / "config.yaml").write_text("{}\n")


def apply(argv: list) -> None:
    if argv[:2] == ["config", "set"] and len(argv) == 4:
        set_config_value(argv[2], argv[3])
    elif argv[:2] == ["config", "unset"] and len(argv) == 3:
        unset_config_value(argv[2])
    else:
        raise SystemExit(f"not a config command: {argv}")


def card(conn, title: str, status: str) -> None:
    task = kb.create_task(conn, title=title, assignee=WORKER)
    if status != "ready":
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task))
        conn.commit()


def tick(conns: dict) -> int:
    spawned = []
    for board, conn in conns.items():
        kd.dispatch_once(conn, board=board, spawn_fn=lambda *args, **kwargs: spawned.append(args) or 999999)
    return len(spawned)


def rows(conns: dict) -> list:
    return sorted(
        [board or "default", *row]
        for board, conn in conns.items()
        for row in conn.execute("SELECT title, status, assignee, claim_lock FROM tasks")
    )


conns = {}
for board in BOARDS:
    if board:
        kboards.create_board(board)
    kb.init_db(board=board)
    conns[board] = kc.connect(board=board)
    card(conns[board], "ready before the hold", "ready")

apply(json.loads(hold_json))
held = [tick(conns)]
for board, conn in conns.items():
    card(conn, "ready during the hold", "ready")
    card(conn, "review during the hold", "review")
held.append(tick(conns))
held_rows = rows(conns)
held_scope = kd.dispatch_profile_allowlist_summary()

apply(json.loads(release_json))
print(json.dumps({
    "held_spawns": held, "held_rows": held_rows, "held_scope": held_scope,
    "released_scope": kd.dispatch_profile_allowlist_summary(), "released_spawns": tick(conns),
}))
