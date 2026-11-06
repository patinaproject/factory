"""The common case manifest every loader produces."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_VERSION = 1
SPLITS = ("development", "holdout")
SUITES = ("regression", "capability")
EXPECTATIONS = ("pass", "fail")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Solution:
    name: str
    expect: str
    targets: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Manifest:
    """One case. `case_dir` is candidate-visible; `grader_dir` never enters a trial."""

    id: str
    version: str
    task_text: str
    task_version: str
    area: str
    group: str
    split: str
    suite: str
    provenance: dict
    pre_fix_state: dict
    dependencies: dict
    responses: dict
    acceptance_checks: tuple[str, ...]
    forbidden_outcomes: tuple[str, ...]
    protected_paths: tuple[str, ...]
    solutions: tuple[Solution, ...]
    case_dir: Path
    grader_dir: Path
    services: dict = field(default_factory=dict)

    @property
    def required(self) -> bool:
        return self.suite == "regression"

    @property
    def repo_dir(self) -> Path:
        return self.case_dir / self.pre_fix_state["repo"]


def require(data: dict, key: str, kind: type, where: str):
    value = data.get(key)
    if not isinstance(value, kind) or not value:
        raise ManifestError(f"{where}: {key} must be a non-empty {kind.__name__}")
    return value


def parse(data: dict, case_dir: Path, grader_dir: Path) -> Manifest:
    where = str(case_dir / "manifest.json")
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestError(f"{where}: manifest_version must be {MANIFEST_VERSION}")
    split, suite = require(data, "split", str, where), require(data, "suite", str, where)
    if split not in SPLITS or suite not in SUITES:
        raise ManifestError(f"{where}: split must be one of {SPLITS} and suite one of {SUITES}")
    task = require(data, "task", dict, where)
    solutions = []
    for name, spec in require(data, "solutions", dict, where).items():
        if spec.get("expect") not in EXPECTATIONS:
            raise ManifestError(f"{where}: solution {name} expect must be one of {EXPECTATIONS}")
        if spec["expect"] == "fail" and not spec.get("targets"):
            raise ManifestError(f"{where}: near-miss {name} must name the checks it targets")
        solutions.append(Solution(name, spec["expect"], tuple(spec.get("targets", ())), spec.get("description", "")))
    if not any(s.name == "reference" and s.expect == "pass" for s in solutions):
        raise ManifestError(f"{where}: needs a reference solution that is expected to pass")
    checks = tuple(require(data, "acceptance_checks", list, where))
    forbidden = tuple(data.get("forbidden_outcomes", []))
    known = set(checks) | set(forbidden) | {"grader_tampered"}
    for solution in solutions:
        if unknown := set(solution.targets) - known:
            raise ManifestError(f"{where}: solution {solution.name} targets unknown checks {sorted(unknown)}")
    pre_fix = require(data, "pre_fix_state", dict, where)
    if not (case_dir / pre_fix.get("repo", "")).is_dir():
        raise ManifestError(f"{where}: pre_fix_state.repo must name a folder in the case")
    responses_file = data.get("responses")
    responses = json.loads((case_dir / responses_file).read_text(encoding="utf-8")) if responses_file else {}
    return Manifest(
        id=require(data, "id", str, where), version=require(data, "version", str, where),
        task_text=(case_dir / require(task, "file", str, where)).read_text(encoding="utf-8"),
        task_version=require(task, "version", str, where), area=require(data, "area", str, where),
        group=require(data, "group", str, where), split=split, suite=suite,
        provenance=require(data, "provenance", dict, where), pre_fix_state=pre_fix,
        dependencies=require(data, "dependencies", dict, where), responses=responses,
        acceptance_checks=checks, forbidden_outcomes=forbidden,
        protected_paths=tuple(data.get("protected_paths", [])), solutions=tuple(solutions),
        case_dir=case_dir, grader_dir=grader_dir, services=dict(pre_fix.get("services", {})),
    )
