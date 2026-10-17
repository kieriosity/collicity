"""SEC-09 by structure: roles own nothing, bypass nothing and hold only listed privileges.

These tests read the catalog, so they also cover what future migrations add: a new table without
forced RLS, a new privilege or a new SECURITY DEFINER function missing from the lists below
fails here, and every widening of access shows up in review.
"""

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import InternalError, ProgrammingError

from collicity_db import Role
from collicity_db.roles import SERVICE_ROLES, DefinerRole
from collicity_testing.postgres import MigratedDatabase

OUR_SCHEMAS = ("public", "ledger", "sweep", "audit", "standing", "runtime")

# Tables that hold no tenant data and so carry no tenant RLS. Keep this list short.
GLOBAL_TABLES = {"alembic_version", "price_version", "price_item"}

# The tenant predicate each tenant table's isolation policy must use, as Postgres deparses it.
TENANT_PREDICATE = "(tenant_id = current_tenant_id())"
TENANT_PREDICATE_OVERRIDES = {"tenant": "(id = current_tenant_id())"}
# Roles a tenant_isolation policy may name: every service role, plus definer roles that RLS
# must still bind inside their functions.
TENANT_POLICY_ROLES = {r.value for r in SERVICE_ROLES} | {
    role.value for role in DefinerRole if role is not DefinerRole.SWEEPER
}

# Policies other than tenant_isolation and migrator_all: (table, name) -> (roles, command,
# permissive). A further permissive policy for a service role would widen what it sees, so
# any that name one must be RESTRICTIVE.
EXTRA_POLICIES = {
    ("reservation", "sweeper_scan"): (["sweeper"], "SELECT", "PERMISSIVE"),
    ("pending", "sweeper_scan"): (["sweeper"], "SELECT", "PERMISSIVE"),
    ("assignment", "owner_creates"): (["app_user"], "INSERT", "RESTRICTIVE"),
    ("assignment_version", "owner_drafts"): (["app_user"], "INSERT", "RESTRICTIVE"),
}

_READ_TENANCY = {("tenant", "SELECT"), ("principal", "SELECT"), ("role_grant", "SELECT")}
_READ_PRICES = {("price_version", "SELECT"), ("price_item", "SELECT")}
_READ_LEDGER = {
    (table, "SELECT")
    for table in ("budget", "ledger_account", "reservation", "reservation_account", "ledger_entry")
}
_READ_AUDIT = {("record", "SELECT"), ("seal", "SELECT")}
_READ_STANDING = {
    ("assignment", "SELECT"),
    ("assignment_version", "SELECT"),
    ("version_acceptance", "SELECT"),
}
EXPECTED_TABLE_GRANTS: dict[str, set[tuple[str, str]]] = {
    "app_user": _READ_TENANCY | _READ_PRICES | _READ_LEDGER | _READ_AUDIT | _READ_STANDING,
    "app_worker": _READ_TENANCY | _READ_PRICES | _READ_LEDGER | _READ_STANDING,
    "gw": _READ_TENANCY | _READ_PRICES | {("reservation", "SELECT")},
    "ledger_definer": {
        ("budget", "SELECT"),
        ("ledger_account", "SELECT"),
        ("ledger_account", "INSERT"),
        ("ledger_account", "UPDATE"),
        ("reservation", "SELECT"),
        ("reservation", "INSERT"),
        ("reservation", "UPDATE"),
        ("reservation_account", "SELECT"),
        ("reservation_account", "INSERT"),
        ("ledger_entry", "INSERT"),
    },
    "audit_definer": {
        (table, privilege)
        for table, privileges in {
            "record": ("SELECT", "INSERT"),
            "seal": ("SELECT", "INSERT"),
            "tenant_key": ("SELECT", "INSERT"),
            "erased": ("SELECT", "INSERT"),
            "pending": ("SELECT", "INSERT", "DELETE"),
            "chain": ("SELECT", "INSERT", "UPDATE"),
            "person_key": ("SELECT", "INSERT", "DELETE"),
            "blind_index": ("SELECT", "INSERT", "DELETE"),
        }.items()
        for privilege in privileges
    },
    "standing_definer": {("principal", "SELECT"), ("version_acceptance", "INSERT")}
    | _READ_STANDING,
    "runtime_definer": {
        ("tenant", "SELECT"),
        ("principal", "SELECT"),
        ("role_grant", "SELECT"),
        ("step_grant", "SELECT"),
        ("step_grant", "INSERT"),
    }
    | _READ_STANDING,
    "sweeper": set(),
}
EXPECTED_COLUMN_GRANTS: dict[str, set[tuple[str, str, str]]] = {
    "sweeper": {
        ("reservation", column, "SELECT") for column in ("id", "tenant_id", "state", "expires_at")
    }
    | {("pending", "tenant_id", "SELECT")},
    # The app inserts drafts, never epochs, pauses or the current version.
    "app_user": {
        ("assignment", column, "INSERT") for column in ("id", "tenant_id", "owner_id", "name")
    }
    | {
        ("assignment_version", column, "INSERT")
        for column in (
            "id",
            "tenant_id",
            "assignment_id",
            "number",
            "grants",
            "task_graph",
            "taint",
            "created_by",
        )
    },
    "standing_definer": {("assignment", "current_version_id", "UPDATE")},
    "runtime_definer": {("step_grant", "ended_at", "UPDATE")}
    | {
        (table, column, "UPDATE")
        for table in ("tenant", "principal", "assignment")
        for column in ("epoch", "runs_paused_at", "runs_paused_by")
    },
}
# Functions each role may execute, other than those it owns.
_CONTEXT = {"current_tenant_id()", "current_principal_id()"}
_AUDIT_WRITE = {
    "audit.append(jsonb,bytea,text[],bytea[])",
    "audit.person_key(uuid)",
    "audit.add_person_key(uuid,bytea)",
    "audit.blind_key()",
    "audit.add_blind_key(bytea)",
}
EXPECTED_EXECUTE: dict[str, set[str]] = {
    "app_user": _CONTEXT
    | _AUDIT_WRITE
    | {
        "ledger.reserve(uuid,uuid,uuid,bigint,uuid,text,interval)",
        "ledger.release(uuid)",
        "audit.find(text,bytea)",
        "standing.accept_version(uuid,jsonb)",
        "runtime.pause_tenant()",
        "runtime.resume_tenant()",
        "runtime.pause_principal(uuid)",
        "runtime.resume_principal(uuid)",
        "runtime.pause_assignment(uuid)",
        "runtime.resume_assignment(uuid)",
    },
    "app_worker": _CONTEXT
    | _AUDIT_WRITE
    | {
        "ledger.reserve(uuid,uuid,uuid,bigint,uuid,text,interval)",
        "ledger.release(uuid)",
        "ledger.expire(uuid)",
        "sweep.due_for_expiry(integer)",
        "audit.seal(integer)",
        "audit.verify()",
        "audit.shred_person(uuid)",
        "sweep.tenants_with_unsealed_audit(integer)",
        "runtime.issue_step_grant(uuid,uuid,uuid,uuid,text,integer,text[],boolean,interval)",
        "runtime.end_step_grant(uuid)",
    },
    "gw": _CONTEXT
    | _AUDIT_WRITE
    | {
        "ledger.release(uuid)",
        "ledger.start(uuid,uuid,uuid,bigint,interval)",
        "ledger.settle(uuid,bigint)",
        "runtime.check_step_grant(uuid)",
    },
    "ledger_definer": {"current_tenant_id()"},
    "audit_definer": {"current_tenant_id()"},
    "standing_definer": _CONTEXT | {"audit.append(jsonb,bytea,text[],bytea[])"},
    "runtime_definer": _CONTEXT | {"audit.append(jsonb,bytea,text[],bytea[])"},
    "sweeper": set(),
}
CHECKED_ROLES = tuple(EXPECTED_TABLE_GRANTS)


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


@pytest.mark.parametrize("role", DefinerRole)
def test_definer_roles_cannot_log_in_and_only_migrator_can_become_them(
    database: MigratedDatabase, role: DefinerRole
):
    with _admin(database) as conn:
        attrs = conn.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname = :r"
            ),
            {"r": role.value},
        ).one()
        assert not any(attrs)
        members = conn.execute(
            text(
                "SELECT m.member::regrole::text FROM pg_auth_members m "
                "WHERE m.roleid = CAST(:r AS regrole)"
            ),
            {"r": role.value},
        ).scalars()
        assert set(members) == {"migrator"}


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
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid "
                "        AND a.attname = 'tenant_id' AND NOT a.attisdropped) AS has_tenant "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND c.relkind IN ('r', 'p')"
            )
        ).all()
        policies = {
            (row.tablename, row.policyname): row
            for row in conn.execute(
                text(
                    "SELECT tablename, policyname, roles, cmd, permissive, qual, with_check "
                    "FROM pg_policies"
                )
            )
        }

    assert tables, "the migrations created no tables"
    for name, rls, forced, has_tenant in tables:
        if name in GLOBAL_TABLES:
            assert not has_tenant, f"{name} holds tenant data, so it can't be global"
            continue
        assert rls, f"{name} must ENABLE row level security"
        assert forced, f"{name} must FORCE row level security"
        policy = policies.get((name, "tenant_isolation"))
        assert policy is not None, f"{name} has no tenant_isolation policy"
        predicate = TENANT_PREDICATE_OVERRIDES.get(name, TENANT_PREDICATE)
        assert policy.qual == predicate, name
        assert policy.with_check == predicate, name
        assert {r.value for r in SERVICE_ROLES} <= set(policy.roles) <= TENANT_POLICY_ROLES, name

        # Any further policy is migrator's or listed above: a second permissive policy for a
        # service role would widen what it sees.
        for (table, policy_name), other in policies.items():
            if table != name or policy_name == "tenant_isolation":
                continue
            if policy_name == "migrator_all":
                assert other.roles == ["migrator"], name
                continue
            expected = EXTRA_POLICIES.get((table, policy_name))
            assert expected is not None, f"unexpected policy {policy_name} on {name}"
            assert (other.roles, other.cmd, other.permissive) == expected, policy_name


def test_table_and_column_grants_match_the_expected_lists(database: MigratedDatabase):
    roles = {"roles": list(CHECKED_ROLES)}
    with _admin(database) as conn:
        rows = conn.execute(
            text(
                "SELECT grantee, table_name, privilege_type "
                "FROM information_schema.role_table_grants WHERE grantee = ANY (:roles)"
            ),
            roles,
        ).all()
        column_rows = conn.execute(
            text(
                "SELECT grantee, table_name, column_name, privilege_type "
                "FROM information_schema.column_privileges "
                "WHERE grantee = ANY (:roles) "
                "AND (grantee, table_name, privilege_type) NOT IN ("
                "  SELECT grantee, table_name, privilege_type "
                "  FROM information_schema.role_table_grants)"
            ),
            roles,
        ).all()

    actual: dict[str, set[tuple[str, str]]] = {role: set() for role in CHECKED_ROLES}
    for grantee, table, privilege in rows:
        actual[grantee].add((table, privilege))
    assert actual == EXPECTED_TABLE_GRANTS

    actual_columns: dict[str, set[tuple[str, str, str]]] = {}
    for grantee, table, column, privilege in column_rows:
        actual_columns.setdefault(grantee, set()).add((table, column, privilege))
    assert actual_columns == EXPECTED_COLUMN_GRANTS


def _function_acl(conn: Connection) -> list[tuple[str, str, str, bool, list[str] | None]]:
    """(function, owner, grantee, security definer, config) for every EXECUTE grant."""
    return [
        tuple(row)  # type: ignore[misc]
        for row in conn.execute(
            text(
                "SELECT p.oid::regprocedure::text, p.proowner::regrole::text, "
                "       CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, "
                "       p.prosecdef, p.proconfig "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "CROSS JOIN LATERAL aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
                "WHERE n.nspname = ANY (:schemas) AND a.privilege_type = 'EXECUTE'"
            ),
            {"schemas": list(OUR_SCHEMAS)},
        )
    ]


def test_no_function_is_executable_by_public(database: MigratedDatabase):
    with _admin(database) as conn:
        public = [fn for fn, _, grantee, _, _ in _function_acl(conn) if grantee == "PUBLIC"]
    assert public == []


def test_execute_grants_match_the_expected_list(database: MigratedDatabase):
    with _admin(database) as conn:
        acl = _function_acl(conn)
    actual: dict[str, set[str]] = {role: set() for role in CHECKED_ROLES}
    for fn, owner, grantee, _, _ in acl:
        if grantee in actual and grantee != owner:
            actual[grantee].add(fn)
    assert actual == EXPECTED_EXECUTE


def test_security_definer_functions_have_a_definer_owner_and_a_pinned_search_path(
    database: MigratedDatabase,
):
    with _admin(database) as conn:
        definers = {
            (fn, owner, tuple(config or ()))
            for fn, owner, _, secdef, config in _function_acl(conn)
            if secdef
        }
    assert definers, "expected SECURITY DEFINER functions"
    for fn, owner, config in definers:
        assert owner in {r.value for r in DefinerRole}, fn
        # A definer function that resolves names through the caller's search_path can be
        # tricked into calling an attacker's object.
        assert "search_path=pg_catalog, pg_temp" in config, fn


@pytest.mark.parametrize("role", SERVICE_ROLES)
@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE sneaky (id int)",
        "SET ROLE migrator",
        "SET ROLE ledger_definer",
        "SET ROLE sweeper",
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
        "CREATE FUNCTION ledger.sneaky() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        "INSERT INTO budget (tenant_id, scope, period, limit_micro_usd) "
        "VALUES (gen_random_uuid(), 'organization', 'month', 1)",
        "UPDATE budget SET limit_micro_usd = limit_micro_usd * 10",
        "UPDATE ledger_account SET reserved_micro_usd = 0",
        "UPDATE ledger_account SET limit_micro_usd = limit_micro_usd * 10",
        "UPDATE reservation SET state = 'released'",
        "DELETE FROM reservation",
        "INSERT INTO ledger_entry (tenant_id, ledger_account_id, reservation_id, kind, "
        "reserved_delta, spent_delta) VALUES (gen_random_uuid(), gen_random_uuid(), "
        "gen_random_uuid(), 'settle', 0, 0)",
        "UPDATE price_item SET micro_usd = 0",
        "INSERT INTO price_version (label, effective_at, source) VALUES ('x', now(), 'x')",
        "SELECT ledger.period_start('day', now())",
        # The audit log: no direct writes, no changes, no reading keys except through functions.
        "INSERT INTO audit.record (id, tenant_id, occurred_at, kind, digest) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), now(), 'admin_change', decode('00', 'hex'))",
        "UPDATE audit.record SET outcome = 'ok'",
        "DELETE FROM audit.record",
        "UPDATE audit.seal SET hash = decode('00', 'hex')",
        "DELETE FROM audit.seal",
        "UPDATE audit.chain SET last_seq = 0",
        "DELETE FROM audit.pending",
        "SELECT * FROM audit.person_key",
        "DELETE FROM audit.person_key",
        "DELETE FROM audit.blind_index",
        "SELECT * FROM audit.tenant_key",
        "CREATE FUNCTION audit.sneaky() RETURNS int LANGUAGE sql AS 'SELECT 1'",
        # Standing instructions change only through acceptance; epochs only through pauses.
        "UPDATE assignment SET current_version_id = id",
        "UPDATE assignment SET epoch = 0",
        "UPDATE assignment SET runs_paused_at = NULL",
        "UPDATE assignment_version SET grants = '[]'",
        "DELETE FROM assignment_version",
        "INSERT INTO version_acceptance (version_id, tenant_id, assignment_id, accepted_by, "
        "content_hash, widening) VALUES (gen_random_uuid(), gen_random_uuid(), "
        "gen_random_uuid(), gen_random_uuid(), decode('00', 'hex'), '[]')",
        "INSERT INTO step_grant (id, tenant_id, assignment_id, version_id, owner_id, step_key, "
        "attempt, grants, dry_run, tenant_epoch, owner_epoch, assignment_epoch, expires_at) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
        "gen_random_uuid(), 's', 1, '[]', false, 0, 0, 0, now())",
        "UPDATE step_grant SET ended_at = NULL",
        "UPDATE principal SET runs_paused_at = NULL",
        "CREATE FUNCTION runtime.sneaky() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    ],
)
def test_service_roles_are_refused(database: MigratedDatabase, role: Role, statement: str):
    with (
        pytest.raises((ProgrammingError, InternalError), match=r"permission denied|must be owner"),
        database.engine(role).begin() as conn,
    ):
        conn.execute(text(statement))


_ID = "gen_random_uuid()"


@pytest.mark.parametrize(
    ("role", "statement"),
    [
        # Only callers that start work reserve; the gateway checks reservations, never makes them.
        (Role.GW, f"SELECT * FROM ledger.reserve({_ID}, {_ID}, NULL, 1, {_ID}, 'x', '1 min')"),
        # Only the gateway marks work started or records what it cost.
        (Role.APP_WORKER, f"SELECT ledger.start({_ID}, {_ID}, NULL, 1, '1 hour')"),
        (Role.APP_USER, f"SELECT ledger.start({_ID}, {_ID}, NULL, 1, '1 hour')"),
        (Role.APP_WORKER, f"SELECT ledger.settle({_ID}, 0)"),
        (Role.APP_USER, f"SELECT ledger.settle({_ID}, 0)"),
        # Expiry and the cross-tenant scan belong to the worker's sweeper.
        (Role.APP_USER, f"SELECT ledger.expire({_ID})"),
        (Role.GW, f"SELECT ledger.expire({_ID})"),
        (Role.APP_USER, "SELECT * FROM sweep.due_for_expiry(10)"),
        (Role.GW, "SELECT * FROM sweep.due_for_expiry(10)"),
        (Role.APP_WORKER, "SELECT * FROM ledger.lock_accounts(gen_random_uuid())"),
        # Sealing, verification and erasure belong to the worker; search to the console.
        (Role.GW, "SELECT audit.seal(10)"),
        (Role.APP_USER, "SELECT audit.seal(10)"),
        (Role.GW, "SELECT * FROM audit.verify()"),
        (Role.APP_USER, f"SELECT * FROM audit.shred_person({_ID})"),
        (Role.GW, f"SELECT * FROM audit.shred_person({_ID})"),
        (Role.GW, "SELECT * FROM audit.find('email', decode('00', 'hex'))"),
        (Role.APP_USER, "SELECT * FROM sweep.tenants_with_unsealed_audit(10)"),
        (Role.APP_WORKER, "SELECT audit.canonical(NULL::audit.record)"),
        # Standing instructions are read-only to the worker (SEC-12).
        (
            Role.APP_WORKER,
            "INSERT INTO assignment (id, tenant_id, owner_id, name) "
            f"VALUES ({_ID}, {_ID}, {_ID}, 'x')",
        ),
        (Role.APP_WORKER, f"SELECT standing.accept_version({_ID}, '[]')"),
        (Role.GW, f"SELECT standing.accept_version({_ID}, '[]')"),
        # Only the worker issues step grants; only the gateway checks them.
        (
            Role.GW,
            f"SELECT runtime.issue_step_grant({_ID}, {_ID}, {_ID}, NULL, 's', 1, NULL, "
            "false, '1 minute')",
        ),
        (
            Role.APP_USER,
            f"SELECT runtime.issue_step_grant({_ID}, {_ID}, {_ID}, NULL, 's', 1, NULL, "
            "false, '1 minute')",
        ),
        (Role.APP_WORKER, f"SELECT * FROM runtime.check_step_grant({_ID})"),
        (Role.APP_USER, f"SELECT * FROM runtime.check_step_grant({_ID})"),
        # The kill switch belongs to people, through the app.
        (Role.APP_WORKER, "SELECT runtime.pause_tenant()"),
        (Role.GW, "SELECT runtime.resume_tenant()"),
        (Role.APP_WORKER, f"SELECT runtime.resume_principal({_ID})"),
        (Role.APP_WORKER, "SELECT runtime.is_admin()"),
    ],
)
def test_ledger_functions_are_split_by_role(database: MigratedDatabase, role: Role, statement: str):
    with (
        pytest.raises(ProgrammingError, match=r"permission denied"),
        database.engine(role).begin() as conn,
    ):
        conn.execute(text(statement))
