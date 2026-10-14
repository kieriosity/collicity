"""ORM mappings for the tables the migrations create.

Migrations are hand-written SQL, because row-level security, policies and grants are the point
of them. These mappings must match; ``tests/test_schema.py`` fails when they drift.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    Text,
    UniqueConstraint,
)
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
