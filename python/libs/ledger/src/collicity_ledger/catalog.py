"""The price catalog (BUD-08): publish versions from a file, and load them.

Prices live in versioned catalog files (``catalog/prices/*.json`` at the repository root), not in
code. Publishing inserts a new immutable version; each reservation records the version it used.

File format::

    {
      "label": "2026-10-15",
      "effective_at": "2026-10-15T00:00:00Z",
      "source": "where the prices came from",
      "models": [
        {"sku": "anthropic/claude-sonnet-5-5",
         "usd_per_mtok": {"input": "2", "output": "10", "cache_write_5m": "2.50",
                          "cache_write_1h": "4", "cache_read": "0.20"},
         "tiers": [{"min_prompt_tokens": 100001, "usd_per_mtok": {...}}]}
      ],
      "items": [{"sku": "...", "meter": "second", "unit_size": 1, "micro_usd": 34}]
    }
"""

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, func, insert, select

from collicity_core import new_id
from collicity_db.models import PriceItem, PriceVersion
from collicity_ledger.pricing import Meter, PriceList, Tier

MTOK = 1_000_000
MODEL_METERS = (
    Meter.INPUT,
    Meter.OUTPUT,
    Meter.CACHE_WRITE_5M,
    Meter.CACHE_WRITE_1H,
    Meter.CACHE_READ,
)


class CatalogError(ValueError):
    """A catalog file is malformed or would let a worst case undershoot an actual cost."""


def _micro_usd_per_mtok(usd: str) -> int:
    value = Decimal(usd) * MTOK
    if value != value.to_integral_value() or value < 0:
        raise CatalogError(f"${usd}/MTok isn't a whole number of micro-USD per MTok")
    return int(value)


def catalog_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expand a catalog document into price_item rows, checking it as it goes."""
    rows: list[dict[str, Any]] = []
    for model in document.get("models", []):
        sku = model["sku"]
        tiers = [{"min_prompt_tokens": 0, "usd_per_mtok": model["usd_per_mtok"]}]
        tiers += model.get("tiers", [])
        for tier in tiers:
            prices = {meter: tier["usd_per_mtok"].get(meter.value) for meter in MODEL_METERS}
            missing = [meter.value for meter, usd in prices.items() if usd is None]
            if missing:
                raise CatalogError(f"{sku} lacks prices for {', '.join(missing)}")
            micro = {meter: _micro_usd_per_mtok(usd) for meter, usd in prices.items()}
            # A cache read never costs more than input, nor input more than a cache write,
            # so the worst case (no reads, every declared write) bounds the actual cost.
            ordered = [Meter.CACHE_READ, Meter.INPUT, Meter.CACHE_WRITE_5M, Meter.CACHE_WRITE_1H]
            if [micro[m] for m in ordered] != sorted(micro[m] for m in ordered):
                raise CatalogError(f"{sku} prices must rise from cache read to input to writes")
            rows += [
                {
                    "sku": sku,
                    "meter": meter.value,
                    "tier_floor": int(tier["min_prompt_tokens"]),
                    "unit_size": MTOK,
                    "micro_usd": micro[meter],
                }
                for meter in MODEL_METERS
            ]
    for item in document.get("items", []):
        rows.append(
            {
                "sku": item["sku"],
                "meter": Meter(item["meter"]).value,
                "tier_floor": int(item.get("tier_floor", 0)),
                "unit_size": int(item["unit_size"]),
                "micro_usd": int(item["micro_usd"]),
            }
        )
    if not rows:
        raise CatalogError("the catalog has no prices")
    return rows


def publish(conn: Connection, document: Mapping[str, Any]) -> UUID:
    """Insert a new price version in the caller's transaction (as migrator) and return its id.

    Publishing a label again is a no-op when its prices are identical, so deploys can publish
    every catalog file each time. Different prices under a published label are an error: a
    change needs a new label.
    """
    rows = catalog_rows(document)
    existing = conn.execute(
        select(PriceVersion.id).where(PriceVersion.label == document["label"])
    ).scalar_one_or_none()
    if existing is not None:
        published = load(conn, existing)
        if published.tiers != _price_list(existing, rows).tiers:
            raise CatalogError(f"{document['label']} is already published with other prices")
        return existing

    version_id = new_id()
    conn.execute(
        insert(PriceVersion).values(
            id=version_id,
            label=document["label"],
            effective_at=datetime.fromisoformat(document["effective_at"]),
            source=document["source"],
        )
    )
    conn.execute(insert(PriceItem), [{"price_version_id": version_id, **row} for row in rows])
    return version_id


def current_version_id(conn: Connection, at: datetime | None = None) -> UUID:
    """The latest version in effect at ``at`` (default: now)."""
    query = (
        select(PriceVersion.id)
        .where(PriceVersion.effective_at <= (func.now() if at is None else at))
        .order_by(PriceVersion.effective_at.desc())
        .limit(1)
    )
    version_id = conn.execute(query).scalar_one_or_none()
    if version_id is None:
        raise LookupError("no price version is in effect")
    return version_id


def load(conn: Connection, version_id: UUID) -> PriceList:
    """Load one version. Versions are immutable, so callers may cache the result forever."""
    rows = conn.execute(
        select(
            PriceItem.sku,
            PriceItem.meter,
            PriceItem.tier_floor,
            PriceItem.unit_size,
            PriceItem.micro_usd,
        )
        .where(PriceItem.price_version_id == version_id)
        .order_by(PriceItem.sku, PriceItem.meter, PriceItem.tier_floor)
    ).all()
    if not rows:
        raise LookupError(f"price version {version_id} doesn't exist")
    return _price_list(
        version_id,
        [
            {"sku": sku, "meter": meter, "tier_floor": floor, "unit_size": size, "micro_usd": price}
            for sku, meter, floor, size, price in rows
        ],
    )


def _price_list(version_id: UUID, rows: list[dict[str, Any]]) -> PriceList:
    tiers: dict[tuple[str, Meter], list[Tier]] = {}
    for row in sorted(rows, key=lambda r: (r["sku"], r["meter"], r["tier_floor"])):
        tiers.setdefault((row["sku"], Meter(row["meter"])), []).append(
            Tier(row["tier_floor"], row["unit_size"], row["micro_usd"])
        )
    return PriceList(version_id, {key: tuple(value) for key, value in tiers.items()})
