"""ORM mappings for the tables the migrations create.

Migrations are hand-written SQL, because row-level security, policies and grants are the point
of them. These mappings must match; ``tests/test_migrations.py`` fails when they drift.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Postgres's own default names, so hand-written SQL and these mappings agree.
NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "ck": "%(table_name)s_%(constraint_name)s_check",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 (SQLAlchemy reads this class attribute)
        str: Text(),
        int: BigInteger(),
        datetime: DateTime(timezone=True),
    }


class Tenant(Base):
    """A customer organization. ``epoch`` bumps on a tenant-wide pause (ASG-13)."""

    __tablename__ = "tenant"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str]
    home_region: Mapped[str]
    status: Mapped[str]
    epoch: Mapped[int]
    workos_organization_id: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime]
    runs_paused_at: Mapped[datetime | None]
    runs_paused_by: Mapped[UUID | None]


class Principal(Base):
    """A person or service in a tenant. ``epoch`` bumps on suspension or deprovisioning."""

    __tablename__ = "principal"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "idp_subject"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.id"))
    kind: Mapped[str]
    status: Mapped[str]
    epoch: Mapped[int]
    idp_subject: Mapped[str | None]
    email: Mapped[str | None]
    display_name: Mapped[str]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    runs_paused_at: Mapped[datetime | None]
    runs_paused_by: Mapped[UUID | None]


class RoleGrant(Base):
    """A tenant role held by a principal (ADM-01)."""

    __tablename__ = "role_grant"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"], ["principal.tenant_id", "principal.id"]
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(primary_key=True)
    granted_at: Mapped[datetime]


class PriceVersion(Base):
    """A published price catalog version (BUD-08). Immutable."""

    __tablename__ = "price_version"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(unique=True)
    effective_at: Mapped[datetime]
    source: Mapped[str]
    published_at: Mapped[datetime]


class PriceItem(Base):
    """``micro_usd`` per ``unit_size`` units of ``meter`` for ``sku``, from ``tier_floor`` up."""

    __tablename__ = "price_item"

    price_version_id: Mapped[UUID] = mapped_column(ForeignKey("price_version.id"), primary_key=True)
    sku: Mapped[str] = mapped_column(primary_key=True)
    meter: Mapped[str] = mapped_column(primary_key=True)
    tier_floor: Mapped[int] = mapped_column(primary_key=True, server_default=text("0"))
    unit_size: Mapped[int]
    micro_usd: Mapped[int]


class Budget(Base):
    """A limit per period for the organization, a person, an assignment or a workspace."""

    __tablename__ = "budget"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id",
            "scope",
            "principal_id",
            "assignment_id",
            "period",
            postgresql_nulls_not_distinct=True,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"], ["principal.tenant_id", "principal.id"]
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.id"))
    scope: Mapped[str]
    principal_id: Mapped[UUID | None]
    assignment_id: Mapped[UUID | None]
    period: Mapped[str]
    limit_micro_usd: Mapped[int]
    lock_rank: Mapped[int] = mapped_column(
        SmallInteger,
        Computed(
            "CASE scope WHEN 'organization' THEN 10 WHEN 'user' THEN 30 ELSE 40 END",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime]


class LedgerAccount(Base):
    """One budget's balance for one period."""

    __tablename__ = "ledger_account"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("budget_id", "period_start"),
        ForeignKeyConstraint(["tenant_id", "budget_id"], ["budget.tenant_id", "budget.id"]),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    budget_id: Mapped[UUID]
    period_start: Mapped[datetime]
    limit_micro_usd: Mapped[int]
    spent_micro_usd: Mapped[int]
    reserved_micro_usd: Mapped[int]
    lock_rank: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime]


class Reservation(Base):
    """A worst-case hold for one billable operation (BUD-03)."""

    __tablename__ = "reservation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"], ["principal.tenant_id", "principal.id"]
        ),
        Index(
            "reservation_expires_at_idx",
            "expires_at",
            postgresql_where=text("state IN ('active', 'started')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.id"))
    principal_id: Mapped[UUID]
    assignment_id: Mapped[UUID | None]
    price_version_id: Mapped[UUID] = mapped_column(ForeignKey("price_version.id"))
    purpose: Mapped[str]
    amount_micro_usd: Mapped[int]
    actual_micro_usd: Mapped[int | None]
    state: Mapped[str]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class ReservationAccount(Base):
    """Which ledger accounts a reservation holds against."""

    __tablename__ = "reservation_account"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "reservation_id"], ["reservation.tenant_id", "reservation.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "ledger_account_id"], ["ledger_account.tenant_id", "ledger_account.id"]
        ),
    )

    tenant_id: Mapped[UUID]
    reservation_id: Mapped[UUID] = mapped_column(primary_key=True)
    ledger_account_id: Mapped[UUID] = mapped_column(primary_key=True)


class LedgerEntry(Base):
    """An append-only record of one balance change."""

    __tablename__ = "ledger_entry"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ledger_account_id"], ["ledger_account.tenant_id", "ledger_account.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "reservation_id"], ["reservation.tenant_id", "reservation.id"]
        ),
        Index("ledger_entry_tenant_id_ledger_account_id_idx", "tenant_id", "ledger_account_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    ledger_account_id: Mapped[UUID]
    reservation_id: Mapped[UUID]
    kind: Mapped[str]
    reserved_delta: Mapped[int]
    spent_delta: Mapped[int]
    created_at: Mapped[datetime]


class Assignment(Base):
    """A standing piece of work owned by a person. Its id is also its agent ID (MR-04)."""

    __tablename__ = "assignment"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "owner_id"], ["principal.tenant_id", "principal.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "id", "current_version_id"],
            [
                "assignment_version.tenant_id",
                "assignment_version.assignment_id",
                "assignment_version.id",
            ],
            use_alter=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.id"))
    owner_id: Mapped[UUID]
    name: Mapped[str]
    current_version_id: Mapped[UUID | None]
    epoch: Mapped[int]
    runs_paused_at: Mapped[datetime | None]
    runs_paused_by: Mapped[UUID | None]
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    concurrency_policy: Mapped[str]
    max_running: Mapped[int] = mapped_column(Integer)
    max_queued: Mapped[int] = mapped_column(Integer)


class AssignmentVersion(Base):
    """An immutable version: capability grants, task graph and provenance (ASG-01, SEC-12)."""

    __tablename__ = "assignment_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "assignment_id", "id"),
        UniqueConstraint("assignment_id", "number"),
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id"], ["assignment.tenant_id", "assignment.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "created_by"], ["principal.tenant_id", "principal.id"]),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    assignment_id: Mapped[UUID]
    number: Mapped[int] = mapped_column(Integer)
    grants: Mapped[Any] = mapped_column(JSONB)
    task_graph: Mapped[Any] = mapped_column(JSONB)
    taint: Mapped[str]
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_by: Mapped[UUID]
    created_at: Mapped[datetime]


class VersionAcceptance(Base):
    """A person's acceptance of a version, with what it widened."""

    __tablename__ = "version_acceptance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id", "version_id"],
            [
                "assignment_version.tenant_id",
                "assignment_version.assignment_id",
                "assignment_version.id",
            ],
        ),
        ForeignKeyConstraint(["tenant_id", "accepted_by"], ["principal.tenant_id", "principal.id"]),
    )

    version_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    assignment_id: Mapped[UUID]
    accepted_by: Mapped[UUID]
    accepted_at: Mapped[datetime]
    content_hash: Mapped[bytes] = mapped_column(LargeBinary)
    widening: Mapped[Any] = mapped_column(JSONB)


class StepGrant(Base):
    """A grant for one step and attempt, valid only while its recorded epochs are current."""

    __tablename__ = "step_grant"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id", "version_id"],
            [
                "assignment_version.tenant_id",
                "assignment_version.assignment_id",
                "assignment_version.id",
            ],
        ),
        ForeignKeyConstraint(["tenant_id", "owner_id"], ["principal.tenant_id", "principal.id"]),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["run.tenant_id", "run.id"]),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    assignment_id: Mapped[UUID]
    version_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    run_id: Mapped[UUID | None]
    step_key: Mapped[str]
    attempt: Mapped[int] = mapped_column(Integer)
    grants: Mapped[Any] = mapped_column(JSONB)
    dry_run: Mapped[bool]
    tenant_epoch: Mapped[int]
    owner_epoch: Mapped[int]
    assignment_epoch: Mapped[int]
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    run_epoch: Mapped[int | None]


class TriggerEvent(Base):
    """One trigger event for one assignment; its dedup key is unique while kept (RUN-02)."""

    __tablename__ = "trigger_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "assignment_id", "dedup_key"),
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id"], ["assignment.tenant_id", "assignment.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["run.tenant_id", "run.id"], use_alter=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    assignment_id: Mapped[UUID]
    kind: Mapped[str]
    dedup_key: Mapped[str]
    event_at: Mapped[datetime | None]
    received_at: Mapped[datetime]
    keep_until: Mapped[datetime]
    outcome: Mapped[str]
    run_id: Mapped[UUID | None]


class Run(Base):
    """A run. Its state changes only through the runtime's functions (§4.1)."""

    __tablename__ = "run"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id"], ["assignment.tenant_id", "assignment.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assignment_id", "version_id"],
            [
                "assignment_version.tenant_id",
                "assignment_version.assignment_id",
                "assignment_version.id",
            ],
        ),
        ForeignKeyConstraint(["tenant_id", "owner_id"], ["principal.tenant_id", "principal.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "trigger_event_id"], ["trigger_event.tenant_id", "trigger_event.id"]
        ),
        Index("run_assignment_id_state_idx", "assignment_id", "state"),
        Index(
            "run_assignment_id_priority_created_at_idx",
            "assignment_id",
            text("priority DESC"),
            "created_at",
            "id",
            postgresql_where=text("state = 'queued'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    assignment_id: Mapped[UUID]
    version_id: Mapped[UUID | None]
    owner_id: Mapped[UUID]
    trigger_event_id: Mapped[UUID]
    state: Mapped[str]
    state_version: Mapped[int]
    segment_no: Mapped[int] = mapped_column(Integer)
    continuation: Mapped[Any] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(SmallInteger)
    epoch: Mapped[int]
    effects_applied: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime | None]
    paused_at: Mapped[datetime | None]
    paused_by: Mapped[UUID | None]
    cancel_requested_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]


class OutboxEvent(Base):
    """An event for the relay. ``seq`` is assigned at publish time, per tenant (§9)."""

    __tablename__ = "outbox_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "seq"),
        Index(
            "outbox_event_tenant_id_created_at_idx",
            "tenant_id",
            "created_at",
            "id",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.id"))
    kind: Mapped[str]
    subject_id: Mapped[UUID]
    payload: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    seq: Mapped[int | None]
    published_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]


class OutboxCursor(Base):
    __tablename__ = "outbox_cursor"

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenant.id"), primary_key=True)
    last_seq: Mapped[int]
