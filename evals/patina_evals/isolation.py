"""Fresh, isolated state for each trial.

A trial gets its own home, repository (one commit of the pre-fix state, no
remote, no history), services state, and temp folder. Commands run with an
empty environment plus an allowlist, in their own process group, under CPU and
memory ceilings. Every network endpoint a worker could reach points at a
refusing address. References and graders never enter the trial.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from patina_evals.manifest import Manifest

HARNESS = Path(__file__).resolve().parent
LAUNCH = HARNESS / "launch.py"
MOCK_TRACKER = HARNESS / "mock_tracker.py"
BLOCKED_URL_ENV = "EVALS_BLOCKED_URL"
BLOCKED_URL = "http://127.0.0.1:9"
BLOCKED_URL_VARS = (
    "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "GOOGLE_GEMINI_BASE_URL", "LINEAR_API_URL", "GITHUB_API_URL",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
)
FIXED_GIT_DATE = "2026-01-01T00:00:00+00:00"
WRITER_LOCK = "patina-writer.lock"


@dataclass(frozen=True)
class Limits:
    cpu_seconds: int = 60
    memory_bytes: int = 2 * 1024**3
    command_timeout_seconds: int = 120
    trial_timeout_seconds: int = 600
    concurrency: int = 2

    def record(self) -> dict:
        return {
            "sandbox": "local",
            "cpu": {
                "guarantee_cores": None,
                "guarantee_note": "the local sandbox shares the host CPU; no share is reserved",
                "ceiling_cpu_seconds_per_command": self.cpu_seconds,
                "ceiling_enforced": True,
            },
            "memory": {
                "guarantee_bytes": None,
                "guarantee_note": "the local sandbox shares the host memory; none is reserved",
                "ceiling_bytes_per_command": self.memory_bytes,
                "ceiling_enforced": sys.platform == "linux",
            },
            "concurrency": self.concurrency,
            "timeouts": {"command_seconds": self.command_timeout_seconds, "trial_seconds": self.trial_timeout_seconds},
        }


@dataclass(frozen=True)
class Trial:
    id: str
    root: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def services(self) -> Path:
        return self.root / "services"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def harness(self) -> Path:
        return self.root / ".harness"

    @property
    def tracker_state(self) -> Path:
        return self.services / "tracker.json"


class WriterBusy(RuntimeError):
    pass


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout


def prepare(root: Path, manifest: Manifest, trial_id: str) -> Trial:
    trial = Trial(trial_id, root)
    for folder in (trial.home, trial.services, trial.tmp, trial.harness, trial.home / "bin"):
        folder.mkdir(parents=True, exist_ok=False)
    shutil.copytree(manifest.repo_dir, trial.repo)
    (trial.home / "bin" / "python3").symlink_to(Path(os.path.realpath(getattr(sys, "_base_executable", sys.executable))))
    shutil.copy2(MOCK_TRACKER, trial.services / "mock_tracker.py")
    tracker = manifest.services.get("tracker")
    state = json.loads((manifest.case_dir / tracker).read_text(encoding="utf-8")) if tracker else {"issues": {}, "writes": []}
    trial.tracker_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (trial.home / ".gitconfig").write_text("[user]\n\tname = Trial\n\temail = trial@example.invalid\n", encoding="utf-8")
    env = env_for(trial)
    git(trial.repo, "init", "-q", "-b", "main", env=env)
    git(trial.repo, "add", "-A", env=env)
    git(trial.repo, "commit", "-q", "-m", "pre-fix state", env=env | {"GIT_AUTHOR_DATE": FIXED_GIT_DATE, "GIT_COMMITTER_DATE": FIXED_GIT_DATE})
    return trial


def env_for(trial: Trial, actor: str = "agent") -> dict:
    blocked = os.environ.get(BLOCKED_URL_ENV, BLOCKED_URL)
    env = {
        "HOME": str(trial.home), "TMPDIR": str(trial.tmp), "PATH": f"{trial.home / 'bin'}:/usr/bin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(trial.home / ".gitconfig"),
        "GIT_CEILING_DIRECTORIES": str(trial.root),
        "TRACKER_STATE": str(trial.tracker_state), "TRACKER_ACTOR": actor, "NO_PROXY": "",
    }
    env.update(dict.fromkeys(BLOCKED_URL_VARS, blocked))
    return env


def command(trial: Trial, argv: list[str], limits: Limits, actor: str = "agent") -> list[str]:
    """Run `argv` with only the trial's environment, in a new process group, under the ceilings."""
    pairs = [f"{k}={v}" for k, v in sorted(env_for(trial, actor).items())]
    return [
        "/usr/bin/env", "-i", *pairs, os.path.realpath(getattr(sys, "_base_executable", sys.executable)), str(LAUNCH),
        "--cpu-seconds", str(limits.cpu_seconds), "--memory-bytes", str(limits.memory_bytes),
        "--pgids", str(trial.harness / "pgids"), "--", *argv,
    ]


def tool_argv(trial: Trial, tool: str, args: list[str]) -> list[str]:
    tools = {
        "tracker": ["python3", str(trial.services / "mock_tracker.py")],
        "python3": ["python3"],
        "sh": ["/bin/sh"],
        "git": ["git"],
    }
    if tool not in tools:
        raise ValueError(f"unknown tool {tool}; known: {', '.join(tools)}")
    return [*tools[tool], *args]


def acquire_writer(trial: Trial, owner: str) -> Path:
    """One writer per trial worktree. A second writer is refused, not queued."""
    lock = trial.repo / ".git" / WRITER_LOCK
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise WriterBusy(f"{trial.repo} already has a writer: {lock.read_text(encoding='utf-8').strip()}") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(owner + "\n")
    return lock


def release_writer(lock: Path) -> None:
    lock.unlink(missing_ok=True)


def teardown(trial: Trial) -> list[int]:
    """Kill every process group the trial started. Returns the groups that were still alive."""
    path = trial.harness / "pgids"
    if not path.exists():
        return []
    leaked = []
    for pgid in sorted({int(line) for line in path.read_text(encoding="utf-8").split()}):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        leaked.append(pgid)
    return leaked


def fingerprint(root: Path, patterns: tuple[str, ...] | list[str]) -> dict[str, str]:
    """sha256 of every file under `root` matching any glob, keyed by relative path."""
    files = {p for pattern in patterns for p in root.glob(pattern) if p.is_file()}
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def tree_fingerprint(root: Path) -> dict[str, str]:
    return fingerprint(root, ["**/*"]) if root.exists() else {}
