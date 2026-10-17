"""Costs from the price catalog (BUD-03, BUD-08; technical design §3.2, §6.2).

Money is integer micro-USD. Each cost is summed exactly and rounded up once, so a worst case
computed from bounds is never below the actual cost of any usage within those bounds.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any, Literal, Self
from uuid import UUID


class Meter(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    CACHE_WRITE_5M = "cache_write_5m"
    CACHE_WRITE_1H = "cache_write_1h"
    CACHE_READ = "cache_read"
    CALL = "call"
    SECOND = "second"
    USE = "use"


class NoCostModelError(LookupError):
    """The catalog has no price for an operation, so it can't run (BUD-03)."""


@dataclass(frozen=True, slots=True)
class Tier:
    floor: int
    unit_size: int
    micro_usd: int


@dataclass(frozen=True, slots=True)
class PriceList:
    """One published price version. ``tiers`` maps (sku, meter) to tiers by ascending floor."""

    version_id: UUID
    tiers: Mapping[tuple[str, Meter], tuple[Tier, ...]]

    def unit_price(self, sku: str, meter: Meter, prompt_tokens: int = 0) -> Fraction:
        """micro-USD per unit, from the highest tier whose floor ``prompt_tokens`` reaches."""
        tiers = self.tiers.get((sku, meter))
        if not tiers:
            raise NoCostModelError(f"no price for {sku} {meter.value}")
        tier = next(t for t in reversed(tiers) if t.floor <= prompt_tokens)
        return Fraction(tier.micro_usd, tier.unit_size)


@dataclass(frozen=True, slots=True)
class Line:
    sku: str
    meter: Meter
    quantity: int


def cost(prices: PriceList, lines: Iterable[Line], prompt_tokens: int = 0) -> int:
    """Total micro-USD for ``lines``, rounded up once. Zero-quantity lines need no price."""
    total = sum(
        (
            prices.unit_price(line.sku, line.meter, prompt_tokens) * line.quantity
            for line in lines
            if line.quantity
        ),
        Fraction(0),
    )
    return math.ceil(total)


CacheTtl = Literal["5m", "1h"]


@dataclass(frozen=True, slots=True)
class ModelCallBounds:
    """What the gateway knows before a model call.

    ``prompt_tokens`` comes from the provider's token-counting endpoint and includes any cached
    span. ``cache_write_tokens`` is the span up to the request's cache breakpoint.
    """

    sku: str
    prompt_tokens: int
    max_output_tokens: int
    cache_write_tokens: int = 0
    cache_ttl: CacheTtl = "5m"

    def __post_init__(self) -> None:
        if min(self.prompt_tokens, self.max_output_tokens, self.cache_write_tokens) < 0:
            raise ValueError("token counts can't be negative")
        if self.cache_write_tokens > self.prompt_tokens:
            raise ValueError("the cached span can't exceed the prompt")

    def worst_case(self, prices: PriceList) -> int:
        """Every prompt token at full input price, the cached span at its write price, and
        ``max_output_tokens`` of output (thinking counts as output). Cache reads only lower it.
        """
        write_meter = Meter.CACHE_WRITE_5M if self.cache_ttl == "5m" else Meter.CACHE_WRITE_1H
        return cost(
            prices,
            [
                Line(self.sku, Meter.INPUT, self.prompt_tokens - self.cache_write_tokens),
                Line(self.sku, write_meter, self.cache_write_tokens),
                Line(self.sku, Meter.OUTPUT, self.max_output_tokens),
            ],
            prompt_tokens=self.prompt_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """What a model call actually used, as the provider reports it."""

    input_tokens: int
    output_tokens: int
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
        )

    @classmethod
    def from_anthropic(cls, usage: Mapping[str, Any]) -> Self:
        """Read a Messages API ``usage`` object.

        ``input_tokens`` there is only the uncached remainder. ``cache_creation`` splits writes
        by TTL; without it, every write is priced as a 5-minute write.
        """
        writes = int(usage.get("cache_creation_input_tokens") or 0)
        split: Mapping[str, Any] = usage.get("cache_creation") or {}
        write_1h = int(split.get("ephemeral_1h_input_tokens") or 0)
        if "ephemeral_5m_input_tokens" in split:
            write_5m = int(split["ephemeral_5m_input_tokens"] or 0)
        else:
            write_5m = writes - write_1h
        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_write_5m_tokens=write_5m,
            cache_write_1h_tokens=write_1h,
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        )

    def actual(self, prices: PriceList, sku: str) -> int:
        return cost(
            prices,
            [
                Line(sku, Meter.INPUT, self.input_tokens),
                Line(sku, Meter.CACHE_WRITE_5M, self.cache_write_5m_tokens),
                Line(sku, Meter.CACHE_WRITE_1H, self.cache_write_1h_tokens),
                Line(sku, Meter.CACHE_READ, self.cache_read_tokens),
                Line(sku, Meter.OUTPUT, self.output_tokens),
            ],
            prompt_tokens=self.prompt_tokens,
        )
