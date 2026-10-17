"""BUD-08: price versions are published whole and never change afterwards."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import ProgrammingError

from collicity_db import Role
from collicity_db.models import PriceItem
from collicity_ledger import catalog
from collicity_ledger.pricing import Meter, PriceList
from collicity_testing.ledger import CATALOG_FILE
from collicity_testing.postgres import MigratedDatabase

SONNET = "anthropic/claude-sonnet-5-5"


def test_a_published_catalog_loads_back_as_published(prices: PriceList):
    assert prices.unit_price(SONNET, Meter.INPUT) == 2  # $2/MTok is 2 micro-USD per token
    assert prices.unit_price(SONNET, Meter.CACHE_READ) * 10 == 2
    haiku = "anthropic/claude-haiku-5-5"
    assert prices.unit_price(haiku, Meter.OUTPUT, prompt_tokens=100_000) * 2 == 1
    assert prices.unit_price(haiku, Meter.OUTPUT, prompt_tokens=100_001) * 2 == 5


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE price_item SET micro_usd = 0",
        "DELETE FROM price_item",
        "UPDATE price_version SET effective_at = now()",
        "DELETE FROM price_version",
        "TRUNCATE price_item",
    ],
)
def test_published_prices_are_immutable_even_for_migrator(
    database: MigratedDatabase, prices: PriceList, statement: str
):
    with (
        pytest.raises(ProgrammingError, match="immutable"),
        database.engine(Role.MIGRATOR).begin() as conn,
    ):
        conn.execute(text(statement))


def test_a_published_version_cannot_gain_prices_later(
    database: MigratedDatabase, prices: PriceList
):
    with (
        pytest.raises(ProgrammingError, match="already published"),
        database.engine(Role.MIGRATOR).begin() as conn,
    ):
        conn.execute(
            insert(PriceItem).values(
                price_version_id=prices.version_id,
                sku="anthropic/claude-sonnet-5-5",
                meter="call",
                unit_size=1,
                micro_usd=0,
            )
        )


def test_the_current_version_is_the_latest_in_effect(database: MigratedDatabase, prices: PriceList):
    future = {
        "label": "test-future",
        "effective_at": "2099-01-01T00:00:00+00:00",
        "source": "test",
        "items": [{"sku": "test/fee", "meter": "use", "unit_size": 1, "micro_usd": 1}],
    }
    with database.engine(Role.MIGRATOR).begin() as conn:
        future_id = catalog.publish(conn, future)
    with database.engine(Role.GW).connect() as conn:
        assert catalog.current_version_id(conn) == prices.version_id
        assert catalog.current_version_id(conn, at=datetime(2099, 6, 1, tzinfo=UTC)) == future_id
        assert catalog.load(conn, future_id).unit_price("test/fee", Meter.USE) == 1


def test_republishing_a_label_is_a_no_op_but_changing_its_prices_is_refused(
    database: MigratedDatabase, prices: PriceList
):
    document = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    with database.engine(Role.MIGRATOR).begin() as conn:
        assert catalog.publish(conn, document) == prices.version_id

    document["models"][0]["usd_per_mtok"]["output"] = "6"
    with (
        pytest.raises(catalog.CatalogError, match="already published"),
        database.engine(Role.MIGRATOR).begin() as conn,
    ):
        catalog.publish(conn, document)
