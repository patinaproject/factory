"""Archive a Kanban card and free the branch its worktree holds.

Archiving ends a running worker, and Hermes removes the card's worktree only when
the worktree is clean and every commit is on a remote. Otherwise the worktree
stays and keeps the card's branch checked out. Git refuses to check out one
branch in two worktrees, so the next card for the same issue could not start.
This command detaches the leftover worktree's HEAD. The branch, the commits,
and the uncommitted files all stay where they are.

It prints one JSON line and exits 0 with `{"archived", "freed_worktree"}`, or exits 1
with `{"error": reason}`. Run it again after a failure: it skips a card that is
already archived and still frees the worktree. `--board` names the card's board, such
as `triage`, when it is not the current one.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


PREFLIGHT_RACE = "kanban.db-wal is read-only for this user"


class Failed(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def run(*command: str) -> str:
    try:
        proc = subprocess.run(command, capture_output=True, text=True)
    except OSError as failure:
        raise Failed(f"{command[0]} could not run: {failure.strerror or failure}") from failure
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no error output"
        raise Failed(f"{' '.join(command[:3])} failed: {detail}", proc.stderr)
    return proc.stdout


def run_kanban(*command: str) -> str:
    """Retry once when Hermes' preflight loses a race with another `hermes kanban` process.

    That process removes the `-wal` file between the preflight's existence check and its
    permission check, so the call fails with a message about a file that is gone.
    """
    try:
        return run(*command)
    except Failed as failure:
        if PREFLIGHT_RACE not in failure.stderr:
            raise
        return run(*command)


def read_task(kanban: list[str], task_id: str) -> dict:
    return json.loads(run_kanban(*kanban, "show", task_id, "--json"))["task"]


def is_linked_worktree(path: Path) -> bool:
    return (path / ".git").is_file()


def free_worktree(task: dict) -> str | None:
    if task["workspace_kind"] != "worktree" or not task["workspace_path"]:
        return None
    path = Path(task["workspace_path"]).expanduser()
    if not path.is_dir() or not is_linked_worktree(path):
        return None
    if subprocess.run(["git", "-C", str(path), "symbolic-ref", "--quiet", "HEAD"], capture_output=True).returncode != 0:
        return None
    run("git", "-C", str(path), "checkout", "--quiet", "--detach")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_id")
    parser.add_argument("--board", help="the card's board, when it is not the current one")
    args = parser.parse_args(argv)
    kanban = [os.environ.get("HERMES_BIN", "hermes"), "kanban", *(["--board", args.board] if args.board else [])]
    try:
        task = read_task(kanban, args.task_id)
        if task["status"] != "archived":
            run_kanban(*kanban, "archive", args.task_id)
            task = read_task(kanban, args.task_id)
        freed = free_worktree(task)
    except (Failed, KeyError, ValueError) as failure:
        print(json.dumps({"error": str(failure)}))
        return 1
    print(json.dumps({"archived": args.task_id, "freed_worktree": freed}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
