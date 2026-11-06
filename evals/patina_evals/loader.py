"""Case loaders. Each reads a folder named at run time and returns common manifests.

A later adapter for another eval case format implements `CaseLoader`, registers
itself in `LOADERS`, and is selected with `--loader <name> --cases <path>`. It
maps each external case to a `Manifest`; the harness, graders, and result schema
stay the same.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from patina_evals.manifest import Manifest, parse


class CaseLoader(Protocol):
    def __call__(self, cases: Path, graders: Path) -> list[Manifest]: ...


def manifest_dir(cases: Path, graders: Path) -> list[Manifest]:
    """This repository's format: `<cases>/<id>/manifest.json`, with graders in `<graders>/<id>/`."""
    manifests = []
    for path in sorted(cases.glob("*/manifest.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        manifests.append(parse(data, path.parent, graders / data.get("id", path.parent.name)))
    return manifests


LOADERS: dict[str, Callable[[Path, Path], list[Manifest]]] = {"manifest-dir": manifest_dir}


def register(name: str, loader: CaseLoader) -> None:
    if name in LOADERS:
        raise ValueError(f"loader {name} is already registered")
    LOADERS[name] = loader


def load(name: str, cases: Path, graders: Path) -> list[Manifest]:
    if name not in LOADERS:
        raise ValueError(f"unknown loader {name}; known: {', '.join(sorted(LOADERS))}")
    manifests = LOADERS[name](cases, graders)
    ids = [m.id for m in manifests]
    if len(ids) != len(set(ids)):
        raise ValueError(f"loader {name} returned duplicate case ids")
    return manifests
