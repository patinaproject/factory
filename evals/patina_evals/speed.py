"""Eval speed report built from monotonic spans.

A span is {"id", "kind", "trial", "parent", "start_ns", "end_ns"}. A count record is
{"trial", "calls", "turns", "retries", "retry_delay_ns"}.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable
from contextlib import contextmanager

KINDS = ("run", "trial", "setup", "solve", "grading", "wait")
PARTS = ("setup", "solve", "grading", "wait")


class Recorder:
    def __init__(self, clock: Callable[[], int] = time.monotonic_ns):
        self._clock = clock
        self._spans: list[dict] = []
        self._counts: list[dict] = []
        self._next = 0

    @contextmanager
    def span(self, kind: str, trial: str | None = None, parent: str | None = None):
        if kind not in KINDS:
            raise ValueError(f"span kind must be one of {KINDS}, got {kind!r}")
        self._next += 1
        span_id = f"s{self._next}"
        start = self._clock()
        try:
            yield span_id
        finally:
            self._spans.append(
                {"id": span_id, "kind": kind, "trial": trial, "parent": parent, "start_ns": start, "end_ns": self._clock()}
            )

    def add(self, kind: str, start_ns: int, end_ns: int, trial: str | None = None, parent: str | None = None) -> str:
        """Record a span whose start and end happen in different calls, such as a trial's solver and scorer."""
        if kind not in KINDS:
            raise ValueError(f"span kind must be one of {KINDS}, got {kind!r}")
        self._next += 1
        span_id = f"s{self._next}"
        self._spans.append({"id": span_id, "kind": kind, "trial": trial, "parent": parent, "start_ns": start_ns, "end_ns": end_ns})
        return span_id

    def now(self) -> int:
        return self._clock()

    def count(self, trial: str, calls: int = 0, turns: int = 0, retries: int = 0, retry_delay_ns: int = 0) -> None:
        self._counts.append(
            {"trial": trial, "calls": calls, "turns": turns, "retries": retries, "retry_delay_ns": retry_delay_ns}
        )

    def spans(self) -> list[dict]:
        return [dict(s) for s in self._spans]

    def counts(self) -> list[dict]:
        return [dict(c) for c in self._counts]


def union_ns(spans: list[dict]) -> int:
    total, edge = 0, None
    for span in sorted(spans, key=lambda s: s["start_ns"]):
        start, end = span["start_ns"], span["end_ns"]
        if edge is None or start > edge:
            total += end - start
            edge = end
        elif end > edge:
            total += end - edge
            edge = end
    return total


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def build_speed_report(spans: list[dict], counts: list[dict]) -> dict:
    for span in spans:
        if span.get("end_ns") is None or span["end_ns"] < span["start_ns"]:
            raise ValueError(f"span {span.get('id')} has no valid end")
    by_kind = {kind: [s for s in spans if s["kind"] == kind] for kind in KINDS}
    runs, trials = by_kind["run"], by_kind["trial"]
    flags = []
    if runs:
        wall = max(s["end_ns"] for s in runs) - min(s["start_ns"] for s in runs)
        wall_source = "run"
    elif trials:
        wall, wall_source = union_ns(trials), "trial_union"
    else:
        wall, wall_source = None, None
        flags.append("no_run_or_trial_span")
    durations = [s["end_ns"] - s["start_ns"] for s in trials]
    return {
        "schema_version": 1,
        "clock": "monotonic",
        "end_to_end_wall_ns": wall,
        "wall_source": wall_source,
        "parts": {
            kind: {
                "spans": len(by_kind[kind]),
                "sum_ns": sum(s["end_ns"] - s["start_ns"] for s in by_kind[kind]),
                "union_ns": union_ns(by_kind[kind]),
            }
            for kind in PARTS
        },
        "trials": {
            "count": len(trials),
            "median_ns": statistics.median(durations) if durations else None,
            "p95_ns": percentile(durations, 0.95),
        },
        "calls": sum(c.get("calls", 0) for c in counts),
        "turns": sum(c.get("turns", 0) for c in counts),
        "retries": sum(c.get("retries", 0) for c in counts),
        "retry_delay_ns": sum(c.get("retry_delay_ns", 0) for c in counts),
        "flags": flags,
    }
