"""Tenancy: tenant, principal, role_grant, with tenant row-level security (SEC-09).

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICE_ROLES = "app_user, app_worker, gw"


def _force_rls(table: str, predicate: str) -> None:
    """Force RLS on ``table``: service roles see rows matching ``predicate``; migrator sees all.

    migrator owns the table and could turn RLS off anyway; its policy only keeps data
    migrations from silently matching no rows.
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL TO {SERVICE_ROLES} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f"CREATE POLICY migrator_all ON {table} FOR ALL TO migrator USING (true) WITH CHECK (true)"
    )


def upgrade() -> None:
    # Nothing for PUBLIC; each service role gets exactly what a migration grants it.
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {SERVICE_ROLES}")

    # The tenant and acting person, set per transaction by collicity_db.session. An unset or
    # empty setting is NULL, so every tenant predicate is false and tables read as empty.
    op.execute("""
        CREATE FUNCTION current_tenant_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$
    """)
    op.execute("""
        CREATE FUNCTION current_principal_id() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT nullif(current_setting('app.principal_id', true), '')::uuid $$
    """)
    op.execute("REVOKE ALL ON FUNCTION current_tenant_id(), current_principal_id() FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION current_tenant_id(), current_principal_id() TO {SERVICE_ROLES}"
    )

    op.execute("""
        CREATE TABLE tenant (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            slug text NOT NULL,
            display_name text NOT NULL,
            home_region text NOT NULL,
            status text NOT NULL DEFAULT 'active',
            epoch bigint NOT NULL DEFAULT 0,
            workos_organization_id text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT tenant_slug_key UNIQUE (slug),
            CONSTRAINT tenant_workos_organization_id_key UNIQUE (workos_organization_id),
            CONSTRAINT tenant_slug_check CHECK (slug ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'),
            CONSTRAINT tenant_home_region_check CHECK (home_region ~ '^[a-z]{2}(?:-[a-z]+)+-[0-9]+$'),
            CONSTRAINT tenant_status_check CHECK (status IN ('active', 'suspended')),
            CONSTRAINT tenant_epoch_check CHECK (epoch >= 0)
        )
    """)
    _force_rls("tenant", "id = current_tenant_id()")

    op.execute("""
        CREATE TABLE principal (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenant (id),
            kind text NOT NULL,
            status text NOT NULL DEFAULT 'active',
            epoch bigint NOT NULL DEFAULT 0,
            idp_subject text,
            email text,
            display_name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            -- Lets child tables reference (tenant_id, principal_id), so no row can point at
            -- another tenant's principal.
            CONSTRAINT principal_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT principal_tenant_id_idp_subject_key UNIQUE (tenant_id, idp_subject),
            CONSTRAINT principal_kind_check CHECK (kind IN ('person', 'service')),
            CONSTRAINT principal_status_check
                CHECK (status IN ('active', 'suspended', 'deprovisioned')),
            CONSTRAINT principal_epoch_check CHECK (epoch >= 0)
        )
    """)
    _force_rls("principal", "tenant_id = current_tenant_id()")

    # ADM-01 roles.
    op.execute("""
        CREATE TABLE role_grant (
            tenant_id uuid NOT NULL,
            principal_id uuid NOT NULL,
            role text NOT NULL,
            granted_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT role_grant_pkey PRIMARY KEY (tenant_id, principal_id, role),
            CONSTRAINT role_grant_tenant_id_principal_id_fkey
                FOREIGN KEY (tenant_id, principal_id) REFERENCES principal (tenant_id, id),
            CONSTRAINT role_grant_role_check CHECK (
                role IN ('owner', 'admin', 'security_auditor', 'builder', 'member', 'viewer')
            )
        )
    """)
    _force_rls("role_grant", "tenant_id = current_tenant_id()")

    # Read-only until sign-in and directory sync (P0.1b) define who writes what.
    op.execute(f"GRANT SELECT ON tenant, principal, role_grant TO {SERVICE_ROLES}")


def downgrade() -> None:
    raise NotImplementedError("Collicity migrations are forward-only")
