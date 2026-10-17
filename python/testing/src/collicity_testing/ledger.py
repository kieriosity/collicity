"""Ledger fixtures: a published price catalog and tenants with budgets."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, insert, select, text, update

from collicity_core import new_id
from collicity_db import Role
from collicity_db.models import Budget, LedgerAccount, LedgerEntry, Reservation
from collicity_ledger import catalog
from collicity_ledger.pricing import PriceList
from collicity_testing.postgres import MigratedDatabase, SeededTenant, seed_tenant

CATALOG_FILE = Path(__file__).parents[4] / "catalog" / "prices" / "2026-10-15.json"


@pytest.fixture(scope="session")
def prices(database: MigratedDatabase) -> PriceList:
    """The repository's price catalog, published once per test session."""
    document = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    with database.engine(Role.MIGRATOR).begin() as conn:
        version_id = catalog.publish(conn, document)
        return catalog.load(conn, version_id)


@dataclass(frozen=True)
class LedgerScenario:
    tenant: SeededTenant
    person: UUID
    assignment: UUID
    org_budget: UUID
    user_budget: UUID
    workspace_budget: UUID
    assignment_budget: UUID


def seed_budget(
    db: MigratedDatabase,
    tenant_id: UUID,
    scope: str,
    limit_micro_usd: int,
    *,
    principal_id: UUID | None = None,
    assignment_id: UUID | None = None,
    period: str = "month",
) -> UUID:
    budget_id = new_id()
    with db.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            insert(Budget).values(
                id=budget_id,
                tenant_id=tenant_id,
                scope=scope,
                principal_id=principal_id,
                assignment_id=assignment_id,
                period=period,
                limit_micro_usd=limit_micro_usd,
            )
        )
    return budget_id


def ledger_scenario(
    db: MigratedDatabase,
    *,
    org: int = 10_000_000,
    user: int = 5_000_000,
    workspace: int = 1_000_000,
    assignment: int = 2_000_000,
) -> LedgerScenario:
    """A tenant whose first person has a user, workspace and assignment budget (micro-USD)."""
    tenant = seed_tenant(db, principals=2)
    person = tenant.principal_ids[0]
    assignment_id = new_id()
    return LedgerScenario(
        tenant=tenant,
        person=person,
        assignment=assignment_id,
        org_budget=seed_budget(db, tenant.id, "organization", org),
        user_budget=seed_budget(db, tenant.id, "user", user, principal_id=person),
        workspace_budget=seed_budget(db, tenant.id, "workspace", workspace, principal_id=person),
        assignment_budget=seed_budget(
            db, tenant.id, "assignment", assignment, assignment_id=assignment_id
        ),
    )


@dataclass(frozen=True)
class Balance:
    limit: int
    spent: int
    reserved: int


def balances(db: MigratedDatabase, budget_ids: Iterable[UUID]) -> dict[UUID, Balance]:
    """Each budget's current balance, summed over its periods (zero if never used)."""
    ids = list(budget_ids)
    with db.engine(Role.MIGRATOR).connect() as conn:
        rows = conn.execute(
            select(
                Budget.id,
                Budget.limit_micro_usd,
                func.coalesce(func.sum(LedgerAccount.spent_micro_usd), 0),
                func.coalesce(func.sum(LedgerAccount.reserved_micro_usd), 0),
            )
            .outerjoin(LedgerAccount, LedgerAccount.budget_id == Budget.id)
            .where(Budget.id.in_(ids))
            .group_by(Budget.id, Budget.limit_micro_usd)
        ).all()
    return {row[0]: Balance(int(row[1]), int(row[2]), int(row[3])) for row in rows}


def journal_matches_balances(db: MigratedDatabase, tenant_id: UUID) -> bool:
    """Whether every account's balance equals the sum of its journal entries."""
    with db.engine(Role.MIGRATOR).connect() as conn:
        mismatches = conn.execute(
            select(func.count())
            .select_from(LedgerAccount)
            .where(LedgerAccount.tenant_id == tenant_id)
            .where(
                (
                    LedgerAccount.reserved_micro_usd
                    != select(func.coalesce(func.sum(LedgerEntry.reserved_delta), 0))
                    .where(LedgerEntry.ledger_account_id == LedgerAccount.id)
                    .scalar_subquery()
                )
                | (
                    LedgerAccount.spent_micro_usd
                    != select(func.coalesce(func.sum(LedgerEntry.spent_delta), 0))
                    .where(LedgerEntry.ledger_account_id == LedgerAccount.id)
                    .scalar_subquery()
                )
            )
        ).scalar_one()
    return mismatches == 0


def make_due(db: MigratedDatabase, reservation_id: UUID) -> None:
    """Move a reservation's expiry into the past, as time passing would."""
    with db.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id)
            .values(expires_at=text("now() - interval '1 second'"))
        )
