"""BUD-03 cost model: worst cases bound actual costs; prices come only from the catalog."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from collicity_ledger.catalog import CatalogError, catalog_rows
from collicity_ledger.pricing import (
    CacheTtl,
    Line,
    Meter,
    ModelCallBounds,
    ModelUsage,
    NoCostModelError,
    PriceList,
    Tier,
    cost,
)

CATALOG = Path(__file__).parents[4] / "catalog" / "prices" / "2026-10-15.json"
SONNET = "anthropic/claude-sonnet-5-5"
HAIKU_55 = "anthropic/claude-haiku-5-5"


def _price_list() -> PriceList:
    tiers: dict[tuple[str, Meter], list[Tier]] = {}
    for row in sorted(
        catalog_rows(json.loads(CATALOG.read_text(encoding="utf-8"))),
        key=lambda r: r["tier_floor"],
    ):
        tiers.setdefault((row["sku"], Meter(row["meter"])), []).append(
            Tier(row["tier_floor"], row["unit_size"], row["micro_usd"])
        )
    return PriceList(uuid4(), {k: tuple(v) for k, v in tiers.items()})


PRICES = _price_list()
MODELS = sorted({sku for sku, _ in PRICES.tiers})
TTLS: tuple[CacheTtl, ...] = ("5m", "1h")


def test_the_technical_designs_worked_example():
    # §6.2: Sonnet 5.5, 10,000 input tokens of which 8,000 are written to a 5-minute cache,
    # max_tokens 2,000: $0.020 input + $0.004 cache-write premium + $0.020 output = $0.044.
    bounds = ModelCallBounds(
        SONNET, prompt_tokens=10_000, cache_write_tokens=8_000, max_output_tokens=2_000
    )
    assert bounds.worst_case(PRICES) == 44_000


def test_a_one_hour_cache_write_costs_twice_the_input_price():
    bounds = ModelCallBounds(
        SONNET,
        prompt_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        max_output_tokens=0,
        cache_ttl="1h",
    )
    assert bounds.worst_case(PRICES) == 4_000_000


def test_haiku_55_prices_by_prompt_size():
    small = ModelCallBounds(HAIKU_55, prompt_tokens=100_000, max_output_tokens=1_000)
    large = ModelCallBounds(HAIKU_55, prompt_tokens=100_001, max_output_tokens=1_000)
    assert small.worst_case(PRICES) == 10_000 + 500  # $0.10/MTok in, $0.50/MTok out
    assert large.worst_case(PRICES) == 50_001 + 2_500  # $0.50 in (rounded up), $2.50 out


def test_costs_round_up_once_not_per_line():
    # 1 input token of Haiku 5.5 is 0.1 micro-USD; ten separate lines still cost 1, not 10.
    lines = [Line(HAIKU_55, Meter.INPUT, 1) for _ in range(10)]
    assert cost(PRICES, lines) == 1


def test_an_operation_without_a_price_has_no_cost_model():
    with pytest.raises(NoCostModelError):
        ModelCallBounds("anthropic/unknown", prompt_tokens=1, max_output_tokens=1).worst_case(
            PRICES
        )
    with pytest.raises(NoCostModelError):
        cost(PRICES, [Line("ticketing/create", Meter.CALL, 1)])


def test_zero_quantities_need_no_price():
    assert cost(PRICES, [Line("ticketing/create", Meter.CALL, 0)]) == 0


@st.composite
def bounds_and_usage(draw: st.DrawFn) -> tuple[ModelCallBounds, ModelUsage]:
    """A request's bounds, and any usage the provider could report within them."""
    sku = draw(st.sampled_from(MODELS))
    prompt = draw(st.integers(0, 400_000))
    cached_span = draw(st.integers(0, prompt))
    ttl = draw(st.sampled_from(TTLS))
    max_output = draw(st.integers(0, 128_000))
    written = draw(st.integers(0, cached_span))
    read = draw(st.integers(0, prompt - written))
    bounds = ModelCallBounds(
        sku,
        prompt_tokens=prompt,
        cache_write_tokens=cached_span,
        max_output_tokens=max_output,
        cache_ttl=ttl,
    )
    usage = ModelUsage(
        input_tokens=prompt - written - read,
        output_tokens=draw(st.integers(0, max_output)),
        cache_write_5m_tokens=written if ttl == "5m" else 0,
        cache_write_1h_tokens=written if ttl == "1h" else 0,
        cache_read_tokens=read,
    )
    return bounds, usage


@given(bounds_and_usage())
def test_the_worst_case_is_never_below_the_actual_cost(case: tuple[ModelCallBounds, ModelUsage]):
    bounds, usage = case
    assert usage.actual(PRICES, bounds.sku) <= bounds.worst_case(PRICES)


def test_usage_from_the_messages_api():
    usage = ModelUsage.from_anthropic(
        {
            "input_tokens": 120,
            "output_tokens": 900,
            "cache_creation_input_tokens": 3_000,
            "cache_read_input_tokens": 40_000,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000,
                "ephemeral_1h_input_tokens": 2_000,
            },
        }
    )
    assert usage == ModelUsage(
        input_tokens=120,
        output_tokens=900,
        cache_write_5m_tokens=1_000,
        cache_write_1h_tokens=2_000,
        cache_read_tokens=40_000,
    )
    assert usage.prompt_tokens == 43_120


def test_usage_without_a_ttl_split_prices_writes_as_five_minute():
    usage = ModelUsage.from_anthropic(
        {"input_tokens": 5, "output_tokens": 1, "cache_creation_input_tokens": 70}
    )
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (70, 0)


def test_bounds_reject_impossible_spans():
    with pytest.raises(ValueError, match="cached span"):
        ModelCallBounds(SONNET, prompt_tokens=10, cache_write_tokens=11, max_output_tokens=1)


def test_catalog_rejects_prices_a_worst_case_could_undershoot():
    document = {
        "models": [
            {
                "sku": "x",
                "usd_per_mtok": {
                    "input": "1",
                    "output": "5",
                    "cache_write_5m": "1.25",
                    "cache_write_1h": "2",
                    "cache_read": "1.50",  # a read dearer than input breaks the bound
                },
            }
        ]
    }
    with pytest.raises(CatalogError, match="must rise"):
        catalog_rows(document)


def test_catalog_rejects_fractional_micro_usd():
    document = {
        "models": [
            {
                "sku": "x",
                "usd_per_mtok": {
                    "input": "0.0000001",
                    "output": "5",
                    "cache_write_5m": "1.25",
                    "cache_write_1h": "2",
                    "cache_read": "0",
                },
            }
        ]
    }
    with pytest.raises(CatalogError, match="whole number"):
        catalog_rows(document)
