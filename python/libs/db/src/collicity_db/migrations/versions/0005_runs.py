"""Runs: states, transitions, admission, the run kill switch and the outbox.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-17

Technical design §4.1 and §4.3; spec §7.5 (RUN-01 to RUN-09) and §7.6 (run states).

- Postgres owns every run's state. ``state``, ``state_version`` and ``continuation`` change only
  through ``runtime.transition`` and the runtime's own functions, which check the version, allow
  only the §7.6 transitions and write outbox events in the same transaction.
- Outcomes are honest by construction (RUN-08): a run ends Cancelled or Failed only if it
  applied no side effects, and Rolled back or Completed with partial side effects only if it did.
- Only admission creates runs (RUN-09). It drops duplicate triggers (RUN-02), applies the
  assignment's concurrency policy (RUN-01), admits runs that carry a decided approval first, and
  parks a run in Paused: re-acceptance when no accepted version exists (RUN-03).
- The outbox gives each tenant's events a sequence at publish time, so an event that commits
  late is never skipped; the relay delivers at least once.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICE_ROLES = "app_user, app_worker, gw"
TENANT_ROLES = f"{SERVICE_ROLES}, runtime_definer"
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"

STATES = (
    "queued",
    "running",
    "waiting_person",
    "waiting_timer",
    "paused_budget",
    "paused_reauthorization",
    "paused_reacceptance",
    "paused_outcome_unknown",
    "paused_by_user",
    "succeeded",
    "failed",
    "rolled_back",
    "completed_partial",
    "cancelled",
)
TERMINAL = ("succeeded", "failed", "rolled_back", "completed_partial", "cancelled")
WAITING = ("waiting_person", "waiting_timer")
RESUMABLE_PAUSES = (
    "paused_budget",
    "paused_reauthorization",
    "paused_outcome_unknown",
    "paused_by_user",
)

# §7.6: every allowed (from, to). Anything else is refused.
TRANSITIONS = (
    [("queued", to) for to in ("running", "paused_by_user", "cancelled")]
    + [
        ("running", to)
        for to in (
            "running",  # a checkpoint: new continuation, same segment
            *WAITING,
            *RESUMABLE_PAUSES,
            *TERMINAL,
        )
    ]
    + [(w, to) for w in WAITING for to in ("running", "paused_by_user", "cancelled")]
    + [("paused_reacceptance", to) for to in ("queued", "cancelled")]
    + [
        (p, to)
        for p in RESUMABLE_PAUSES
        for to in ("running", "queued", "paused_by_user", "cancelled", "completed_partial")
        if p != to
    ]
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


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


def upgrade() -> None:
    # RUN-01: each assignment's concurrency policy. The default runs per-item triggers
    # concurrently up to 5 and queues the rest.
    op.execute("""
        ALTER TABLE assignment
            ADD COLUMN concurrency_policy text NOT NULL DEFAULT 'concurrent',
            ADD COLUMN max_running integer NOT NULL DEFAULT 5,
            ADD COLUMN max_queued integer NOT NULL DEFAULT 100,
            ADD CONSTRAINT assignment_concurrency_policy_check
                CHECK (concurrency_policy IN ('skip', 'queue', 'replace', 'concurrent')),
            ADD CONSTRAINT assignment_max_running_check CHECK (max_running > 0),
            ADD CONSTRAINT assignment_max_queued_check CHECK (max_queued >= 0)
    """)
    op.execute(
        "GRANT INSERT (concurrency_policy, max_running, max_queued) ON assignment TO app_user"
    )

    # RUN-02: one row per trigger event and assignment; the key is unique while it is kept.
    op.execute("""
        CREATE TABLE trigger_event (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            assignment_id uuid NOT NULL,
            kind text NOT NULL,
            dedup_key text NOT NULL,
            event_at timestamptz,
            received_at timestamptz NOT NULL DEFAULT now(),
            keep_until timestamptz NOT NULL,
            outcome text NOT NULL,
            run_id uuid,
            CONSTRAINT trigger_event_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT trigger_event_tenant_id_assignment_id_dedup_key_key
                UNIQUE (tenant_id, assignment_id, dedup_key),
            CONSTRAINT trigger_event_tenant_id_assignment_id_fkey
                FOREIGN KEY (tenant_id, assignment_id) REFERENCES assignment (tenant_id, id),
            CONSTRAINT trigger_event_kind_check
                CHECK (kind IN ('schedule', 'mail', 'webhook', 'manual', 'action')),
            CONSTRAINT trigger_event_outcome_check CHECK (outcome IN (
                'admitted', 'awaiting_acceptance', 'skipped', 'queue_full'
            ))
        )
    """)

    states = _quoted(STATES)
    terminal = _quoted(TERMINAL)
    op.execute(f"""
        CREATE TABLE run (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            assignment_id uuid NOT NULL,
            version_id uuid,  -- NULL only while Paused: re-acceptance, before acceptance
            owner_id uuid NOT NULL,
            trigger_event_id uuid NOT NULL,
            state text NOT NULL,
            state_version bigint NOT NULL DEFAULT 0,
            segment_no integer NOT NULL DEFAULT 0,
            continuation jsonb NOT NULL DEFAULT '{{}}',
            priority smallint NOT NULL DEFAULT 0,  -- 1 carries a decided approval (RUN-01)
            epoch bigint NOT NULL DEFAULT 0,
            effects_applied integer NOT NULL DEFAULT 0,
            started_at timestamptz,  -- first entered Running (RUN-01)
            paused_at timestamptz,
            paused_by uuid,
            cancel_requested_at timestamptz,  -- cancel at the next step boundary
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz,
            CONSTRAINT run_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT run_tenant_id_assignment_id_fkey
                FOREIGN KEY (tenant_id, assignment_id) REFERENCES assignment (tenant_id, id),
            CONSTRAINT run_tenant_id_assignment_id_version_id_fkey
                FOREIGN KEY (tenant_id, assignment_id, version_id)
                REFERENCES assignment_version (tenant_id, assignment_id, id),
            CONSTRAINT run_tenant_id_owner_id_fkey
                FOREIGN KEY (tenant_id, owner_id) REFERENCES principal (tenant_id, id),
            CONSTRAINT run_tenant_id_trigger_event_id_fkey
                FOREIGN KEY (tenant_id, trigger_event_id) REFERENCES trigger_event (tenant_id, id),
            CONSTRAINT run_state_check CHECK (state IN ({states})),
            CONSTRAINT run_ended_check CHECK ((ended_at IS NULL) = (state NOT IN ({terminal}))),
            -- Every run outside Paused: re-acceptance runs a version.
            CONSTRAINT run_version_check
                CHECK (version_id IS NOT NULL OR state = 'paused_reacceptance'),
            CONSTRAINT run_effects_applied_check CHECK (effects_applied >= 0),
            CONSTRAINT run_continuation_check CHECK (jsonb_typeof(continuation) = 'object')
        )
    """)
    op.execute("CREATE INDEX run_assignment_id_state_idx ON run (assignment_id, state)")
    op.execute(
        "CREATE INDEX run_assignment_id_priority_created_at_idx "
        "ON run (assignment_id, priority DESC, created_at, id) WHERE state = 'queued'"
    )
    op.execute("""
        ALTER TABLE trigger_event ADD CONSTRAINT trigger_event_tenant_id_run_id_fkey
            FOREIGN KEY (tenant_id, run_id) REFERENCES run (tenant_id, id)
    """)

    # Step grants now also record the run's epoch, and a run's pause ends them.
    op.execute("ALTER TABLE step_grant ADD COLUMN run_epoch bigint")
    op.execute("""
        ALTER TABLE step_grant ADD CONSTRAINT step_grant_tenant_id_run_id_fkey
            FOREIGN KEY (tenant_id, run_id) REFERENCES run (tenant_id, id)
    """)
    op.execute("""
        ALTER TABLE step_grant ADD CONSTRAINT step_grant_run_epoch_check
            CHECK ((run_id IS NULL) = (run_epoch IS NULL))
    """)

    # Events carry identifiers only (§9). seq is assigned when the relay publishes.
    op.execute("""
        CREATE TABLE outbox_event (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenant (id),
            kind text NOT NULL,
            subject_id uuid NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            seq bigint,
            published_at timestamptz,
            delivered_at timestamptz,
            CONSTRAINT outbox_event_tenant_id_seq_key UNIQUE (tenant_id, seq),
            CONSTRAINT outbox_event_published_check CHECK ((seq IS NULL) = (published_at IS NULL)),
            CONSTRAINT outbox_event_delivered_check
                CHECK (delivered_at IS NULL OR published_at IS NOT NULL),
            CONSTRAINT outbox_event_payload_check CHECK (jsonb_typeof(payload) = 'object')
        )
    """)
    op.execute(
        "CREATE INDEX outbox_event_tenant_id_created_at_idx "
        "ON outbox_event (tenant_id, created_at, id) WHERE delivered_at IS NULL"
    )
    op.execute("""
        CREATE TABLE outbox_cursor (
            tenant_id uuid PRIMARY KEY REFERENCES tenant (id),
            last_seq bigint NOT NULL
        )
    """)

    for table in ("trigger_event", "run", "outbox_event", "outbox_cursor"):
        _force_rls(table)
    # standing.accept_version announces acceptances, so parked runs can be requeued.
    op.execute(f"ALTER POLICY tenant_isolation ON outbox_event TO {TENANT_ROLES}, standing_definer")

    op.execute("GRANT SELECT ON run TO app_user, app_worker")
    op.execute("GRANT SELECT ON trigger_event, outbox_event TO app_worker")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON trigger_event, run, outbox_event, outbox_cursor "
        "TO runtime_definer"
    )
    op.execute("GRANT UPDATE (current_version_id) ON assignment TO runtime_definer")
    op.execute("GRANT UPDATE (run_epoch) ON step_grant TO runtime_definer")
    op.execute("GRANT INSERT ON outbox_event TO standing_definer")

    op.execute("CREATE POLICY sweeper_scan ON outbox_event FOR SELECT TO sweeper USING (true)")
    op.execute("GRANT SELECT (tenant_id, delivered_at) ON outbox_event TO sweeper")

    op.execute("SET ROLE runtime_definer")
    _create_run_functions()
    _replace_step_grant_functions()
    op.execute("RESET ROLE")

    op.execute("SET ROLE standing_definer")
    _announce_acceptances()
    op.execute("RESET ROLE")

    op.execute("SET ROLE sweeper")
    op.execute(f"""
        CREATE FUNCTION sweep.tenants_with_outbox(p_limit integer) RETURNS SETOF uuid
        LANGUAGE sql STABLE {DEFINER}
        AS $$
            SELECT DISTINCT e.tenant_id FROM public.outbox_event e
            WHERE e.delivered_at IS NULL
            LIMIT least(greatest(p_limit, 0), 1000)
        $$
    """)
    op.execute("GRANT EXECUTE ON FUNCTION sweep.tenants_with_outbox(integer) TO app_worker")
    op.execute("RESET ROLE")


def _create_run_functions() -> None:
    allowed = ",\n                ".join(f"('{a}', '{b}')" for a, b in TRANSITIONS)
    op.execute(f"""
        CREATE FUNCTION runtime.run_transition_allowed(p_from text, p_to text) RETURNS boolean
        LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM (VALUES
                {allowed}
                ) AS t (from_state, to_state)
                WHERE t.from_state = p_from AND t.to_state = p_to
            )
        $$
    """)

    op.execute("""
        CREATE FUNCTION runtime.emit(p_kind text, p_subject uuid, p_payload jsonb) RETURNS void
        LANGUAGE sql
        SET search_path = pg_catalog, pg_temp
        AS $$
            INSERT INTO public.outbox_event (tenant_id, kind, subject_id, payload)
            VALUES (public.current_tenant_id(), p_kind, p_subject, p_payload)
        $$
    """)

    # Start queued runs while the assignment has room and nothing above it is paused (RUN-01).
    # Callers hold the assignment's row lock, which serializes admission and promotion.
    op.execute("""
        CREATE FUNCTION runtime.promote(p_assignment uuid) RETURNS integer
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            a record;
            q public.run;
            v_running integer;
            v_started integer := 0;
        BEGIN
            SELECT x.max_running, x.owner_id, x.runs_paused_at, x.archived_at INTO a
            FROM public.assignment x WHERE x.id = p_assignment;
            IF a.runs_paused_at IS NOT NULL OR a.archived_at IS NOT NULL
               OR EXISTS (SELECT 1 FROM public.tenant t
                          WHERE t.id = public.current_tenant_id()
                            AND (t.runs_paused_at IS NOT NULL OR t.status <> 'active'))
               OR EXISTS (SELECT 1 FROM public.principal p
                          WHERE p.id = a.owner_id
                            AND (p.runs_paused_at IS NOT NULL OR p.status <> 'active')) THEN
                RETURN 0;
            END IF;
            SELECT count(*) INTO v_running FROM public.run r
            WHERE r.assignment_id = p_assignment AND r.state = 'running';
            WHILE v_running < a.max_running LOOP
                SELECT * INTO q FROM public.run r
                WHERE r.assignment_id = p_assignment AND r.state = 'queued'
                ORDER BY r.priority DESC, r.created_at, r.id
                LIMIT 1
                FOR UPDATE SKIP LOCKED;
                EXIT WHEN NOT FOUND;
                PERFORM runtime.move_run(q, 'running', NULL, 'admitted', NULL);
                v_running := v_running + 1;
                v_started := v_started + 1;
            END LOOP;
            RETURN v_started;
        END
        $$
    """)

    terminal = _quoted(TERMINAL)
    # The one place a run's state changes. Callers lock the assignment, then the run.
    op.execute(f"""
        CREATE FUNCTION runtime.move_run(
            r public.run, p_to text, p_continuation jsonb, p_reason text, p_actor uuid
        )
        RETURNS bigint
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_version bigint;
            v_segment integer;
            v_checkpoint boolean := r.state = 'running' AND p_to = 'running';
        BEGIN
            IF NOT runtime.run_transition_allowed(r.state, p_to) THEN
                RAISE EXCEPTION 'a run can''t go from % to %', r.state, p_to
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            -- RUN-08: outcomes say what happened to the world.
            IF p_to IN ('cancelled', 'failed') AND r.effects_applied > 0 THEN
                RAISE EXCEPTION 'a run that applied side effects can''t end %', p_to
                    USING ERRCODE = 'check_violation';
            END IF;
            IF p_to IN ('rolled_back', 'completed_partial') AND r.effects_applied = 0 THEN
                RAISE EXCEPTION 'a run that applied no side effects can''t end %', p_to
                    USING ERRCODE = 'check_violation';
            END IF;
            -- RUN-01: only a run that never executed a step goes back to the queue.
            IF p_to = 'queued' AND r.started_at IS NOT NULL THEN
                RAISE EXCEPTION 'a run that has started resumes; it doesn''t requeue'
                    USING ERRCODE = 'check_violation';
            END IF;

            UPDATE public.run SET
                state = p_to,
                state_version = state_version + 1,
                continuation = coalesce(p_continuation, continuation),
                segment_no = CASE WHEN p_to = 'running' AND NOT v_checkpoint
                                  THEN segment_no + 1 ELSE segment_no END,
                started_at = CASE WHEN p_to = 'running' THEN coalesce(started_at, now())
                                  ELSE started_at END,
                paused_at = CASE WHEN p_to LIKE 'paused%' THEN coalesce(paused_at, now()) END,
                paused_by = CASE WHEN p_to LIKE 'paused%' THEN coalesce(paused_by, p_actor) END,
                ended_at = CASE WHEN p_to IN ({terminal}) THEN now() END,
                updated_at = now()
            WHERE id = r.id
            RETURNING state_version, segment_no INTO v_version, v_segment;

            IF NOT v_checkpoint THEN
                PERFORM runtime.emit('run.state_changed', r.id, jsonb_build_object(
                    'from', r.state, 'to', p_to, 'version', v_version, 'reason', p_reason));
            END IF;
            IF p_to = 'running' AND NOT v_checkpoint THEN
                -- The relay starts workflow run:{{id}}:{{segment}}; the ID makes it idempotent.
                PERFORM runtime.emit('run.segment_ready', r.id, jsonb_build_object(
                    'segment', v_segment, 'version', v_version));
            END IF;
            IF r.state IN ('queued', 'running') AND p_to NOT IN ('queued', 'running') THEN
                PERFORM runtime.promote(r.assignment_id);
            END IF;
            RETURN v_version;
        END
        $$
    """)

    # The worker's transition, with optimistic concurrency on state_version.
    op.execute(f"""
        CREATE FUNCTION runtime.transition(
            p_run uuid, p_expected_version bigint, p_to text, p_continuation jsonb, p_reason text
        )
        RETURNS bigint
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_assignment uuid;
            r public.run;
        BEGIN
            PERFORM runtime.require_tenant();
            SELECT assignment_id INTO v_assignment FROM public.run WHERE id = p_run;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'run % not found', p_run USING ERRCODE = 'no_data_found';
            END IF;
            PERFORM 1 FROM public.assignment WHERE id = v_assignment FOR UPDATE;
            SELECT * INTO r FROM public.run WHERE id = p_run FOR UPDATE;
            IF r.state_version <> p_expected_version THEN
                RAISE EXCEPTION 'run % is at version %, not %',
                    p_run, r.state_version, p_expected_version
                    USING ERRCODE = 'serialization_failure';
            END IF;
            RETURN runtime.move_run(r, p_to, p_continuation, p_reason, NULL);
        END
        $$
    """)

    # Admission (RUN-01, RUN-02, RUN-03). The only way a run comes to exist (RUN-09).
    op.execute(f"""
        CREATE FUNCTION runtime.admit(
            p_trigger_id uuid,
            p_run_id uuid,
            p_assignment uuid,
            p_kind text,
            p_dedup_key text,
            p_event_at timestamptz,
            p_keep interval,
            p_priority smallint
        )
        RETURNS TABLE (status text, run_id uuid)
        LANGUAGE plpgsql {DEFINER}
        AS $$
        #variable_conflict use_column
        DECLARE
            v_tenant uuid := runtime.require_tenant();
            a record;
            v_running integer;
            v_queued integer;
            v_state text;
            v_outcome text;
            old public.run;
        BEGIN
            IF p_keep IS NULL OR p_keep < interval '1 day' OR p_keep > interval '400 days' THEN
                RAISE EXCEPTION 'a dedup key is kept between 1 and 400 days'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            SELECT x.owner_id, x.current_version_id, x.concurrency_policy, x.max_running,
                   x.max_queued, x.archived_at
            INTO a FROM public.assignment x WHERE x.id = p_assignment FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'not_found'::text, NULL::uuid; RETURN;
            END IF;
            IF a.archived_at IS NOT NULL THEN
                RETURN QUERY SELECT 'archived'::text, NULL::uuid; RETURN;
            END IF;
            -- RUN-02: an event older than its dedup window can't be told from a replay.
            IF p_event_at IS NOT NULL AND p_event_at < now() - p_keep THEN
                RETURN QUERY SELECT 'too_old'::text, NULL::uuid; RETURN;
            END IF;

            INSERT INTO public.trigger_event
                (id, tenant_id, assignment_id, kind, dedup_key, event_at, keep_until, outcome)
            VALUES
                (p_trigger_id, v_tenant, p_assignment, p_kind, p_dedup_key, p_event_at,
                 now() + p_keep, 'admitted')
            ON CONFLICT (tenant_id, assignment_id, dedup_key) DO NOTHING;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'duplicate'::text, NULL::uuid; RETURN;
            END IF;

            SELECT count(*) FILTER (WHERE r.state = 'running'),
                   count(*) FILTER (WHERE r.state = 'queued')
            INTO v_running, v_queued
            FROM public.run r WHERE r.assignment_id = p_assignment;

            IF a.current_version_id IS NULL THEN
                v_state := 'paused_reacceptance';
                v_outcome := 'awaiting_acceptance';
            ELSIF a.concurrency_policy = 'skip' AND v_running + v_queued > 0 THEN
                UPDATE public.trigger_event SET outcome = 'skipped' WHERE id = p_trigger_id;
                RETURN QUERY SELECT 'skipped'::text, NULL::uuid; RETURN;
            ELSIF coalesce(p_priority, 0) = 0
                  AND a.concurrency_policy IN ('queue', 'concurrent')
                  AND v_running >= a.max_running AND v_queued >= a.max_queued THEN
                UPDATE public.trigger_event SET outcome = 'queue_full' WHERE id = p_trigger_id;
                RETURN QUERY SELECT 'queue_full'::text, NULL::uuid; RETURN;
            ELSE
                v_state := 'queued';
                v_outcome := 'admitted';
            END IF;

            IF a.concurrency_policy = 'replace' AND v_state = 'queued' THEN
                -- The older runs make way: queued ones now, the rest at their next step.
                FOR old IN
                    SELECT * FROM public.run r
                    WHERE r.assignment_id = p_assignment
                      AND r.state NOT IN ({_quoted(TERMINAL)})
                    ORDER BY r.created_at
                    FOR UPDATE
                LOOP
                    IF old.state = 'queued' THEN
                        PERFORM runtime.move_run(old, 'cancelled', NULL, 'replaced', NULL);
                    ELSE
                        UPDATE public.run SET cancel_requested_at = coalesce(cancel_requested_at, now())
                        WHERE id = old.id;
                    END IF;
                END LOOP;
            END IF;

            INSERT INTO public.run
                (id, tenant_id, assignment_id, version_id, owner_id, trigger_event_id, state,
                 priority, paused_at)
            VALUES
                (p_run_id, v_tenant, p_assignment,
                 CASE WHEN v_state = 'queued' THEN a.current_version_id END,
                 a.owner_id, p_trigger_id, v_state, coalesce(p_priority, 0),
                 CASE WHEN v_state LIKE 'paused%' THEN now() END);
            UPDATE public.trigger_event SET run_id = p_run_id, outcome = v_outcome
            WHERE id = p_trigger_id;
            PERFORM runtime.emit('run.created', p_run_id, jsonb_build_object('state', v_state));

            IF v_state = 'queued' THEN
                PERFORM runtime.promote(p_assignment);
            END IF;
            RETURN QUERY
                SELECT CASE r.state WHEN 'running' THEN 'started'
                                    WHEN 'queued' THEN 'queued'
                                    ELSE 'awaiting_acceptance' END,
                       r.id
                FROM public.run r WHERE r.id = p_run_id;
        END
        $$
    """)

    # RUN-03: once a version is accepted, runs parked for it return to the queue in trigger
    # order, and the concurrency policy applies to them as to new triggers.
    op.execute(f"""
        CREATE FUNCTION runtime.requeue_after_acceptance(p_assignment uuid) RETURNS integer
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_version uuid;
            r public.run;
            v_count integer := 0;
        BEGIN
            PERFORM runtime.require_tenant();
            SELECT current_version_id INTO v_version
            FROM public.assignment WHERE id = p_assignment FOR UPDATE;
            IF v_version IS NULL THEN
                RETURN 0;
            END IF;
            FOR r IN
                SELECT * FROM public.run x
                WHERE x.assignment_id = p_assignment AND x.state = 'paused_reacceptance'
                ORDER BY x.created_at, x.id
                FOR UPDATE
            LOOP
                UPDATE public.run SET version_id = v_version WHERE id = r.id;
                r.version_id := v_version;
                PERFORM runtime.move_run(r, 'queued', NULL, 'version_accepted', NULL);
                v_count := v_count + 1;
            END LOOP;
            PERFORM runtime.promote(p_assignment);
            RETURN v_count;
        END
        $$
    """)

    # The run-level kill switch (ASG-13): the run's owner or an admin. A pause bumps the run's
    # epoch, so its step grants stop at once; the owner can't lift an admin's pause.
    for action in ("pause", "resume", "cancel"):
        op.execute(f"""
            CREATE FUNCTION runtime.{action}_run(p_run uuid) RETURNS text
            LANGUAGE plpgsql {DEFINER}
            AS $$
            DECLARE
                v_actor uuid := public.current_principal_id();
                v_assignment uuid;
                r public.run;
                v_to text;
            BEGIN
                PERFORM runtime.require_tenant();
                IF v_actor IS NULL THEN
                    RAISE EXCEPTION 'the kill switch needs a signed-in person'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                SELECT assignment_id INTO v_assignment FROM public.run WHERE id = p_run;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'run % not found', p_run USING ERRCODE = 'no_data_found';
                END IF;
                PERFORM 1 FROM public.assignment WHERE id = v_assignment FOR UPDATE;
                SELECT * INTO r FROM public.run WHERE id = p_run FOR UPDATE;
                IF NOT (runtime.is_admin() OR r.owner_id = v_actor) THEN
                    RAISE EXCEPTION 'not allowed to {action} this run'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                {_RUN_SWITCH[action]}
                PERFORM runtime.kill_switch_audit('run', p_run, '{action}');
                RETURN v_to;
            END
            $$
        """)
        op.execute(f"GRANT EXECUTE ON FUNCTION runtime.{action}_run(uuid) TO app_user")

    # The relay: publish (assign each tenant's next sequence numbers in commit order), then
    # deliver at least once. Handlers must be idempotent.
    op.execute(f"""
        CREATE FUNCTION runtime.claim_outbox(p_limit integer)
        RETURNS TABLE (id uuid, kind text, subject_id uuid, payload jsonb, seq bigint)
        LANGUAGE plpgsql {DEFINER}
        AS $$
        #variable_conflict use_column
        DECLARE
            v_tenant uuid := runtime.require_tenant();
            v_seq bigint;
            e record;
        BEGIN
            INSERT INTO public.outbox_cursor (tenant_id, last_seq) VALUES (v_tenant, 0)
            ON CONFLICT (tenant_id) DO NOTHING;
            SELECT c.last_seq INTO v_seq FROM public.outbox_cursor c
            WHERE c.tenant_id = v_tenant FOR UPDATE;
            FOR e IN
                SELECT o.id FROM public.outbox_event o
                WHERE o.seq IS NULL
                ORDER BY o.created_at, o.id
                LIMIT 10000
            LOOP
                v_seq := v_seq + 1;
                UPDATE public.outbox_event SET seq = v_seq, published_at = now() WHERE id = e.id;
            END LOOP;
            UPDATE public.outbox_cursor SET last_seq = v_seq WHERE tenant_id = v_tenant;
            RETURN QUERY
                SELECT o.id, o.kind, o.subject_id, o.payload, o.seq
                FROM public.outbox_event o
                WHERE o.seq IS NOT NULL AND o.delivered_at IS NULL
                ORDER BY o.seq
                LIMIT least(greatest(p_limit, 0), 1000);
        END
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION runtime.mark_delivered(p_ids uuid[]) RETURNS integer
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_count integer;
        BEGIN
            PERFORM runtime.require_tenant();
            UPDATE public.outbox_event SET delivered_at = now()
            WHERE id = ANY (p_ids) AND seq IS NOT NULL AND delivered_at IS NULL;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END
        $$
    """)

    admit = "runtime.admit(uuid, uuid, uuid, text, text, timestamptz, interval, smallint)"
    op.execute(f"GRANT EXECUTE ON FUNCTION {admit} TO app_user, app_worker")
    op.execute(
        "GRANT EXECUTE ON FUNCTION runtime.transition(uuid, bigint, text, jsonb, text) "
        "TO app_worker"
    )
    op.execute("GRANT EXECUTE ON FUNCTION runtime.requeue_after_acceptance(uuid) TO app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION runtime.claim_outbox(integer) TO app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION runtime.mark_delivered(uuid[]) TO app_worker")


_RUN_SWITCH = {
    "pause": """
                IF r.state NOT IN ('queued', 'running', 'waiting_person', 'waiting_timer') THEN
                    RAISE EXCEPTION 'a % run can''t be paused', r.state
                        USING ERRCODE = 'object_not_in_prerequisite_state';
                END IF;
                UPDATE public.run SET epoch = epoch + 1 WHERE id = r.id;
                v_to := 'paused_by_user';
                PERFORM runtime.move_run(r, v_to, NULL, 'paused', v_actor);
    """,
    "resume": """
                IF r.state <> 'paused_by_user' THEN
                    RAISE EXCEPTION 'only a paused run can be resumed'
                        USING ERRCODE = 'object_not_in_prerequisite_state';
                END IF;
                IF r.paused_by <> v_actor AND NOT runtime.is_admin() THEN
                    RAISE EXCEPTION 'not allowed to resume this run'
                        USING ERRCODE = 'insufficient_privilege';
                END IF;
                v_to := CASE WHEN r.started_at IS NULL THEN 'queued' ELSE 'running' END;
                PERFORM runtime.move_run(r, v_to, NULL, 'resumed', v_actor);
                IF v_to = 'queued' THEN
                    PERFORM runtime.promote(r.assignment_id);
                END IF;
    """,
    "cancel": """
                IF r.ended_at IS NOT NULL THEN
                    RAISE EXCEPTION 'the run has already ended'
                        USING ERRCODE = 'object_not_in_prerequisite_state';
                END IF;
                UPDATE public.run SET epoch = epoch + 1,
                    cancel_requested_at = coalesce(cancel_requested_at, now())
                WHERE id = r.id;
                IF r.effects_applied = 0 THEN
                    v_to := 'cancelled';
                    PERFORM runtime.move_run(r, v_to, NULL, 'cancelled', v_actor);
                ELSIF r.state = 'running' THEN
                    v_to := 'running';  -- rolls back at its next step boundary (RUN-06)
                ELSE
                    v_to := 'running';
                    PERFORM runtime.move_run(r, v_to, NULL, 'rolling_back', v_actor);
                END IF;
    """,
}


def _replace_step_grant_functions() -> None:
    # As in 0004, plus: a grant for a run needs the run Running, records its epoch, and fails
    # once the run's epoch moves or it leaves Running.
    op.execute("""
        CREATE OR REPLACE FUNCTION runtime.issue_step_grant(
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
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_tenant uuid := runtime.require_tenant();
            t record;
            a record;
            o record;
            v record;
            v_run_state text;
            v_run_epoch bigint;
            v_run_assignment uuid;
            v_run_version uuid;
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

            IF p_run_id IS NOT NULL THEN
                SELECT x.state, x.epoch, x.assignment_id, x.version_id
                INTO v_run_state, v_run_epoch, v_run_assignment, v_run_version
                FROM public.run x WHERE x.id = p_run_id;
                IF NOT FOUND OR v_run_assignment <> p_assignment_id
                   OR v_run_version IS DISTINCT FROM p_version_id THEN
                    RETURN 'not_found';
                END IF;
                IF v_run_state <> 'running' THEN RETURN 'run_not_running'; END IF;
            END IF;

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
                grants, dry_run, tenant_epoch, owner_epoch, assignment_epoch, run_epoch,
                expires_at
            ) VALUES (
                p_id, v_tenant, p_assignment_id, p_version_id, a.owner_id, p_run_id, p_step_key,
                p_attempt, v_grants, coalesce(p_dry_run, false), t.epoch, o.epoch, a.epoch,
                v_run_epoch, now() + p_ttl
            );
            RETURN 'issued';
        END
        $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION runtime.check_step_grant(p_id uuid)
        RETURNS TABLE (
            status text, owner_id uuid, assignment_id uuid, version_id uuid, run_id uuid,
            grants jsonb, dry_run boolean
        )
        LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
        AS $$
        #variable_conflict use_column
        DECLARE
            g public.step_grant;
            t record;
            o record;
            a record;
            v_run_state text;
            v_run_epoch bigint;
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
            IF g.run_id IS NOT NULL THEN
                SELECT x.state, x.epoch INTO v_run_state, v_run_epoch
                FROM public.run x WHERE x.id = g.run_id;
            END IF;

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
                WHEN g.run_id IS NOT NULL AND v_run_epoch <> g.run_epoch THEN 'run_paused'
                WHEN g.run_id IS NOT NULL AND v_run_state <> 'running' THEN 'run_not_running'
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


def _announce_acceptances() -> None:
    # standing.accept_version, as in 0004, now also emits version.accepted for the relay.
    op.execute("""
        CREATE OR REPLACE FUNCTION standing.accept_version(p_version_id uuid, p_widening jsonb)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
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
            INSERT INTO public.outbox_event (tenant_id, kind, subject_id, payload)
            VALUES (v_tenant, 'version.accepted', v.assignment_id,
                    jsonb_build_object('version_id', v.id));
        END
        $$
    """)


def downgrade() -> None:
    raise NotImplementedError("Collicity migrations are forward-only")
