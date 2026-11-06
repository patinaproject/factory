"""The Inspect task: one sample per case solution, a scripted solver, and a grading scorer.

No model is called. The solver applies a scripted solution (a file overlay or a
recorded tool stream) from the case's grader folder; it never calls `generate`.
A later real-agent solver replaces `scripted_solver` and keeps the scorer.
"""

from __future__ import annotations

import json
import shutil
import traceback
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import sandbox

from patina_evals import isolation
from patina_evals.manifest import Manifest, Solution
from patina_evals.speed import Recorder

TAMPERED = "grader_tampered"


@dataclass
class Context:
    """Run state the solver and scorer share. Inspect records solver arguments, so this stays module-level."""

    manifests: dict[str, Manifest]
    limits: isolation.Limits
    recorder: Recorder
    run_start_ns: int
    trials: dict[str, dict] = field(default_factory=dict)


CONTEXT: Context | None = None


def solution_of(manifest: Manifest, name: str) -> Solution:
    return next(s for s in manifest.solutions if s.name == name)


def samples(manifests: list[Manifest]) -> list[Sample]:
    return [
        Sample(id=f"{m.id}:{s.name}", input=m.task_text, metadata={"case": m.id, "solution": s.name})
        for m in manifests for s in m.solutions
    ]


async def run_in_trial(trial: isolation.Trial, argv: list[str], actor: str = "agent", stdin: str | None = None) -> dict:
    limits = CONTEXT.limits
    result = await sandbox().exec(
        isolation.command(trial, argv, limits, actor), input=stdin, cwd=str(trial.repo),
        timeout=limits.command_timeout_seconds,
    )
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


async def apply_solution(trial: isolation.Trial, manifest: Manifest, solution: Solution) -> dict:
    source = manifest.grader_dir / "solutions" / solution.name
    calls = turns = 0
    steps = []
    if (source / "files").is_dir():
        shutil.copytree(source / "files", trial.repo, dirs_exist_ok=True)
        turns += 1
    if (source / "stream.json").is_file():
        for step in json.loads((source / "stream.json").read_text(encoding="utf-8")):
            turns += 1
            if "human" in step:
                reply = manifest.responses["human"][step["human"]]
                argv = isolation.tool_argv(trial, "tracker", ["comment", reply["issue"], "--body", reply["body"]])
                steps.append({"human": step["human"], **await run_in_trial(trial, argv, actor="person")})
            else:
                calls += 1
                argv = isolation.tool_argv(trial, step["tool"], step["args"])
                steps.append({"tool": step["tool"], **await run_in_trial(trial, argv, stdin=step.get("stdin"))})
    return {"calls": calls, "turns": turns, "steps": steps}


@solver
def scripted_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        ctx = CONTEXT
        manifest = ctx.manifests[state.metadata["case"]]
        solution = solution_of(manifest, state.metadata["solution"])
        trial_id = f"{manifest.id}--{solution.name}--{uuid.uuid4().hex[:8]}"
        start = ctx.recorder.now()
        ctx.recorder.add("wait", ctx.run_start_ns, start, trial=trial_id)
        info = {"trial": trial_id, "start_ns": start, "lock": None}
        ctx.trials[trial_id] = info
        state.store.set("trial", trial_id)
        try:
            with ctx.recorder.span("setup", trial=trial_id):
                root = Path((await sandbox().exec(["/bin/pwd"])).stdout.strip()) / "trial"
                trial = isolation.prepare(root, manifest, trial_id)
                info["root"] = str(root)
                info["lock"] = isolation.acquire_writer(trial, trial_id)
                info["protected"] = isolation.fingerprint(trial.repo, manifest.protected_paths)
                info["graders"] = isolation.tree_fingerprint(manifest.grader_dir)
            with ctx.recorder.span("solve", trial=trial_id):
                applied = await apply_solution(trial, manifest, solution)
            ctx.recorder.count(trial_id, calls=applied["calls"], turns=applied["turns"])
            info["steps"] = applied["steps"]
        except Exception:
            info["error"] = traceback.format_exc(limit=5)
        return state

    return solve


def load_grader(manifest: Manifest):
    """Compile the grader from source so loading it writes no bytecode into the grader folder it fingerprints."""
    path = manifest.grader_dir / "grader.py"
    module = types.ModuleType(f"grader_{manifest.id.replace('-', '_')}")
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


def verdict_of(manifest: Manifest, graded: dict, tampered: bool) -> tuple[dict, dict, bool]:
    checks, forbidden = dict(graded["checks"]), dict(graded.get("forbidden", {}))
    if set(checks) != set(manifest.acceptance_checks) or set(forbidden) != set(manifest.forbidden_outcomes):
        raise ValueError(f"grader for {manifest.id} returned checks that differ from its manifest")
    forbidden[TAMPERED] = tampered
    passed = all(checks.values()) and not any(forbidden.values())
    return checks, forbidden, passed


def outcome(solution: Solution, checks: dict, forbidden: dict, passed: bool) -> str:
    if solution.expect == "pass":
        return "ok" if passed else "unexpected"
    hit = any(checks.get(t) is False or forbidden.get(t) is True for t in solution.targets)
    return "ok" if not passed and hit else "unexpected"


async def grade_trial(manifest: Manifest, solution: Solution, info: dict) -> dict:
    trial = isolation.Trial(info["trial"], Path(info["root"]))
    grader = load_grader(manifest)
    probes = {}
    for name, probe in grader.PROBES.items():
        probes[name] = await run_in_trial(trial, probe["argv"], stdin=probe.get("stdin"))
    observed = {"probes": probes, "tracker": json.loads(trial.tracker_state.read_text(encoding="utf-8"))}
    tampered = (
        isolation.fingerprint(trial.repo, manifest.protected_paths) != info["protected"]
        or isolation.tree_fingerprint(manifest.grader_dir) != info["graders"]
    )
    checks, forbidden, passed = verdict_of(manifest, grader.grade(observed), tampered)
    return {
        "verdict": "pass" if passed else "fail", "checks": checks, "forbidden": forbidden,
        "outcome": outcome(solution, checks, forbidden, passed),
    }


@scorer(metrics=[mean()])
def grading_scorer():
    async def score(state: TaskState, target: Target) -> Score:
        ctx = CONTEXT
        manifest = ctx.manifests[state.metadata["case"]]
        solution = solution_of(manifest, state.metadata["solution"])
        info = ctx.trials[state.store.get("trial")]
        result = {"verdict": None, "checks": {}, "forbidden": {}, "outcome": "error"}
        try:
            if "error" not in info:
                with ctx.recorder.span("grading", trial=info["trial"]):
                    result = await grade_trial(manifest, solution, info)
        except Exception:
            info["error"] = traceback.format_exc(limit=5)
        finally:
            if "root" in info:
                info["leaked_process_groups"] = isolation.teardown(isolation.Trial(info["trial"], Path(info["root"])))
            if info.get("lock"):
                isolation.release_writer(info["lock"])
            ctx.recorder.add("trial", info["start_ns"], ctx.recorder.now(), trial=info["trial"])
        if info.get("error"):
            result["outcome"] = "error"
        result |= {
            "trial": info["trial"], "error": info.get("error"),
            "leaked_process_groups": info.get("leaked_process_groups", []),
        }
        return Score(value=1.0 if result["outcome"] == "ok" else 0.0, explanation=result["outcome"], metadata=result)

    return score


def build_task(manifests: list[Manifest], limits: isolation.Limits) -> Task:
    return Task(
        dataset=samples(manifests), solver=scripted_solver(), scorer=grading_scorer(), sandbox="local",
        time_limit=limits.trial_timeout_seconds, name="patina_smoke",
    )
