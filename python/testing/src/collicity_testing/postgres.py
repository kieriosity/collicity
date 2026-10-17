"""Shared fixtures: a migrated Postgres 17 database with Collicity's roles.

By default a disposable container is started (Docker required). Set
COLLICITY_TEST_ADMIN_DATABASE_URL to a superuser URL to use an existing server instead, such as
the one ``just db-up`` starts; each session then creates and drops its own database. Use
127.0.0.1 rather than localhost on Windows, where an IPv6 attempt stalls each connection.
"""

import os
import secrets
from collections.abc import Generator
from dataclasses import dataclass, field
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Engine, insert
from sqlalchemy.engine import make_url

from collicity_core import new_id
from collicity_db.migrate import upgrade
from collicity_db.models import Principal, RoleGrant, Tenant
from collicity_db.roles import Role, bootstrap_cluster, libpq_url
from collicity_db.session import make_engine

POSTGRES_IMAGE = "postgres:17-alpine"
# Roles are cluster-wide: on a shared server, use the passwords the environment already gave them
# (``just test`` exports the local ones), so a test run never changes the dev database's logins.
TEST_PASSWORDS = {
    role: os.environ.get(f"COLLICITY_ROLE_PASSWORD_{role.name}", f"{role.value}-test-only")
    for role in Role
}


@dataclass
class MigratedDatabase:
    name: str
    admin_url: str = field(repr=False)
    urls: dict[Role, str] = field(repr=False)
    _engines: dict[Role, Engine] = field(default_factory=dict[Role, Engine], repr=False)

    _admin_engine: Engine | None = field(default=None, repr=False)

    def engine(self, role: Role) -> Engine:
        """A pooled engine connected as ``role``, shared for the test session."""
        if role not in self._engines:
            self._engines[role] = make_engine(self.urls[role])
        return self._engines[role]

    def admin_engine(self) -> Engine:
        """A superuser engine on the test database, for reading the catalog."""
        if self._admin_engine is None:
            self._admin_engine = make_engine(self.admin_url)
        return self._admin_engine

    def dispose(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        if self._admin_engine is not None:
            self._admin_engine.dispose()


def _server_url() -> Generator[str]:
    if url := os.environ.get("COLLICITY_TEST_ADMIN_DATABASE_URL"):
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415 (needs Docker)

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container.get_connection_url()


@dataclass(frozen=True)
class SeededTenant:
    id: UUID
    principal_ids: tuple[UUID, ...]


def seed_tenant(db: MigratedDatabase, principals: int = 2, role: str = "member") -> SeededTenant:
    """Insert a tenant with ``principals`` people, each holding ``role``, as migrator."""
    tenant_id = new_id()
    principal_ids = tuple(new_id() for _ in range(principals))
    with db.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            insert(Tenant).values(
                id=tenant_id,
                slug=f"t-{tenant_id.hex[-12:]}",
                display_name=f"Tenant {tenant_id.hex[-6:]}",
                home_region="us-east-1",
            )
        )
        for principal_id in principal_ids:
            conn.execute(
                insert(Principal).values(
                    id=principal_id,
                    tenant_id=tenant_id,
                    kind="person",
                    idp_subject=f"user_{principal_id.hex}",
                    display_name=f"Person {principal_id.hex[-6:]}",
                )
            )
            conn.execute(
                insert(RoleGrant).values(tenant_id=tenant_id, principal_id=principal_id, role=role)
            )
    return SeededTenant(tenant_id, principal_ids)


@pytest.fixture(scope="session")
def database() -> Generator[MigratedDatabase]:
    for server_url in _server_url():
        name = f"collicity_test_{secrets.token_hex(4)}"
        bootstrap_cluster(server_url, name, TEST_PASSWORDS)
        base = make_url(server_url).set(database=name)
        db = MigratedDatabase(
            name=name,
            admin_url=base.render_as_string(hide_password=False),
            urls={
                role: base.set(username=role.value, password=TEST_PASSWORDS[role]).render_as_string(
                    hide_password=False
                )
                for role in Role
            },
        )
        upgrade(db.urls[Role.MIGRATOR])
        try:
            yield db
        finally:
            db.dispose()
            with psycopg.connect(libpq_url(server_url), autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )
