"""Assignments, immutable versions, acceptance, step grants and the kill switch.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17

Technical design §3.2 (read call, step 1), §4.1 (kill switch), §4.4 and §8.2 (versions and
standing instructions), §5.2 (stable IDs).

- Standing instructions are read-only to the worker. The app inserts drafts as their owner;
  only ``standing.accept_version`` makes one current, for its owner, and audits it (SEC-12).
- A step grant is a row for one step and attempt (CON-04). ``runtime.issue_step_grant`` records
  the tenant, owner and assignment epochs itself, and only for an accepted version's own
  capabilities. ``runtime.check_step_grant`` fails as soon as any of those epochs moves.
- Pausing a tenant, a person or an assignment bumps its epoch (ASG-13), which invalidates every
  grant issued before, and notifies ``collicity_epochs`` so gateway caches drop them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICE_ROLES = "app_user, app_worker, gw"
DEFINERS = "standing_definer, runtime_definer"
TENANT_ROLES = f"{SERVICE_ROLES}, {DEFINERS}"
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"


def _force_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL TO {TENANT_ROLES} "
        "USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id())"
    )
    op.execute(
        f"CREATE POLICY migrator_all ON {table} FOR ALL TO migrator USING (true) WITH CHECK (true)"
    )


def _immutable(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION reject_change()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_change()"
    )


def upgrade() -> None:
    for role in ("standing_definer", "runtime_definer"):
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        )

    # Who paused, so a person can't lift a pause an admin set on them.
    for table in ("tenant", "principal"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN runs_paused_at timestamptz")
        op.execute(f"ALTER TABLE {table} ADD COLUMN runs_paused_by uuid")

    op.execute("""
        CREATE TABLE assignment (
            id uuid PRIMARY KEY,  -- also the agent's ID (MR-04)
            tenant_id uuid NOT NULL REFERENCES tenant (id),
            owner_id uuid NOT NULL,
            name text NOT NULL,
            current_version_id uuid,  -- set only by standing.accept_version
            epoch bigint NOT NULL DEFAULT 0,
            runs_paused_at timestamptz,
            runs_paused_by uuid,
            archived_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT assignment_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT assignment_tenant_id_owner_id_fkey
                FOREIGN KEY (tenant_id, owner_id) REFERENCES principal (tenant_id, id),
            CONSTRAINT assignment_name_check CHECK (length(name) BETWEEN 1 AND 200),
            CONSTRAINT assignment_epoch_check CHECK (epoch >= 0)
        )
    """)

    # Immutable once written. Grants hold stable resource IDs resolved at drafting time, so a
    # rename can't widen them; a changed resolution is a new version (§5.2, RUN-03).
    op.execute("""
        CREATE TABLE assignment_version (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            assignment_id uuid NOT NULL,
            number integer NOT NULL,
            grants jsonb NOT NULL,
            task_graph jsonb NOT NULL,
            taint text NOT NULL,  -- untrusted when drafted from untrusted content (SEC-12)
            content_hash bytea NOT NULL,
            created_by uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT assignment_version_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT assignment_version_tenant_id_assignment_id_id_key
                UNIQUE (tenant_id, assignment_id, id),
            CONSTRAINT assignment_version_assignment_id_number_key UNIQUE (assignment_id, number),
            CONSTRAINT assignment_version_tenant_id_assignment_id_fkey
                FOREIGN KEY (tenant_id, assignment_id) REFERENCES assignment (tenant_id, id),
            CONSTRAINT assignment_version_tenant_id_created_by_fkey
                FOREIGN KEY (tenant_id, created_by) REFERENCES principal (tenant_id, id),
            CONSTRAINT assignment_version_number_check CHECK (number > 0),
            CONSTRAINT assignment_version_grants_check CHECK (jsonb_typeof(grants) = 'array'),
            CONSTRAINT assignment_version_task_graph_check
                CHECK (jsonb_typeof(task_graph) = 'object'),
            CONSTRAINT assignment_version_taint_check CHECK (taint IN ('trusted', 'untrusted'))
        )
    """)
    op.execute("""
        CREATE FUNCTION assignment_version_hash() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.content_hash := sha256(convert_to(
                jsonb_build_array(NEW.grants, NEW.task_graph, NEW.taint)::text, 'UTF8'));
            RETURN NEW;
        END
        $$
    """)
    op.execute(
        "CREATE TRIGGER assignment_version_hash BEFORE INSERT ON assignment_version "
        "FOR EACH ROW EXECUTE FUNCTION assignment_version_hash()"
    )
    _immutable("assignment_version")

    # The current version must be one of the assignment's own.
    op.execute("""
        ALTER TABLE assignment ADD CONSTRAINT assignment_tenant_id_id_current_version_id_fkey
            FOREIGN KEY (tenant_id, id, current_version_id)
            REFERENCES assignment_version (tenant_id, assignment_id, id)
    """)

    op.execute("""
        CREATE TABLE version_acceptance (
            version_id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            assignment_id uuid NOT NULL,
            accepted_by uuid NOT NULL,
            accepted_at timestamptz NOT NULL DEFAULT now(),
            content_hash bytea NOT NULL,
            widening jsonb NOT NULL,  -- what the acceptance screen showed as widened
            CONSTRAINT version_acceptance_tenant_id_assignment_id_version_id_fkey
                FOREIGN KEY (tenant_id, assignment_id, version_id)
                REFERENCES assignment_version (tenant_id, assignment_id, id),
            CONSTRAINT version_acceptance_tenant_id_accepted_by_fkey
                FOREIGN KEY (tenant_id, accepted_by) REFERENCES principal (tenant_id, id),
            CONSTRAINT version_acceptance_widening_check CHECK (jsonb_typeof(widening) = 'array')
        )
    """)
    _immutable("version_acceptance")

    op.execute("""
        CREATE TABLE step_grant (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            assignment_id uuid NOT NULL,
            version_id uuid NOT NULL,
            owner_id uuid NOT NULL,  -- whose access the step uses
            run_id uuid,  -- references run once the run engine lands (P0.7)
            step_key text NOT NULL,
            attempt integer NOT NULL,
            grants jsonb NOT NULL,
            dry_run boolean NOT NULL,
            tenant_epoch bigint NOT NULL,
            owner_epoch bigint NOT NULL,
            assignment_epoch bigint NOT NULL,
            issued_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            ended_at timestamptz,
            CONSTRAINT step_grant_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT step_grant_tenant_id_assignment_id_version_id_fkey
                FOREIGN KEY (tenant_id, assignment_id, version_id)
                REFERENCES assignment_version (tenant_id, assignment_id, id),
            CONSTRAINT step_grant_tenant_id_owner_id_fkey
                FOREIGN KEY (tenant_id, owner_id) REFERENCES principal (tenant_id, id),
            CONSTRAINT step_grant_attempt_check CHECK (attempt > 0),
            CONSTRAINT step_grant_grants_check CHECK (jsonb_typeof(grants) = 'array')
        )
    """)

    for table in ("assignment", "assignment_version", "version_acceptance", "step_grant"):
        _force_rls(table)
    # The definers read and update tenancy rows inside their functions, still under tenant RLS.
    op.execute(f"ALTER POLICY tenant_isolation ON tenant TO {TENANT_ROLES}")
    op.execute(f"ALTER POLICY tenant_isolation ON principal TO {TENANT_ROLES}")
    op.execute(f"ALTER POLICY tenant_isolation ON role_grant TO {TENANT_ROLES}")

    # The app drafts only as the owner, and only for live assignments it owns.
    op.execute("""
        CREATE POLICY owner_creates ON assignment AS RESTRICTIVE FOR INSERT TO app_user
        WITH CHECK (owner_id = current_principal_id())
    """)
    op.execute("""
        CREATE POLICY owner_drafts ON assignment_version AS RESTRICTIVE FOR INSERT TO app_user
        WITH CHECK (
            created_by = current_principal_id()
            AND EXISTS (
                SELECT 1 FROM assignment a
                WHERE a.id = assignment_id
                  AND a.owner_id = current_principal_id()
                  AND a.archived_at IS NULL
            )
        )
    """)

    standing = "assignment, assignment_version, version_acceptance"
    op.execute(f"GRANT SELECT ON {standing} TO app_user, app_worker")
    op.execute("GRANT INSERT (id, tenant_id, owner_id, name) ON assignment TO app_user")
    op.execute(
        "GRANT INSERT (id, tenant_id, assignment_id, number, grants, task_graph, taint, "
        "created_by) ON assignment_version TO app_user"
    )

    op.execute(f"GRANT USAGE ON SCHEMA public, audit TO {DEFINERS}")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION current_tenant_id(), current_principal_id() TO {DEFINERS}"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION audit.append(jsonb, bytea, text[], bytea[]) TO {DEFINERS}"
    )
    op.execute(f"GRANT SELECT ON principal, {standing} TO standing_definer")
    op.execute("GRANT INSERT ON version_acceptance TO standing_definer")
    op.execute("GRANT UPDATE (current_version_id) ON assignment TO standing_definer")
    op.execute(
        f"GRANT SELECT ON tenant, principal, role_grant, {standing}, step_grant TO runtime_definer"
    )
    op.execute("GRANT INSERT ON step_grant TO runtime_definer")
    op.execute("GRANT UPDATE (ended_at) ON step_grant TO runtime_definer")
    op.execute(
        "GRANT UPDATE (epoch, runs_paused_at, runs_paused_by) "
        "ON tenant, principal, assignment TO runtime_definer"
    )

    op.execute("CREATE SCHEMA standing AUTHORIZATION standing_definer")
    op.execute("CREATE SCHEMA runtime AUTHORIZATION runtime_definer")
    op.execute("SET ROLE standing_definer")
    _create_standing_functions()
    op.execute("RESET ROLE")
    op.execute("SET ROLE runtime_definer")
    _create_runtime_functions()
    op.execute("RESET ROLE")


def _create_standing_functions() -> None:
    # The one way a draft becomes current (§8.2): its owner, a person, accepts it.
    op.execute(f"""
        CREATE FUNCTION standing.accept_version(p_version_id uuid, p_widening jsonb)
        RETURNS void
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := public.current_tenant_id();
            v_person uuid := public.current_principal_id();
            v record;
        BEGIN
            IF v_tenant IS NULL OR v_person IS NULL THEN
                RAISE EXCEPTION 'accepting a version needs a signed-in person'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.principal p
                WHERE p.id = v_person AND p.kind = 'person' AND p.status = 'active'
            ) THEN
                RAISE EXCEPTION 'only an active person can accept a version'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;

            SELECT ver.id, ver.assignment_id, ver.number, ver.content_hash,
                   a.owner_id, a.archived_at
            INTO v
            FROM public.assignment_version ver
            JOIN public.assignment a ON a.id = ver.assignment_id
            WHERE ver.id = p_version_id
            FOR UPDATE OF a;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'version % not found', p_version_id
                    USING ERRCODE = 'no_data_found';
            END IF;
            IF v.owner_id <> v_person THEN
                RAISE EXCEPTION 'only the assignment''s owner can accept its versions'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF v.archived_at IS NOT NULL THEN
                RAISE EXCEPTION 'the assignment is archived'
                    USING ERRCODE = 'object_not_in_prerequisite_state';
            END IF;
            IF EXISTS (SELECT 1 FROM public.version_acceptance x WHERE x.version_id = v.id) THEN
                RAISE EXCEPTION 'version % is already accepted', v.id
                    USING ERRCODE = 'unique_violation';
            END IF;

            INSERT INTO public.version_acceptance
                (version_id, tenant_id, assignment_id, accepted_by, content_hash, widening)
            VALUES
                (v.id, v_tenant, v.assignment_id, v_person, v.content_hash,
                 coalesce(p_widening, '[]'::jsonb));
            UPDATE public.assignment SET current_version_id = v.id WHERE id = v.assignment_id;

            PERFORM audit.append(
                jsonb_build_object(
                    'id', gen_random_uuid(),
                    'kind', 'version_accepted',
                    'outcome', 'accepted',
                    'actor_id', v_person,
                    'user_id', v_person,
                    'agent_id', v.assignment_id,
                    'detail', jsonb_build_object(
                        'version_id', v.id,
                        'number', v.number,
                        'content_hash', encode(v.content_hash, 'hex'),
                        'widened', jsonb_array_length(coalesce(p_widening, '[]'::jsonb))
                    )
                ),
                NULL, NULL, NULL
            );
        END
        $$
    """)
    op.execute("GRANT USAGE ON SCHEMA standing TO app_user")
    op.execute("GRANT EXECUTE ON FUNCTION standing.accept_version(uuid, jsonb) TO app_user")


def _create_runtime_functions() -> None:
    op.execute("""
        CREATE FUNCTION runtime.require_tenant() RETURNS uuid
        LANGUAGE plpgsql STABLE
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_tenant uuid := public.current_tenant_id();
        BEGIN
            IF v_tenant IS NULL THEN
                RAISE EXCEPTION 'runtime functions need a tenant context'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN v_tenant;
        END
        $$
    """)

    op.execute("""
        CREATE FUNCTION runtime.is_admin() RETURNS boolean
        LANGUAGE sql STABLE
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.role_grant g
                WHERE g.principal_id = public.current_principal_id()
                  AND g.role IN ('owner', 'admin')
            )
        $$
    """)

    # Issue a grant for one step and attempt. The epochs come from the database, never the
    # caller, and the capabilities only from the accepted version.
    op.execute(f"""
        CREATE FUNCTION runtime.issue_step_grant(
            p_id uuid,
            p_assignment_id uuid,
            p_version_id uuid,
            p_run_id uuid,
            p_step_key text,
            p_attempt integer,
            p_actions text[],
            p_dry_run boolean,
            p_ttl interval
        )
        RETURNS text
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := runtime.require_tenant();
            t record;
            a record;
            o record;
            v record;
            v_grants jsonb;
        BEGIN
            IF p_ttl IS NULL OR p_ttl < interval '1 second' OR p_ttl > interval '1 hour' THEN
                RAISE EXCEPTION 'a step grant lives between 1 second and 1 hour'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            SELECT status, epoch, runs_paused_at INTO t FROM public.tenant WHERE id = v_tenant;
            IF t.status <> 'active' THEN RETURN 'tenant_inactive'; END IF;
            IF t.runs_paused_at IS NOT NULL THEN RETURN 'tenant_paused'; END IF;

            SELECT owner_id, epoch, runs_paused_at, archived_at INTO a
            FROM public.assignment WHERE id = p_assignment_id;
            IF NOT FOUND THEN RETURN 'not_found'; END IF;
            IF a.archived_at IS NOT NULL THEN RETURN 'archived'; END IF;
            IF a.runs_paused_at IS NOT NULL THEN RETURN 'assignment_paused'; END IF;

            SELECT status, epoch, runs_paused_at INTO o
            FROM public.principal WHERE id = a.owner_id;
            IF o.status <> 'active' THEN RETURN 'owner_inactive'; END IF;
            IF o.runs_paused_at IS NOT NULL THEN RETURN 'owner_paused'; END IF;

            SELECT ver.grants INTO v FROM public.assignment_version ver
            WHERE ver.id = p_version_id AND ver.assignment_id = p_assignment_id;
            IF NOT FOUND THEN RETURN 'not_found'; END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.version_acceptance x WHERE x.version_id = p_version_id
            ) THEN
                RETURN 'not_accepted';
            END IF;

            SELECT coalesce(jsonb_agg(g), '[]'::jsonb) INTO v_grants
            FROM jsonb_array_elements(v.grants) AS g
            WHERE p_actions IS NULL OR g ->> 'action' = ANY (p_actions);
            IF p_actions IS NOT NULL AND EXISTS (
                SELECT 1 FROM unnest(p_actions) AS wanted
                WHERE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(v.grants) AS g
                    WHERE g ->> 'action' = wanted
                )
            ) THEN
                RETURN 'not_in_version';
            END IF;

            INSERT INTO public.step_grant (
                id, tenant_id, assignment_id, version_id, owner_id, run_id, step_key, attempt,
                grants, dry_run, tenant_epoch, owner_epoch, assignment_epoch, expires_at
            ) VALUES (
                p_id, v_tenant, p_assignment_id, p_version_id, a.owner_id, p_run_id, p_step_key,
                p_attempt, v_grants, coalesce(p_dry_run, false), t.epoch, o.epoch, a.epoch,
                now() + p_ttl
            );
            RETURN 'issued';
        END
        $$
    """)

    # The gateway's check before every call (§5.1 step 1). Any moved epoch fails it.
    op.execute(f"""
        CREATE FUNCTION runtime.check_step_grant(p_id uuid)
        RETURNS TABLE (
            status text, owner_id uuid, assignment_id uuid, version_id uuid, run_id uuid,
            grants jsonb, dry_run boolean
        )
        LANGUAGE plpgsql STABLE {DEFINER}
        AS $$
        #variable_conflict use_column
        DECLARE
            g public.step_grant;
            t record;
            o record;
            a record;
            v_status text;
        BEGIN
            PERFORM runtime.require_tenant();
            SELECT * INTO g FROM public.step_grant s WHERE s.id = p_id;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'not_found'::text, NULL::uuid, NULL::uuid, NULL::uuid,
                    NULL::uuid, NULL::jsonb, NULL::boolean;
                RETURN;
            END IF;
            SELECT x.status, x.epoch, x.runs_paused_at INTO t
            FROM public.tenant x WHERE x.id = g.tenant_id;
            SELECT x.status, x.epoch, x.runs_paused_at INTO o
            FROM public.principal x WHERE x.id = g.owner_id;
            SELECT x.epoch, x.runs_paused_at, x.archived_at INTO a
            FROM public.assignment x WHERE x.id = g.assignment_id;

            v_status := CASE
                WHEN g.ended_at IS NOT NULL THEN 'ended'
                WHEN g.expires_at <= now() THEN 'expired'
                WHEN t.status <> 'active' THEN 'tenant_inactive'
                WHEN t.epoch <> g.tenant_epoch OR t.runs_paused_at IS NOT NULL
                    THEN 'tenant_paused'
                WHEN o.status <> 'active' THEN 'owner_inactive'
                WHEN o.epoch <> g.owner_epoch OR o.runs_paused_at IS NOT NULL
                    THEN 'owner_paused'
                WHEN a.archived_at IS NOT NULL THEN 'archived'
                WHEN a.epoch <> g.assignment_epoch OR a.runs_paused_at IS NOT NULL
                    THEN 'assignment_paused'
                ELSE 'valid'
            END;
            IF v_status <> 'valid' THEN
                RETURN QUERY SELECT v_status, NULL::uuid, NULL::uuid, NULL::uuid, NULL::uuid,
                    NULL::jsonb, NULL::boolean;
                RETURN;
            END IF;
            RETURN QUERY SELECT v_status, g.owner_id, g.assignment_id, g.version_id, g.run_id,
                g.grants, g.dry_run;
        END
        $$
    """)

    op.execute(f"""
        CREATE FUNCTION runtime.end_step_grant(p_id uuid) RETURNS boolean
        LANGUAGE plpgsql {DEFINER}
        AS $$
        BEGIN
            PERFORM runtime.require_tenant();
            UPDATE public.step_grant SET ended_at = now() WHERE id = p_id AND ended_at IS NULL;
            RETURN FOUND;
        END
        $$
    """)

    # The kill switch (ASG-13). A pause bumps the epoch, so every grant issued before it fails
    # its next check; a resume clears the pause but never restores an old epoch.
    op.execute("""
        CREATE FUNCTION runtime.kill_switch_audit(p_scope text, p_target uuid, p_action text)
        RETURNS void
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            PERFORM audit.append(
                jsonb_build_object(
                    'id', gen_random_uuid(),
                    'kind', 'kill_switch',
                    'outcome', p_action,
                    'actor_id', public.current_principal_id(),
                    'detail', jsonb_build_object('scope', p_scope, 'target', p_target)
                ),
                NULL, NULL, NULL
            );
            PERFORM pg_notify(
                'collicity_epochs',
                jsonb_build_object(
                    'tenant', public.current_tenant_id(), 'scope', p_scope, 'target', p_target
                )::text
            );
        END
        $$
    """)

    for scope, table, may_pause, may_resume in (
        (
            "tenant",
            "tenant",
            "runtime.is_admin()",
            "runtime.is_admin()",
        ),
        (
            "principal",
            "principal",
            "runtime.is_admin() OR p_target = v_actor",
            "runtime.is_admin() OR (p_target = v_actor AND r.runs_paused_by = v_actor)",
        ),
        (
            "assignment",
            "assignment",
            "runtime.is_admin() OR r.owner_id = v_actor",
            "runtime.is_admin() OR (r.owner_id = v_actor AND r.runs_paused_by = v_actor)",
        ),
    ):
        target_param = "" if scope == "tenant" else "p_target uuid"
        target_value = "runtime.require_tenant()" if scope == "tenant" else "p_target"
        owner_column = ", owner_id" if scope == "assignment" else ""
        for action, allowed, change in (
            (
                "pause",
                may_pause,
                "epoch = epoch + 1, runs_paused_at = coalesce(runs_paused_at, now()), "
                "runs_paused_by = coalesce(runs_paused_by, v_actor)",
            ),
            ("resume", may_resume, "runs_paused_at = NULL, runs_paused_by = NULL"),
        ):
            op.execute(f"""
                CREATE FUNCTION runtime.{action}_{scope}({target_param}) RETURNS void
                LANGUAGE plpgsql {DEFINER}
                AS $$
                DECLARE
                    v_actor uuid := public.current_principal_id();
                    r record;
                BEGIN
                    PERFORM runtime.require_tenant();
                    IF v_actor IS NULL THEN
                        RAISE EXCEPTION 'the kill switch needs a signed-in person'
                            USING ERRCODE = 'insufficient_privilege';
                    END IF;
                    SELECT runs_paused_by{owner_column} INTO r
                    FROM public.{table} WHERE id = {target_value} FOR UPDATE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION '% not found', '{scope}' USING ERRCODE = 'no_data_found';
                    END IF;
                    IF NOT ({allowed}) THEN
                        RAISE EXCEPTION 'not allowed to {action} this {scope}''s runs'
                            USING ERRCODE = 'insufficient_privilege';
                    END IF;
                    UPDATE public.{table} SET {change} WHERE id = {target_value};
                    PERFORM runtime.kill_switch_audit('{scope}', {target_value}, '{action}');
                END
                $$
            """)
            signature = "" if scope == "tenant" else "uuid"
            op.execute(
                f"GRANT EXECUTE ON FUNCTION runtime.{action}_{scope}({signature}) TO app_user"
            )

    op.execute("GRANT USAGE ON SCHEMA runtime TO app_user, app_worker, gw")
    issue = (
        "runtime.issue_step_grant(uuid, uuid, uuid, uuid, text, integer, text[], boolean, interval)"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION {issue} TO app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION runtime.end_step_grant(uuid) TO app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION runtime.check_step_grant(uuid) TO gw")


def downgrade() -> None:
    raise NotImplementedError("Collicity migrations are forward-only")
