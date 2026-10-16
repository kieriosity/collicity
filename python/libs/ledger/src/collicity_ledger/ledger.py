"""Reservations against budgets (BUD-03, BUD-10, BUD-11; technical design §6.2).

Thin wrappers over the ``ledger`` schema's functions. Call them on a connection from
``collicity_db.tenant_connection``: the functions refuse to run without a tenant, and row-level
security keeps every call inside it. Each wrapper runs in the caller's transaction.

Who may call what is enforced by database grants, not by this module:

- ``reserve``: app and worker, before anything billable (the caller's worst case);
- ``start`` and ``settle``: the gateway only, around the billable call itself;
- ``release``: anyone holding the reservation, while it hasn't started;
- ``expire`` and ``due_for_expiry``: the worker's sweeper.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Connection, text

from collicity_core import new_id
from collicity_ledger.pricing import ModelCallBounds, PriceList


class ReserveStatus(StrEnum):
    RESERVED = "reserved"
    INSUFFICIENT = "insufficient"  # some budget lacks headroom; nothing was reserved
    NO_BUDGET = "no_budget"  # no organization budget, or none for what it's charged to


class StartStatus(StrEnum):
    STARTED = "started"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"  # held for another person or assignment
    NOT_ACTIVE = "not_active"  # already started, released or settled
    EXPIRED = "expired"
    INSUFFICIENT = "insufficient"  # smaller than the gateway's own worst case


class SettleStatus(StrEnum):
    SETTLED = "settled"
    SETTLED_OVERRUN = "settled_overrun"  # actual cost above the hold: the cost model is wrong
    NOT_FOUND = "not_found"
    NOT_STARTED = "not_started"


class ReleaseStatus(StrEnum):
    RELEASED = "released"
    NOT_FOUND = "not_found"
    NOT_ACTIVE = "not_active"


class ExpireStatus(StrEnum):
    RELEASED = "released"  # never started: given back
    CHARGED = "charged"  # may have started: charged in full until the actual cost arrives
    NOT_DUE = "not_due"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class ReserveResult:
    status: ReserveStatus
    reservation_id: UUID
    blocking_budget_ids: tuple[UUID, ...] = ()


def reserve(
    conn: Connection,
    *,
    principal_id: UUID,
    amount_micro_usd: int,
    price_version_id: UUID,
    purpose: str,
    ttl: timedelta,
    assignment_id: UUID | None = None,
    reservation_id: UUID | None = None,
) -> ReserveResult:
    """Hold ``amount_micro_usd`` against every applicable budget, or nothing.

    Without ``assignment_id`` the work is charged to the person's workspace (BUD-11). Pass the
    same ``reservation_id`` when retrying, so a lost reply can't hold the amount twice.
    """
    reservation_id = reservation_id or new_id()
    status, blocking = conn.execute(
        text(
            "SELECT status, blocking_budget_ids FROM ledger.reserve("
            ":id, :principal, :assignment, :amount, :version, :purpose, :ttl)"
        ),
        {
            "id": reservation_id,
            "principal": principal_id,
            "assignment": assignment_id,
            "amount": amount_micro_usd,
            "version": price_version_id,
            "purpose": purpose,
            "ttl": ttl,
        },
    ).one()
    return ReserveResult(ReserveStatus(status), reservation_id, tuple(blocking or ()))


def start(
    conn: Connection,
    reservation_id: UUID,
    *,
    principal_id: UUID,
    required_micro_usd: int,
    settle_within: timedelta,
    assignment_id: UUID | None = None,
) -> StartStatus:
    """Mark a reservation started, if it covers ``required_micro_usd`` (the gateway's figure)."""
    status = conn.execute(
        text("SELECT ledger.start(:id, :principal, :assignment, :required, :within)"),
        {
            "id": reservation_id,
            "principal": principal_id,
            "assignment": assignment_id,
            "required": required_micro_usd,
            "within": settle_within,
        },
    ).scalar_one()
    return StartStatus(status)


def start_model_call(
    conn: Connection,
    reservation_id: UUID,
    *,
    prices: PriceList,
    bounds: ModelCallBounds,
    principal_id: UUID,
    assignment_id: UUID | None = None,
    settle_within: timedelta = timedelta(hours=1),
) -> StartStatus:
    """The gateway's check before a model call (§3.2, step 2).

    The gateway recomputes the worst case itself, from the price version the reservation
    recorded, and refuses a reservation smaller than its own figure. ``prices`` must be that
    version.
    """
    recorded = conn.execute(
        text("SELECT price_version_id FROM reservation WHERE id = :id"), {"id": reservation_id}
    ).scalar_one_or_none()
    if recorded is not None and recorded != prices.version_id:
        raise ValueError("prices must be the version the reservation recorded")
    return start(
        conn,
        reservation_id,
        principal_id=principal_id,
        assignment_id=assignment_id,
        required_micro_usd=bounds.worst_case(prices),
        settle_within=settle_within,
    )


def settle(conn: Connection, reservation_id: UUID, actual_micro_usd: int) -> SettleStatus:
    """Record the actual cost of a started (or expired and charged) reservation."""
    status = conn.execute(
        text("SELECT ledger.settle(:id, :actual)"),
        {"id": reservation_id, "actual": actual_micro_usd},
    ).scalar_one()
    return SettleStatus(status)


def release(conn: Connection, reservation_id: UUID) -> ReleaseStatus:
    status = conn.execute(text("SELECT ledger.release(:id)"), {"id": reservation_id}).scalar_one()
    return ReleaseStatus(status)


def expire(conn: Connection, reservation_id: UUID) -> ExpireStatus:
    status = conn.execute(text("SELECT ledger.expire(:id)"), {"id": reservation_id}).scalar_one()
    return ExpireStatus(status)


def due_for_expiry(conn: Connection, limit: int = 500) -> Sequence[tuple[UUID, UUID]]:
    """(tenant, reservation) pairs past expiry, across tenants. Needs no tenant context.

    The sweeper then expires each one in its own ``tenant_connection``.
    """
    rows = conn.execute(
        text("SELECT tenant_id, reservation_id FROM sweep.due_for_expiry(:limit)"),
        {"limit": limit},
    ).all()
    return [(row.tenant_id, row.reservation_id) for row in rows]
