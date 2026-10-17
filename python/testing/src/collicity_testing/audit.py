"""Audit fixtures: a tenant whose connections come with audit keys."""

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import Connection

from collicity_audit import Keys
from collicity_audit.crypto import LocalKeyWrapper
from collicity_core import TenantContext
from collicity_db import Role, tenant_connection
from collicity_testing.postgres import MigratedDatabase, SeededTenant, seed_tenant

# A fixed local wrapping key: tests only.
WRAPPER = LocalKeyWrapper(bytes(range(32)))


@dataclass(frozen=True)
class AuditTenant:
    tenant: SeededTenant
    open: Callable[[Role], AbstractContextManager[tuple[Connection, Keys]]]


def audit_session(
    database: MigratedDatabase, tenant: SeededTenant
) -> Callable[[Role], AbstractContextManager[tuple[Connection, Keys]]]:
    @contextmanager
    def open_(role: Role) -> Generator[tuple[Connection, Keys]]:
        ctx = TenantContext(tenant.id, tenant.principal_ids[0] if tenant.principal_ids else None)
        with tenant_connection(database.engine(role), ctx) as conn:
            yield conn, Keys(WRAPPER, tenant.id)

    return open_


@pytest.fixture
def audit_tenant(database: MigratedDatabase) -> AuditTenant:
    """A fresh tenant with three people; ``open(role)`` yields a tenant connection and keys."""
    seeded = seed_tenant(database, principals=3)
    return AuditTenant(seeded, audit_session(database, seeded))
