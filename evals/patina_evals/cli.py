"""The one local and CI entrypoint: `python3 -m patina_evals run|parity|validate`."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from patina_evals import adapter, isolation, ledger, loader, parity, schema, task
from patina_evals.speed import Recorder, build_speed_report

EVALS = Path(__file__).resolve().parents[1]
MODEL = "mockllm/model"


def revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=EVALS, check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run_trials(manifests, limits: isolation.Limits, recorder: Recorder, log_dir: Path) -> tuple[dict, int, str | None]:
    """Run every case solution as an Inspect sample. Returns scores by sample id, model calls, and the log path."""
    if not manifests:
        return {}, 0, None
    from inspect_ai import eval as inspect_eval

    task.CONTEXT = task.Context({m.id: m for m in manifests}, limits, recorder, recorder.now())
    logs = inspect_eval(
        task.build_task(manifests, limits), model=MODEL, max_samples=limits.concurrency, log_dir=str(log_dir),
        display="none", fail_on_error=False, sandbox_cleanup=True,
    )
    log = logs[0]
    scores, calls = {}, 0
    for sample in log.samples or []:
        calls += sum(1 for event in sample.events if getattr(event, "event", None) == "model")
        score = next(iter((sample.scores or {}).values()), None)
        scores[str(sample.id)] = score.metadata if score is not None else None
    return scores, calls, log.location


def recorded_events(manifests, scores: dict) -> list[dict]:
    """Usage a solution recorded in `usage.jsonl`, tagged with its trial. Scripted smoke solutions record none."""
    events = []
    for m in manifests:
        for s in m.solutions:
            path = m.grader_dir / "solutions" / s.name / "usage.jsonl"
            meta = scores.get(f"{m.id}:{s.name}")
            if path.is_file() and meta:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        events.append(json.loads(line) | {"trial": meta["trial"], "card": m.id})
    return events


def case_result(m, scores: dict) -> dict:
    trials = []
    for s in m.solutions:
        meta = scores.get(f"{m.id}:{s.name}") or {
            "trial": None, "verdict": None, "outcome": "missing", "checks": {}, "forbidden": {}, "error": "no score recorded",
            "leaked_process_groups": [],
        }
        trials.append({
            "trial": meta["trial"], "solution": s.name, "expect": s.expect, "targets": list(s.targets),
            "verdict": meta["verdict"], "outcome": meta["outcome"], "checks": meta["checks"], "forbidden": meta["forbidden"],
            "error": meta["error"], "leaked_process_groups": meta["leaked_process_groups"],
        })
    outcomes = {t["outcome"] for t in trials}
    status = "incomplete" if outcomes & {"error", "missing"} else "pass" if outcomes == {"ok"} else "fail"
    return {
        "id": m.id, "version": m.version, "task_version": m.task_version, "area": m.area, "group": m.group,
        "split": m.split, "suite": m.suite, "required": m.required, "status": status, "trials": trials,
    }


def summarize(cases: list[dict], parity_report: dict, cost: dict, cost_paths: list[str], model_calls: int) -> dict:
    required_failed = [c["id"] for c in cases if c["required"] and c["status"] == "fail"]
    capability_failed = [c["id"] for c in cases if not c["required"] and c["status"] != "pass"]
    incomplete = [c["id"] for c in cases if c["required"] and c["status"] == "incomplete"]
    diagnostics = [f"capability case {c} did not pass; capability cases never fail the run" for c in capability_failed]
    if model_calls:
        required_failed.append("model_call_made")
    assertions = []
    for path in cost_paths:
        try:
            assertions.append({"path": path, "status": "known", "value": ledger.require_known_cost(cost, path)})
        except ledger.CostUnknown as error:
            assertions.append({"path": path, "status": "unknown", "value": None, "reason": str(error)})
    if not parity_report["valid"]:
        reason = "parity_invalid"
    elif incomplete:
        reason = "incomplete"
    elif required_failed:
        reason = "required_failure"
    elif any(a["status"] == "unknown" for a in assertions):
        reason = "cost_unknown"
    else:
        reason = "ok"
    return {
        "cases": len(cases), "trials": sum(len(c["trials"]) for c in cases), "required_failed": required_failed,
        "capability_failed": capability_failed, "incomplete": incomplete, "cost_assertions": assertions,
        "diagnostics": diagnostics, "exit_code": schema.EXIT_CODES[reason], "exit_reason": reason,
    }


def run(args: argparse.Namespace) -> int:
    started = datetime.now(timezone.utc).isoformat()
    limits = isolation.Limits(
        cpu_seconds=args.cpu_seconds, memory_bytes=args.memory_bytes, command_timeout_seconds=args.command_timeout,
        trial_timeout_seconds=args.trial_timeout, concurrency=args.concurrency,
    )
    manifests = [
        m for m in loader.load(args.loader, Path(args.cases), Path(args.graders))
        if m.split == args.split and (args.suite == "all" or m.suite == args.suite)
    ]
    parity_report = parity.check(adapter.REPO_ROOT, Path(args.differences))
    recorder = Recorder()
    log_dir = Path(args.log_dir) if args.log_dir else Path(tempfile.mkdtemp(prefix="patina-evals-logs-"))
    with recorder.span("run"):
        scores, model_calls, log_path = run_trials(manifests, limits, recorder, log_dir)
    cases = [case_result(m, scores) for m in manifests]
    cards = {
        c["id"]: {"success": c["status"] == "pass", "trials": [t["trial"] for t in c["trials"] if t["trial"]]}
        for c in cases
    }
    cost = ledger.build_cost_report(recorded_events(manifests, scores), None, cards)
    result = {
        "schema_version": schema.SCHEMA_VERSION,
        "kind": "patina-eval-result",
        "evidence": {"type": "plumbing_validation", "agent_reliability_evidence": False, "note": schema.PLUMBING_NOTE},
        "run": {
            "split": args.split, "suite": args.suite, "loader": args.loader, "harness_revision": revision(),
            "started_at": started, "inspect_ai_version": importlib.metadata.version("inspect_ai"), "model": MODEL,
            "model_calls": model_calls, "solver": "scripted", "inspect_log": log_path,
        },
        "resources": limits.record(),
        "parity": parity_report,
        "cases": cases,
        "summary": summarize(cases, parity_report, cost, args.require_cost, model_calls),
        "cost": cost,
        "speed": build_speed_report(recorder.spans(), recorder.counts()),
    }
    schema.validate(result)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print(report(result))
    return result["summary"]["exit_code"]


def report(result: dict) -> str:
    s = result["summary"]
    lines = [
        "PLUMBING VALIDATION ONLY: " + result["evidence"]["note"],
        f"split={result['run']['split']} suite={result['run']['suite']} cases={s['cases']} trials={s['trials']} "
        f"model={result['run']['model']} model_calls={result['run']['model_calls']}",
        f"parity: {'valid' if result['parity']['valid'] else 'INVALID'}",
    ]
    for case in result["cases"]:
        lines.append(f"  {case['status']:<10} {case['id']} ({case['suite']}, {'required' if case['required'] else 'nonblocking'})")
        for t in case["trials"]:
            lines.append(f"      {t['outcome']:<10} {t['solution']} expect={t['expect']} verdict={t['verdict']}")
    cost = result["cost"]["experiment_total"]["cost_usd"]
    lines.append(f"cost: {'unknown' if cost['value'] is None else cost['value']} flags={','.join(cost['flags']) or '-'}")
    wall = result["speed"]["end_to_end_wall_ns"]
    lines.append(f"wall: {None if wall is None else round(wall / 1e9, 3)} s")
    lines += [f"diagnostic: {d}" for d in s["diagnostics"]]
    lines.append(f"exit {s['exit_code']} ({s['exit_reason']})")
    return "\n".join(lines)


def parity_command(args: argparse.Namespace) -> int:
    result = parity.check(adapter.REPO_ROOT, Path(args.differences))
    text = json.dumps(result, indent=2, default=repr)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["valid"] else schema.EXIT_CODES["parity_invalid"]


def validate_command(args: argparse.Namespace) -> int:
    schema.validate(json.loads(Path(args.result).read_text(encoding="utf-8")))
    print(f"{args.result} is a valid schema version {schema.SCHEMA_VERSION} result")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patina_evals", description="Model-free patina-mode eval harness.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run the selected cases and write a result file")
    run_parser.add_argument("--split", choices=("development", "holdout"), required=True)
    run_parser.add_argument("--suite", choices=("regression", "capability", "all"), required=True)
    run_parser.add_argument("--out", required=True)
    run_parser.add_argument("--loader", default="manifest-dir")
    run_parser.add_argument("--cases", default=str(EVALS / "fixtures"))
    run_parser.add_argument("--graders", default=str(EVALS / "graders"))
    run_parser.add_argument("--differences", default=str(parity.DIFFERENCES))
    run_parser.add_argument("--log-dir")
    run_parser.add_argument("--concurrency", type=int, default=2)
    run_parser.add_argument("--cpu-seconds", type=int, default=60)
    run_parser.add_argument("--memory-bytes", type=int, default=2 * 1024**3)
    run_parser.add_argument("--command-timeout", type=int, default=120)
    run_parser.add_argument("--trial-timeout", type=int, default=600)
    run_parser.add_argument("--require-cost", action="append", default=[], metavar="PATH",
                            help="fail with exit 5 unless the cost at this report path is known, such as experiment_total.cost_usd")
    run_parser.set_defaults(handler=run)
    parity_parser = commands.add_parser("parity", help="compare the trial adapter with the real worker construction")
    parity_parser.add_argument("--differences", default=str(parity.DIFFERENCES))
    parity_parser.add_argument("--out")
    parity_parser.set_defaults(handler=parity_command)
    validate_parser = commands.add_parser("validate", help="validate a result file against the schema")
    validate_parser.add_argument("result")
    validate_parser.set_defaults(handler=validate_command)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
