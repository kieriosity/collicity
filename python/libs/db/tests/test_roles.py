"""SEC-09 by structure: service roles own nothing, bypass nothing and hold only listed grants.

These tests read the catalog, so they also cover tables that future migrations add: a new table
without forced RLS, or a new privilege missing from EXPECTED_TABLE_GRANTS, fails here.
"""

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import InternalError, ProgrammingError

from collicity_db import Role
from collicity_db.roles import SERVICE_ROLES
from collicity_testing.postgres import MigratedDatabase

# Tables that hold no tenant data and so carry no tenant RLS. Keep this list short.
GLOBAL_TABLES = {"alembic_version"}

# The tenant predicate each tenant table's isolation policy must use, as Postgres deparses it.
TENANT_PREDICATE = "(tenant_id = current_tenant_id())"
TENANT_PREDICATE_OVERRIDES = {"tenant": "(id = current_tenant_id())"}

# Every table privilege a service role holds. A migration that grants more must update this,
# so each widening of access shows up in review.
_READ_TENANCY = {
    ("tenant", "SELECT"),
    ("principal", "SELECT"),
    ("role_grant", "SELECT"),
}
EXPECTED_TABLE_GRANTS: dict[Role, set[tuple[str, str]]] = {
    Role.APP_USER: _READ_TENANCY,
    Role.APP_WORKER: _READ_TENANCY,
    Role.GW: _READ_TENANCY,
}


def _admin(database: MigratedDatabase) -> Connection:
    return database.admin_engine().connect()


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_service_roles_have_no_elevated_attributes_or_memberships(
    database: MigratedDatabase, role: Role
):
    with _admin(database) as conn:
        attrs = conn.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
                "FROM pg_roles WHERE rolname = :r"
            ),
            {"r": role.value},
        ).one()
        assert not any(attrs)
        memberships = conn.execute(
            text(
                "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                "WHERE r.rolname = :r"
            ),
            {"r": role.value},
        ).scalar_one()
        assert memberships == 0


def test_migrator_cannot_bypass_rls_either(database: MigratedDatabase):
    with _admin(database) as conn:
        attrs = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'migrator'")
        ).one()
        assert not any(attrs)


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_service_roles_own_nothing(database: MigratedDatabase, role: Role):
    with _admin(database) as conn:
        owned = conn.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE r.rolname = :r "
                "UNION ALL SELECT p.proname FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                "WHERE r.rolname = :r "
                "UNION ALL SELECT n.nspname FROM pg_namespace n "
                "JOIN pg_roles r ON r.oid = n.nspowner WHERE r.rolname = :r"
            ),
            {"r": role.value},
        ).all()
        assert owned == []


def test_every_table_is_global_or_under_forced_tenant_rls(database: MigratedDatabase):
    with _admin(database) as conn:
        tables = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r', 'p')"
            )
        ).all()
        policies = {
            (row.tablename, row.policyname): row
            for row in conn.execute(
                text("SELECT tablename, policyname, roles, cmd, qual, with_check FROM pg_policies")
            )
        }

    assert tables, "the migrations created no tables"
    for name, rls, forced in tables:
        if name in GLOBAL_TABLES:
            continue
        assert rls, f"{name} must ENABLE row level security"
        assert forced, f"{name} must FORCE row level security"
        policy = policies.get((name, "tenant_isolation"))
        assert policy is not None, f"{name} has no tenant_isolation policy"
        predicate = TENANT_PREDICATE_OVERRIDES.get(name, TENANT_PREDICATE)
        assert policy.qual == predicate, name
        assert policy.with_check == predicate, name
        assert set(policy.roles) == {r.value for r in SERVICE_ROLES}, name
        # Every other policy on the table belongs to migrator alone; a second permissive
        # policy for a service role would widen what it sees.
        others = [p for (t, n), p in policies.items() if t == name and n != "tenant_isolation"]
        assert all(p.roles == ["migrator"] for p in others), name


def test_service_role_grants_match_the_expected_list(database: MigratedDatabase):
    with _admin(database) as conn:
        rows = conn.execute(
            text(
                "SELECT grantee, table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE grantee IN ('app_user', 'app_worker', 'gw')"
            )
        ).all()
        column_grants = conn.execute(
            text(
                "SELECT grantee, table_name, column_name, privilege_type "
                "FROM information_schema.column_privileges "
                "WHERE grantee IN ('app_user', 'app_worker', 'gw') "
                "AND (grantee, table_name, privilege_type) NOT IN ("
                "  SELECT grantee, table_name, privilege_type "
                "  FROM information_schema.role_table_grants)"
            )
        ).all()

    actual: dict[Role, set[tuple[str, str]]] = {role: set() for role in SERVICE_ROLES}
    for grantee, table, privilege in rows:
        actual[Role(grantee)].add((table, privilege))
    assert actual == EXPECTED_TABLE_GRANTS
    assert column_grants == []


@pytest.mark.parametrize("role", SERVICE_ROLES)
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE sneaky (id int)",
        "SET ROLE migrator",
        "ALTER TABLE principal NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE principal DISABLE ROW LEVEL SECURITY",
        "DROP POLICY tenant_isolation ON principal",
        "SELECT * FROM alembic_version",
        "INSERT INTO tenant (slug, display_name, home_region) VALUES ('x', 'X', 'us-east-1')",
        "UPDATE principal SET status = 'active'",
        "DELETE FROM role_grant",
        "UPDATE tenant SET epoch = epoch + 1",
        "CREATE FUNCTION current_tenant_id() RETURNS uuid LANGUAGE sql AS 'SELECT NULL::uuid'",
        "CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS uuid "
        "LANGUAGE sql AS 'SELECT NULL::uuid'",
    ],
)
def test_service_roles_are_refused(database: MigratedDatabase, role: Role, statement: str):
    with (
        pytest.raises((ProgrammingError, InternalError), match=r"permission denied|must be owner"),
        database.engine(role).begin() as conn,
    ):
        conn.execute(text(statement))
