"""What one request cost. Decisions: DECISIONS.md section L.

Money is an integer number of micro-dollars (1 = $0.000001), never a float: floats can't hold amounts
like $0.0004 exactly, and adding up millions of them drifts away from the real total.

A price of "$X per 1M tokens" is the same as "X micro-dollars per token", so the maths is just
tokens × price, with Decimal so the config's prices stay exact until we round once at the end.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens, as the provider's pricing page lists it."""

    input: Decimal
    output: Decimal
    cached_input: Decimal  # prompt tokens read from the provider's prompt cache
    cache_write: Decimal  # prompt tokens written to the cache (Anthropic charges extra for these)

    @classmethod
    def from_config(cls, raw: dict) -> "Price":
        def d(key, default=None):
            value = raw.get(key, default)
            return Decimal(str(value))  # via str, so 0.15 stays 0.15 and not 0.1499999...
        return cls(
            input=d("input"),
            output=d("output"),
            cached_input=d("cached_input", raw["input"]),  # no discount listed = full price
            cache_write=d("cache_write", raw["input"]),
        )


@dataclass(frozen=True)
class Tokens:
    uncached: int
    cached: int
    cache_write: int
    output: int  # includes hidden reasoning tokens: providers bill them as output


def split_usage(usage: dict) -> Tokens:
    """OpenAI-format usage -> the token kinds that are priced differently."""
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or 0
    write = details.get("cache_creation_tokens") or 0  # only set by our Anthropic adapter
    prompt = usage.get("prompt_tokens", 0)
    return Tokens(uncached=max(0, prompt - cached - write), cached=cached, cache_write=write,
                  output=usage.get("completion_tokens", 0))


def cost_micros(price: Price, usage: Optional[dict]) -> int:
    if not usage:
        return 0
    t = split_usage(usage)
    total = (t.uncached * price.input + t.cached * price.cached_input
             + t.cache_write * price.cache_write + t.output * price.output)
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micros_to_usd(micros: int) -> str:
    return f"{Decimal(micros) / 1_000_000:.6f}"


def usd(amount: float) -> str:
    """For messages: cents for normal amounts, more digits for tiny ones so they don't show as $0.00."""
    return f"${amount:.2f}" if amount >= 0.01 or amount == 0 else f"${amount:.6f}"
