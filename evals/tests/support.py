import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1]

GRADER = '''
PROBES = {"answer": {"argv": ["python3", "-c", "print(open('answer.txt').read().strip())"]}}


def grade(observed):
    return {"checks": {"answer_is_yes": observed["probes"]["answer"]["stdout"].strip() == "yes"}, "forbidden": {}}
'''


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def synthetic_case(root: Path, case_id: str, *, suite="regression", split="development", reference="yes",
                   reference_stream=None, grader=GRADER, usage=None, extra_solutions=None) -> None:
    """A minimal case: the reference writes `answer.txt`, and the grader wants it to say yes."""
    case, graders = root / "cases" / case_id, root / "graders" / case_id
    (case / "repo").mkdir(parents=True)
    (case / "repo" / "answer.txt").write_text("unset\n", encoding="utf-8")
    (case / "task.md").write_text("Write yes into answer.txt.\n", encoding="utf-8")
    solutions = {"reference": {"expect": "pass"}, **(extra_solutions or {})}
    write_json(case / "manifest.json", {
        "manifest_version": 1, "id": case_id, "version": "1.0.0", "task": {"file": "task.md", "version": "1.0.0"},
        "area": "synthetic", "group": f"group-{case_id}", "split": split, "suite": suite,
        "provenance": {"sources": ["synthetic"], "synthetic": True}, "pre_fix_state": {"repo": "repo", "services": {}},
        "dependencies": {"python": ">=3.14"}, "acceptance_checks": ["answer_is_yes"], "forbidden_outcomes": [],
        "solutions": solutions,
    })
    (graders).mkdir(parents=True)
    (graders / "grader.py").write_text(grader, encoding="utf-8")
    ref = graders / "solutions" / "reference"
    if reference_stream is not None:
        write_json(ref / "stream.json", reference_stream)
    else:
        (ref / "files").mkdir(parents=True)
        (ref / "files" / "answer.txt").write_text(reference + "\n", encoding="utf-8")
    if usage is not None:
        (ref / "usage.jsonl").write_text("".join(json.dumps(e) + "\n" for e in usage), encoding="utf-8")


def run_cli(*args: str, env: dict | None = None, timeout: int = 240) -> tuple[int, dict | None, str]:
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "result.json"
        proc = subprocess.run(
            [sys.executable, "-m", "patina_evals", *args, "--out", str(out), "--log-dir", str(Path(scratch) / "logs")],
            cwd=EVALS, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})},
        )
        result = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return proc.returncode, result, proc.stdout + proc.stderr


def run_synthetic(root: Path, *args: str, env: dict | None = None) -> tuple[int, dict | None, str]:
    return run_cli("run", "--cases", str(root / "cases"), "--graders", str(root / "graders"), *args, env=env)


class Listener:
    """A local socket that accepts and counts connections, standing in for every provider endpoint."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(0.2)
        self.connections = 0
        self.stopped = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def serve(self):
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.connections += 1
            conn.close()

    def close(self):
        self.stopped = True
        self.thread.join()
        self.sock.close()
