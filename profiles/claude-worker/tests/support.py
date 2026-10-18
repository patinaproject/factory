from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
REPO = SCRIPTS.parents[2]
BOT_LOGIN = "factory-app[bot]"

SHEBANG = "#!/usr/bin/env python3\n"


def with_interpreter(script: str) -> str:
    """Pin a fake script to the interpreter running the tests.

    `env python3` follows the PATH claude-session hands the script. On macOS
    that can reach /usr/bin/python3, a shim that adds SDKROOT, CPATH,
    LIBRARY_PATH, and MANPATH to the environment the fake then records.
    """
    assert script.startswith(SHEBANG)
    return f"#!{sys.executable}\n" + script[len(SHEBANG):]


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
# The real claude gets no test variables, so the fake reads its options from the file beside it.
options = json.load(open(__file__ + ".json"))
argv = sys.argv[1:]
settings = None
if "--settings" in argv:
    path = argv[argv.index("--settings") + 1]
    settings = {"content": json.load(open(path)), "mode": oct(os.stat(path).st_mode & 0o777)}
    # Like Claude Code, run the SessionStart hooks once the settings are read.
    import subprocess
    hooks = {} if options.get("skip_start_hooks") else settings["content"].get("hooks", {})
    for group in hooks.get("SessionStart", []):
        for hook in group["hooks"]:
            subprocess.run(hook["command"], shell=True, check=True)
    settings["exists_after_start"] = os.path.exists(path)
with open(options["log"] + ".tmp", "w") as log:
    json.dump({"argv": argv, "cwd": os.getcwd(), "env": dict(os.environ), "settings": settings, "pid": os.getpid()}, log)
os.rename(options["log"] + ".tmp", options["log"])
if options.get("hold_seconds"):
    import time
    time.sleep(options["hold_seconds"])
leftover = None
if options.get("leftover_pid_file"):
    import subprocess
    leftover = subprocess.Popen(["sleep", "600"], start_new_session=bool(options.get("leftover_detached")))
    with open(options["leftover_pid_file"], "w") as pid_file:
        pid_file.write(str(leftover.pid))
for line in options.get("transcript_lines", []):
    with open(options["transcript"], "a") as transcript:
        transcript.write(json.dumps(line) + "\\n")
print(json.dumps({
    **options.get("output", {}),
    "type": "result",
    "subtype": options.get("subtype", "success"),
    "is_error": False,
    "result": options.get("result", "Opened the pull request."),
    "session_id": "ignored",
    "num_turns": 7,
}), flush=True)
if leftover and options.get("waits_for_leftover"):
    leftover.wait()
"""


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, unquote

state_path = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(state_path + ".log", "a") as log:
    log.write(json.dumps(args) + "\\n")
path, _, query = args[1].partition("?")
parts = [unquote(part) for part in path.split("/")]
prefix = "/".join(parts[:3])
if prefix != "repos/" + state["repo"]:
    sys.exit("gh: Not Found (HTTP 404)")
if parts[3] == "branches":
    if "/".join(parts[4:]) not in state["branches"]:
        sys.exit("gh: Branch not found (HTTP 404)")
    print(json.dumps({"name": "/".join(parts[4:])}))
elif parts[3] == "activity":
    params = parse_qs(query)
    ref = params["ref"][0].removeprefix("refs/heads/")
    records = [
        {"activity_type": "branch_creation", "actor": {"login": login}, "timestamp": f"2026-09-01T00:00:{i:02d}Z"}
        for i, login in enumerate(state["branches"].get(ref, []))
    ] + [{"activity_type": "push", "actor": {"login": "person"}, "timestamp": at} for at in state.get("pushes", {}).get(ref, [])]
    records = [record for record in records if record["activity_type"] in params.get("activity_type", [record["activity_type"]])]
    records.sort(key=lambda record: record["timestamp"], reverse=True)
    print(json.dumps([{**record, "ref": "refs/heads/" + ref} for record in records][:int(params.get("per_page", ["30"])[0])]))
elif parts[3] == "pulls" and len(parts) == 4:
    head = parse_qs(query)["head"][0].partition(":")[2]
    print(json.dumps([
        {**pull, "number": int(number)} for number, pull in state["pulls"].items()
        if pull["state"] == "open" and pull["head"]["ref"] == head
    ]))
elif parts[3] == "compare":
    base, _, head = parts[4].partition("...")
    print(json.dumps({"status": state.get("compare", {}).get(parts[4], "diverged")}))
elif parts[3] == "pulls":
    pull = state["pulls"][parts[4]]
    fields = {args[i + 1].partition("=")[0]: args[i + 1].partition("=")[2] for i, arg in enumerate(args) if arg == "-f"}
    if "-X" in args:
        if state.get("fail_writes"):
            sys.exit("gh: Resource not accessible by integration (HTTP 403)")
        pull.update(fields)
        json.dump(state, open(state_path, "w"))
    print(json.dumps({**pull, "number": int(parts[4])}))
elif parts[3] == "issues" and "-X" in args:
    if state.get("fail_writes"):
        sys.exit("gh: Resource not accessible by integration (HTTP 403)")
    state.setdefault("comments", {}).setdefault(parts[4], []).append(args[args.index("-f") + 1].partition("=")[2])
    json.dump(state, open(state_path, "w"))
    print(json.dumps({}))
else:
    sys.exit("gh: Not Found (HTTP 404)")
"""


class FakeGithub:
    """A gh that answers the branch, activity, and pull request reads that card_branch and claude-session make.

    A branch maps to the actors of its `branch_creation` records, oldest first. An empty list is a branch with no record.
    `push` adds a push record. Every creation record predates the pull requests' default `created_at`.
    """

    def __init__(self, directory: Path, repo: str) -> None:
        self.path = directory / "fake-gh"
        self.path.write_text(with_interpreter(FAKE_GH))
        os.chmod(self.path, 0o755)
        self.state_path = directory / "fake-gh-state.json"
        self.state = {"repo": repo, "branches": {}, "pulls": {}}
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def create_branch(self, name: str, *actors: str) -> None:
        self.state["branches"][name] = list(actors)
        self.save()

    def add_pull(self, number: int, head: str, *, state: str = "open", merged: bool = False,
                 head_repo: str | None = None, updated_at: str = "2026-10-01T00:00:00Z",
                 author: str = "person", body: str = "", created_at: str = "2026-10-01T00:00:00Z") -> None:
        self.state["pulls"][str(number)] = {
            "state": state, "merged": merged, "updated_at": updated_at, "user": {"login": author}, "body": body,
            "html_url": f"https://github.com/{self.state['repo']}/pull/{number}",
            "created_at": created_at,
            "head": {"ref": head, "sha": f"sha{number}", "repo": {"full_name": head_repo or self.state["repo"]}},
        }
        self.save()

    def push(self, branch: str, at: str) -> None:
        """A push record on `branch` in the repository's activity, at timestamp `at`."""
        self.state.setdefault("pushes", {}).setdefault(branch, []).append(at)
        self.save()

    def set_compare(self, base: int, head: int, status: str) -> None:
        """What GitHub answers when it compares pull request `base`'s head with pull request `head`'s head."""
        self.state.setdefault("compare", {})[f"sha{base}...sha{head}"] = status
        self.save()

    def pull(self, number: int) -> dict:
        return json.loads(self.state_path.read_text())["pulls"][str(number)]

    def comments(self, number: int) -> list:
        return json.loads(self.state_path.read_text()).get("comments", {}).get(str(number), [])

    def fail_writes(self) -> None:
        self.state["fail_writes"] = True
        self.save()

    def env(self) -> dict:
        return {"GH_BIN": str(self.path), "FAKE_GH_STATE": str(self.state_path)}

    def calls(self) -> list:
        log = Path(str(self.state_path) + ".log")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class GitFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        seed = self.tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("seed\n")
        git(seed, "add", "README.md")
        git(seed, "commit", "-q", "-m", "seed")
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "clone", "-q", "--bare", str(seed), str(self.origin))
        self.main = self.tmp / "main"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.main))

    def add_worktree(self, branch: str) -> Path:
        path = self.main / ".worktrees" / branch
        git(self.main, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def push_upstream_commit(self, name: str, ref: str = "refs/heads/main", base: str | None = None) -> str:
        """Push one commit to ref, on top of base when given, else on the default branch."""
        other = self.tmp / f"other-{name}"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        if base:
            git(other, "fetch", "-q", "origin", base)
            git(other, "reset", "-q", "--hard", "FETCH_HEAD")
        (other / f"{name}.txt").write_text(name)
        git(other, "add", f"{name}.txt")
        git(other, "commit", "-q", "-m", name)
        git(other, "push", "-q", "origin", f"+HEAD:{ref}")
        return git(other, "rev-parse", "HEAD")

    def write_settings(
        self, repositories: list, claude_session: dict | None = None, login: str | None = BOT_LOGIN
    ) -> Path:
        path = self.tmp / "settings.json"
        settings = {"repositories": repositories, "claude_session": claude_session or {}}
        if login:
            settings["github"] = {"login": login}
        path.write_text(json.dumps(settings))
        return path

    def make_fake_claude(self, log: Path) -> Path:
        path = self.tmp / "fake-claude"
        path.write_text(with_interpreter(FAKE_CLAUDE))
        os.chmod(path, 0o755)
        self.fake_claude_options = {"log": str(log)}
        self.configure_fake_claude()
        return path

    def configure_fake_claude(self, **options) -> None:
        self.fake_claude_options.update(options)
        (self.tmp / "fake-claude.json").write_text(json.dumps(self.fake_claude_options))

    def install_linear_plugin(
        self, home: Path, cached_token: str | None = "cached-token", *, accepted: tuple = ("cached-token",)
    ) -> None:
        """The real plugin client under a worker home, against a fake Linear that accepts the accepted tokens."""
        plugin = home / "plugins" / "linear-agent-session"
        plugin.mkdir(parents=True)
        (plugin / "linear.py").symlink_to(REPO / "plugins" / "linear-agent-session" / "tests" / "staged_linear.py")
        (home / "linear-fake.json").write_text(json.dumps({"accepted": list(accepted)}))
        if cached_token:
            cache = home / "plugin-data" / "linear-agent-session" / "token.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(json.dumps({"client_id": "id1", "access_token": cached_token, "expires_at": 4102444800}))

    @staticmethod
    def linear_requests(home: Path) -> list:
        log = home / "linear-fake.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
