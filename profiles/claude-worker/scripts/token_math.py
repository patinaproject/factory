"""Pure token and price math shared by token-spend and the eval cost ledger."""

from __future__ import annotations

from dataclasses import astuple, dataclass


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0

    def __add__(self, other: Tokens) -> Tokens:
        return Tokens(*(a + b for a, b in zip(astuple(self), astuple(other))))

    def __sub__(self, other: Tokens) -> Tokens:
        return Tokens(*(a - b for a, b in zip(astuple(self), astuple(other))))

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total(self) -> int:
        return sum(astuple(self))

    def as_dict(self) -> dict:
        return {
            "input": self.input, "cache_read": self.cache_read, "cache_write": self.cache_write,
            "output": self.output, "total": self.total,
        }


def usage_tokens(usage: dict) -> Tokens:
    one_hour = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    return Tokens(
        input=usage.get("input_tokens") or 0, cache_read=usage.get("cache_read_input_tokens") or 0,
        cache_write_5m=written - one_hour, cache_write_1h=one_hour, output=usage.get("output_tokens") or 0,
    )


def cost_of(tokens: Tokens, rate: dict) -> float:
    return (
        tokens.input * rate["input"] + tokens.output * rate["output"] + tokens.cache_read * rate["cache_read"]
        + tokens.cache_write_5m * rate["cache_write_5m"] + tokens.cache_write_1h * rate["cache_write_1h"]
    ) / 1e6


def summarize(spends, prices: dict) -> dict:
    models: dict[str, Tokens] = {}
    for spend in spends:
        models[spend.model] = models.get(spend.model, Tokens()) + spend.tokens
    rows, total, cost, unpriced = [], Tokens(), 0.0, []
    for model, tokens in sorted(models.items(), key=lambda item: (-item[1].total, item[0])):
        rate = prices.get(model)
        priced = None if rate is None else cost_of(tokens, rate)
        if priced is None:
            unpriced.append(model)
        else:
            cost += priced
        total += tokens
        rows.append({"model": model, "tokens": tokens.as_dict(), "cost_usd": None if priced is None else round(priced, 6)})
    return {"tokens": total.as_dict(), "cost_usd": round(cost, 6), "unpriced_models": unpriced, "models": rows}
