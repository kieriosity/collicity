"""SEC-09: each tenant sees only its own rows, under every service role."""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DataError, IntegrityError

from collicity_core import TenantContext, new_id
from collicity_db import Role, make_engine, make_session_factory, tenant_connection, tenant_session
from collicity_db.models import Principal, RoleGrant, Tenant
from collicity_db.roles import SERVICE_ROLES
from collicity_testing.postgres import MigratedDatabase, seed_tenant

TENANT_TABLES = ("tenant", "principal", "role_grant")


@pytest.mark.parametrize("role", SERVICE_ROLES)
@pytest.mark.parametrize("table", TENANT_TABLES)
def test_without_a_context_every_tenant_table_reads_as_empty(
    database: MigratedDatabase, role: Role, table: str
):
    seed_tenant(database)
    with database.engine(role).connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(sizes=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=4))
def test_each_tenant_sees_exactly_its_own_rows(database: MigratedDatabase, sizes: list[int]):
    tenants = [seed_tenant(database, principals=n) for n in sizes]
    factory = {role: make_session_factory(database.engine(role)) for role in SERVICE_ROLES}

    for seeded in tenants:
        for role in SERVICE_ROLES:
            with tenant_session(factory[role], TenantContext(seeded.id)) as session:
                assert session.scalars(select(Tenant.id)).all() == [seeded.id]
                assert set(session.scalars(select(Principal.id))) == set(seeded.principal_ids)
                grants = session.execute(select(RoleGrant.tenant_id, RoleGrant.principal_id))
                assert {(g.tenant_id, g.principal_id) for g in grants} == {
                    (seeded.id, p) for p in seeded.principal_ids
                }


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_naming_another_tenants_rows_returns_nothing(database: MigratedDatabase, role: Role):
    mine, theirs = seed_tenant(database), seed_tenant(database)
    with tenant_connection(database.engine(role), TenantContext(mine.id)) as conn:
        assert conn.execute(select(Tenant).where(Tenant.id == theirs.id)).first() is None
        assert (
            conn.execute(select(Principal).where(Principal.tenant_id == theirs.id)).first() is None
        )
        assert (
            conn.execute(select(Principal).where(Principal.id.in_(theirs.principal_ids))).first()
            is None
        )


def test_context_ends_with_its_transaction_on_a_reused_connection(database: MigratedDatabase):
    seeded = seed_tenant(database)
    engine = make_engine(database.urls[Role.APP_USER], pool_size=1, max_overflow=0)
    try:
        with tenant_connection(engine, TenantContext(seeded.id, seeded.principal_ids[0])) as conn:
            backend = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
            assert conn.execute(text("SELECT current_tenant_id()")).scalar_one() == seeded.id

        with engine.connect() as conn:
            assert conn.execute(text("SELECT pg_backend_pid()")).scalar_one() == backend
            assert conn.execute(text("SELECT current_tenant_id()")).scalar_one() is None
            assert conn.execute(text("SELECT current_principal_id()")).scalar_one() is None
            assert conn.execute(text("SELECT count(*) FROM principal")).scalar_one() == 0
    finally:
        engine.dispose()


def test_context_ends_when_the_transaction_fails(database: MigratedDatabase):
    seeded = seed_tenant(database)
    engine = make_engine(database.urls[Role.APP_USER], pool_size=1, max_overflow=0)
    try:
        with (
            pytest.raises(RuntimeError, match="boom"),
            tenant_connection(engine, TenantContext(seeded.id)),
        ):
            raise RuntimeError("boom")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_tenant_id()")).scalar_one() is None
    finally:
        engine.dispose()


def test_acting_person_is_set_only_when_given(database: MigratedDatabase):
    seeded = seed_tenant(database)
    engine = database.engine(Role.APP_USER)
    person = seeded.principal_ids[0]
    with tenant_connection(engine, TenantContext(seeded.id, person)) as conn:
        assert conn.execute(text("SELECT current_principal_id()")).scalar_one() == person
    with tenant_connection(engine, TenantContext(seeded.id)) as conn:
        assert conn.execute(text("SELECT current_principal_id()")).scalar_one() is None


def test_a_malformed_tenant_setting_fails_instead_of_matching(database: MigratedDatabase):
    seed_tenant(database)
    with database.engine(Role.APP_USER).begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', 'not-a-uuid', true)"))
        with pytest.raises(DataError):
            conn.execute(text("SELECT count(*) FROM principal"))


def test_a_context_for_an_unknown_tenant_sees_nothing(database: MigratedDatabase):
    seed_tenant(database)
    with tenant_connection(database.engine(Role.GW), TenantContext(new_id())) as conn:
        for table in TENANT_TABLES:
            count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
            assert count == 0


def test_composite_keys_stop_a_row_pointing_at_another_tenants_principal(
    database: MigratedDatabase,
):
    mine, theirs = seed_tenant(database), seed_tenant(database)
    # Even migrator, which sees every row, can't link one tenant's grant to another's principal.
    with pytest.raises(IntegrityError), database.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            insert(RoleGrant).values(
                tenant_id=mine.id, principal_id=theirs.principal_ids[0], role="admin"
            )
        )
