"""Eval cost ledger: wraps the token-spend math and keeps unknown cost unknown.

Event fields (one dict per recorded or synthetic stream record):

  scope             "worker" | "grader" | "other"            required
  agent             "main" | "subagent" | "auxiliary"        required
  trial             trial id                                  required
  attempt           attempt number, default 1
  card              card id; defaults to the card whose "trials" list holds the trial
  model             model name; absent means the model is unknown
  usage             Claude usage dict; absent or lacking input/output tokens is missing usage
  message_id        API message id; request_id is the fallback. Repeats of one id within a
                    scope, trial, and attempt are one message and the last record wins
  cumulative        true when usage is the running total of session_id (a resume result);
                    the ledger counts the delta from the previous total of that session
  session_id        required when cumulative
  parent_inclusive  true when usage already includes the usage of subagent events that name
                    this event's message_id as parent_message_id
  parent_message_id on a subagent event, the message_id of its parent
  retry             true when the record is a retried request
  interrupted       true when the run stopped before usage was final
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "profiles" / "claude-worker" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from token_math import Tokens, cost_of, usage_tokens  # noqa: E402

SCHEMA_VERSION = 1
SCOPES = ("worker", "grader", "other")
AGENTS = ("main", "subagent", "auxiliary")
CATEGORIES = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "cache_write_unknown_ttl", "output")
UNKNOWN_MODEL = "unknown"
DEFAULT_PRICES = SCRIPTS / "token_prices.json"
TOKEN_INCOMPLETE = frozenset({
    "missing_usage", "interrupted", "cumulative_decreased", "parent_inclusive_inconsistent",
    "parent_inclusive_unresolved", "cache_write_breakdown_inconsistent",
})


class CostUnknown(Exception):
    pass


def load_prices(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or DEFAULT_PRICES).read_text(encoding="utf-8"))


def zero() -> dict:
    return dict.fromkeys(CATEGORIES, 0)


def plus(a: dict, b: dict) -> dict:
    return {c: a[c] + b[c] for c in CATEGORIES}


def parse_usage(usage: object) -> tuple[dict | None, list[str]]:
    if not isinstance(usage, dict) or usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return None, ["missing_usage"]
    flags: list[str] = []
    written = usage.get("cache_creation_input_tokens") or 0
    breakdown = usage.get("cache_creation")
    five = one = unknown = 0
    if isinstance(breakdown, dict) and ("ephemeral_5m_input_tokens" in breakdown or "ephemeral_1h_input_tokens" in breakdown):
        five = breakdown.get("ephemeral_5m_input_tokens") or 0
        one = breakdown.get("ephemeral_1h_input_tokens") or 0
        if written and written != five + one:
            unknown = written - five - one
            if unknown < 0:
                flags.append("cache_write_breakdown_inconsistent")
                unknown = 0
    elif written:
        unknown = written
    tokens = usage_tokens({
        "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": five + one, "cache_creation": {"ephemeral_1h_input_tokens": one},
    })
    return {
        "input": tokens.input, "cache_read": tokens.cache_read, "cache_write_5m": tokens.cache_write_5m,
        "cache_write_1h": tokens.cache_write_1h, "cache_write_unknown_ttl": unknown, "output": tokens.output,
    }, flags


def subtract(whole: dict, part: dict) -> tuple[dict, bool]:
    """Whole minus part. Cache writes collapse to unknown TTL when either side has an unknown split."""
    writes = ("cache_write_5m", "cache_write_1h", "cache_write_unknown_ttl")
    result = {c: whole[c] - part[c] for c in CATEGORIES}
    if whole["cache_write_unknown_ttl"] or part["cache_write_unknown_ttl"]:
        result["cache_write_5m"] = result["cache_write_1h"] = 0
        result["cache_write_unknown_ttl"] = sum(whole[c] for c in writes) - sum(part[c] for c in writes)
    negative = any(v < 0 for v in result.values())
    return {c: max(0, v) for c, v in result.items()}, negative


class Record:
    __slots__ = ("scope", "trial", "card", "attempt", "agent", "model", "cats", "retry", "flags", "message_id", "parent_id", "inclusive")

    def __init__(self, event: dict, cats: dict | None, flags: list[str]):
        self.scope = event["scope"]
        self.trial = event["trial"]
        self.card = event.get("card")
        self.attempt = event.get("attempt", 1)
        self.agent = event["agent"]
        self.model = event.get("model")
        self.cats = cats
        self.retry = bool(event.get("retry"))
        self.message_id = event.get("message_id") or event.get("request_id")
        self.parent_id = event.get("parent_message_id")
        self.inclusive = bool(event.get("parent_inclusive"))
        self.flags = list(flags)
        if event.get("interrupted"):
            self.flags.append("interrupted")


def validate(event: dict) -> None:
    for field, allowed in (("scope", SCOPES), ("agent", AGENTS)):
        if event.get(field) not in allowed:
            raise ValueError(f"event {field} must be one of {allowed}, got {event.get(field)!r}")
    if not event.get("trial"):
        raise ValueError("event needs a trial id")
    if event.get("cumulative") and not event.get("session_id"):
        raise ValueError("a cumulative event needs a session_id")


def resolve(events: list[dict]) -> list[Record]:
    messages: dict[tuple, int] = {}
    ordered: list[dict] = []
    for event in events:
        validate(event)
        key = event.get("message_id") or event.get("request_id")
        if key and not event.get("cumulative"):
            slot = (event["scope"], event["trial"], event.get("attempt", 1), key)
            if slot in messages:
                ordered[messages[slot]] = event
                continue
            messages[slot] = len(ordered)
        ordered.append(event)

    totals: dict[tuple, dict] = {}
    records = []
    for event in ordered:
        cats, flags = parse_usage(event.get("usage"))
        if event.get("cumulative") and cats is not None:
            slot = (event["scope"], event["trial"], event["session_id"], event.get("model"))
            previous = totals.get(slot, zero())
            totals[slot] = cats
            cats, negative = subtract(cats, previous)
            if negative:
                flags.append("cumulative_decreased")
        records.append(Record(event, cats, flags))

    children: dict[tuple, list[Record]] = {}
    for record in records:
        if record.parent_id:
            children.setdefault((record.scope, record.trial, record.attempt, record.parent_id), []).append(record)
    for record in records:
        if not record.inclusive or record.cats is None:
            continue
        for child in children.get((record.scope, record.trial, record.attempt, record.message_id), []):
            if child.cats is None:
                record.flags.append("parent_inclusive_unresolved")
                break
            record.cats, negative = subtract(record.cats, child.cats)
            if negative:
                record.flags.append("parent_inclusive_inconsistent")
    return records


def known(value: float | dict | None, flags: list[str], lower_bound: float | dict | None = None) -> dict:
    out = {"value": value, "complete": value is not None, "flags": sorted(set(flags))}
    if value is None:
        out["known_lower_bound"] = lower_bound
    return out


def model_row(model: str, records: list[Record], prices: dict) -> dict:
    cats, flags = zero(), set()
    for record in records:
        flags.update(record.flags)
        if record.cats is not None:
            cats = plus(cats, record.cats)
    token_incomplete = bool(flags & TOKEN_INCOMPLETE)
    tokens = known(None, list(flags), cats) if token_incomplete else known(cats, list(flags))
    rate = prices.get(model)
    priced = Tokens(cats["input"], cats["cache_read"], cats["cache_write_5m"], cats["cache_write_1h"], cats["output"])
    lower = None if rate is None else round(cost_of(priced, rate), 6)
    cost_flags = set(flags)
    if rate is None:
        cost_flags.add(f"unpriced_model:{model}")
    if cats["cache_write_unknown_ttl"]:
        cost_flags.add("cache_write_unknown_ttl")
    if token_incomplete or rate is None or cats["cache_write_unknown_ttl"]:
        cost = known(None, list(cost_flags), lower)
    else:
        cost = known(lower, list(cost_flags))
    return {"model": model, "events": len(records), "tokens": tokens, "cost_usd": cost}


def aggregate(records: list[Record], prices: dict, missing: list[str] = ()) -> dict:
    grouped: dict[str, list[Record]] = {}
    for record in records:
        grouped.setdefault(record.model or UNKNOWN_MODEL, []).append(record)
    rows = [model_row(model, rs, prices) for model, rs in sorted(grouped.items())]
    token_flags = sorted({f for row in rows for f in row["tokens"]["flags"]})
    cost_flags = sorted({f for row in rows for f in row["cost_usd"]["flags"]})
    totals = zero()
    for row in rows:
        totals = plus(totals, row["tokens"]["value"] or row["tokens"]["known_lower_bound"])
    token_flags += missing
    cost_flags += missing
    tokens = known(totals if not missing and all(r["tokens"]["complete"] for r in rows) else None, token_flags, totals)
    lower = round(
        sum(r["cost_usd"]["value"] if r["cost_usd"]["complete"] else (r["cost_usd"]["known_lower_bound"] or 0.0) for r in rows), 6
    )
    cost = known(lower if not missing and all(r["cost_usd"]["complete"] for r in rows) else None, cost_flags, lower)
    return {"events": len(records), "models": rows, "tokens": tokens, "cost_usd": cost}


def without_models(report: dict) -> dict:
    return {k: v for k, v in report.items() if k != "models"}


def scope_report(records: list[Record], prices: dict, missing: list[str] = ()) -> dict:
    report = aggregate(records, prices, missing)
    report["attribution"] = {
        agent: without_models(aggregate([r for r in records if r.agent == agent], prices)) for agent in AGENTS
    }
    report["retries"] = without_models(aggregate([r for r in records if r.retry], prices))
    report["attempts"] = len({(r.trial, r.attempt) for r in records})
    return report


def build_cost_report(events, prices: dict | None, cards: dict[str, dict]) -> dict:
    prices = load_prices() if prices is None else prices
    trial_card = {trial: card for card, info in cards.items() for trial in info.get("trials", [])}
    records = resolve(list(events))
    flags: list[str] = []
    for record in records:
        record.card = record.card or trial_card.get(record.trial)
        if record.scope == "worker" and record.card is None:
            flags.append(f"worker_event_without_card:{record.trial}")
    worker_trials = {r.trial for r in records if r.scope == "worker"}
    for card, info in sorted(cards.items()):
        flags += [f"trial_without_events:{t}" for t in info.get("trials", []) if t not in worker_trials]
    if not records:
        flags.append("no_events")

    lacking = [f for f in flags if f.startswith("trial_without_events")]
    scopes = {
        scope: scope_report([r for r in records if r.scope == scope], prices, lacking if scope == "worker" else [])
        for scope in SCOPES
    }
    total = without_models(aggregate(records, prices, [f for f in flags if f == "no_events"]))
    worker = [r for r in records if r.scope == "worker"]
    per_card = {}
    for card, info in sorted(cards.items()):
        mine = aggregate([r for r in worker if r.card == card], prices)
        per_card[card] = {
            "success": bool(info.get("success")), "trials": list(info.get("trials", [])),
            "attempts": len({(r.trial, r.attempt) for r in worker if r.card == card}),
            "tokens": mine["tokens"], "cost_usd": mine["cost_usd"],
        }
    wins = [card for card, info in cards.items() if info.get("success")]
    worker_cost = scopes["worker"]["cost_usd"]
    per_win: dict = {"successful_cards": len(wins), "worker_cost_usd": worker_cost}
    if not wins:
        per_win |= {"status": "no_success", "value": None, "complete": False, "flags": ["no_success"]}
    elif worker_cost["value"] is None:
        per_win |= {"status": "unknown", "value": None, "complete": False, "flags": worker_cost["flags"] or ["worker_accounting_incomplete"]}
    else:
        per_win |= {"status": "known", "value": round(worker_cost["value"] / len(wins), 6), "complete": True, "flags": []}

    complete = total["cost_usd"]["complete"] and total["tokens"]["complete"] and not flags
    return {
        "schema_version": SCHEMA_VERSION, "complete": complete,
        "flags": sorted(set(flags) | set(total["cost_usd"]["flags"])),
        "scopes": scopes, "experiment_total": total, "cards": per_card,
        "worker_cost_per_successful_card": per_win,
    }


def require_known_cost(report: dict, path: str) -> float:
    node: object = report
    for part in path.split("."):
        try:
            node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise CostUnknown(f"no accounting at {path}") from None
    if not isinstance(node, dict) or "value" not in node:
        raise CostUnknown(f"{path} is not a cost value")
    if node["value"] is None or not node.get("complete") or node.get("status", "known") != "known":
        raise CostUnknown(f"cost at {path} is unknown: {', '.join(node.get('flags') or ['no flags'])}")
    return node["value"]
