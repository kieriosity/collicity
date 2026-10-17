"""Price catalog and budget ledger (BUD-01, 03, 06, 08, 10, 11; technical design §6).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15

Balances change only through the functions in schema ``ledger``. They are SECURITY DEFINER,
owned by ``ledger_definer``, which tenant RLS still binds, so a definer function can't reach
another tenant's rows either. The worker and app may reserve; only the gateway may mark a
reservation started or settle it. ``sweep.due_for_expiry`` (owned by ``sweeper``) is the one
cross-tenant read, and returns identifiers only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICE_ROLES = "app_user, app_worker, gw"
LEDGER_TABLE_ROLES = "app_user, app_worker, gw, ledger_definer"
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"


def _force_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL TO {LEDGER_TABLE_ROLES} "
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
    # Functions are private unless a migration grants them.
    for owner in ("migrator", "ledger_definer", "sweeper"):
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        )

    op.execute("""
        CREATE FUNCTION reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
    """)

    # ---- Price catalog (BUD-08). Global, versioned, immutable once published. ----
    op.execute("""
        CREATE TABLE price_version (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            label text NOT NULL,
            effective_at timestamptz NOT NULL,
            source text NOT NULL,
            published_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT price_version_label_key UNIQUE (label)
        )
    """)
    # micro_usd is the price of unit_size units: 2000000 per 1000000 input tokens is $2/MTok.
    # A tier applies when the request's prompt has at least tier_floor tokens; 0 is the base.
    op.execute("""
        CREATE TABLE price_item (
            price_version_id uuid NOT NULL REFERENCES price_version (id),
            sku text NOT NULL,
            meter text NOT NULL,
            tier_floor bigint NOT NULL DEFAULT 0,
            unit_size bigint NOT NULL,
            micro_usd bigint NOT NULL,
            CONSTRAINT price_item_pkey PRIMARY KEY (price_version_id, sku, meter, tier_floor),
            CONSTRAINT price_item_meter_check CHECK (meter IN (
                'input', 'output', 'cache_write_5m', 'cache_write_1h', 'cache_read',
                'call', 'second', 'use'
            )),
            CONSTRAINT price_item_tier_floor_check CHECK (tier_floor >= 0),
            CONSTRAINT price_item_unit_size_check CHECK (unit_size > 0),
            CONSTRAINT price_item_micro_usd_check CHECK (micro_usd >= 0)
        )
    """)
    _immutable("price_version")
    _immutable("price_item")
    # Items join a version only in the transaction that publishes it (now() is the
    # transaction's start time), so a published version can't gain prices later.
    op.execute("""
        CREATE FUNCTION price_item_same_transaction() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF (SELECT v.published_at FROM price_version v WHERE v.id = NEW.price_version_id)
                    <> now() THEN
                RAISE EXCEPTION 'price version % is already published', NEW.price_version_id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute(
        "CREATE TRIGGER price_item_same_transaction BEFORE INSERT ON price_item "
        "FOR EACH ROW EXECUTE FUNCTION price_item_same_transaction()"
    )
    op.execute(f"GRANT SELECT ON price_version, price_item TO {SERVICE_ROLES}")

    # ---- Budgets (BUD-01, BUD-06 without groups, BUD-11). ----
    # The hierarchy follows from the scope keys: a reservation always holds against every
    # organization budget, the person's user budgets, and its assignment or workspace budgets.
    op.execute("""
        CREATE TABLE budget (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenant (id),
            scope text NOT NULL,
            principal_id uuid,
            assignment_id uuid,
            period text NOT NULL,
            limit_micro_usd bigint NOT NULL,
            lock_rank smallint GENERATED ALWAYS AS (
                CASE scope WHEN 'organization' THEN 10 WHEN 'user' THEN 30 ELSE 40 END
            ) STORED,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT budget_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT budget_tenant_id_scope_principal_id_assignment_id_period_key
                UNIQUE NULLS NOT DISTINCT (tenant_id, scope, principal_id, assignment_id, period),
            CONSTRAINT budget_tenant_id_principal_id_fkey
                FOREIGN KEY (tenant_id, principal_id) REFERENCES principal (tenant_id, id),
            CONSTRAINT budget_scope_check CHECK (
                (scope = 'organization' AND principal_id IS NULL AND assignment_id IS NULL)
                OR (scope IN ('user', 'workspace')
                    AND principal_id IS NOT NULL AND assignment_id IS NULL)
                OR (scope = 'assignment' AND assignment_id IS NOT NULL AND principal_id IS NULL)
            ),
            CONSTRAINT budget_period_check CHECK (period IN ('day', 'week', 'month')),
            CONSTRAINT budget_limit_micro_usd_check CHECK (limit_micro_usd >= 0)
        )
    """)

    # One row per budget per period, created on first use. limit_micro_usd is this period's
    # limit: copied from the budget, and raised here by a one-time increase (BUD-04).
    op.execute("""
        CREATE TABLE ledger_account (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL,
            budget_id uuid NOT NULL,
            period_start timestamptz NOT NULL,
            limit_micro_usd bigint NOT NULL,
            spent_micro_usd bigint NOT NULL DEFAULT 0,
            reserved_micro_usd bigint NOT NULL DEFAULT 0,
            lock_rank smallint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ledger_account_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT ledger_account_budget_id_period_start_key UNIQUE (budget_id, period_start),
            CONSTRAINT ledger_account_tenant_id_budget_id_fkey
                FOREIGN KEY (tenant_id, budget_id) REFERENCES budget (tenant_id, id),
            CONSTRAINT ledger_account_limit_micro_usd_check CHECK (limit_micro_usd >= 0),
            CONSTRAINT ledger_account_spent_micro_usd_check CHECK (spent_micro_usd >= 0),
            CONSTRAINT ledger_account_reserved_micro_usd_check CHECK (reserved_micro_usd >= 0)
        )
    """)

    # active -> started -> settled, or active -> released, or (at expiry) started -> charged
    # in full -> settled when the actual cost arrives (BUD-03).
    op.execute("""
        CREATE TABLE reservation (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenant (id),
            principal_id uuid NOT NULL,
            assignment_id uuid,
            price_version_id uuid NOT NULL REFERENCES price_version (id),
            purpose text NOT NULL,
            amount_micro_usd bigint NOT NULL,
            actual_micro_usd bigint,
            state text NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            started_at timestamptz,
            finished_at timestamptz,
            CONSTRAINT reservation_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT reservation_tenant_id_principal_id_fkey
                FOREIGN KEY (tenant_id, principal_id) REFERENCES principal (tenant_id, id),
            CONSTRAINT reservation_state_check
                CHECK (state IN ('active', 'started', 'released', 'charged', 'settled')),
            CONSTRAINT reservation_amount_micro_usd_check CHECK (amount_micro_usd > 0),
            CONSTRAINT reservation_actual_micro_usd_check CHECK (actual_micro_usd >= 0),
            CONSTRAINT reservation_started_check
                CHECK ((started_at IS NULL) = (state IN ('active', 'released'))),
            CONSTRAINT reservation_finished_check
                CHECK ((finished_at IS NULL) = (state IN ('active', 'started'))),
            CONSTRAINT reservation_actual_check
                CHECK ((actual_micro_usd IS NULL) = (state <> 'settled'))
        )
    """)
    op.execute(
        "CREATE INDEX reservation_expires_at_idx ON reservation (expires_at) "
        "WHERE state IN ('active', 'started')"
    )

    op.execute("""
        CREATE TABLE reservation_account (
            tenant_id uuid NOT NULL,
            reservation_id uuid NOT NULL,
            ledger_account_id uuid NOT NULL,
            CONSTRAINT reservation_account_pkey PRIMARY KEY (reservation_id, ledger_account_id),
            CONSTRAINT reservation_account_tenant_id_reservation_id_fkey
                FOREIGN KEY (tenant_id, reservation_id) REFERENCES reservation (tenant_id, id),
            CONSTRAINT reservation_account_tenant_id_ledger_account_id_fkey
                FOREIGN KEY (tenant_id, ledger_account_id)
                REFERENCES ledger_account (tenant_id, id)
        )
    """)

    # Append-only journal: every balance change, so balances can be re-derived and audited.
    op.execute("""
        CREATE TABLE ledger_entry (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL,
            ledger_account_id uuid NOT NULL,
            reservation_id uuid NOT NULL,
            kind text NOT NULL,
            reserved_delta bigint NOT NULL,
            spent_delta bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ledger_entry_tenant_id_ledger_account_id_fkey
                FOREIGN KEY (tenant_id, ledger_account_id)
                REFERENCES ledger_account (tenant_id, id),
            CONSTRAINT ledger_entry_tenant_id_reservation_id_fkey
                FOREIGN KEY (tenant_id, reservation_id) REFERENCES reservation (tenant_id, id),
            CONSTRAINT ledger_entry_kind_check
                CHECK (kind IN ('reserve', 'release', 'settle', 'charge', 'reconcile'))
        )
    """)
    op.execute(
        "CREATE INDEX ledger_entry_tenant_id_ledger_account_id_idx "
        "ON ledger_entry (tenant_id, ledger_account_id)"
    )
    _immutable("ledger_entry")

    for table in ("budget", "ledger_account", "reservation", "reservation_account", "ledger_entry"):
        _force_rls(table)

    # Service roles read balances; nobody but ledger_definer writes them.
    op.execute(
        "GRANT SELECT ON budget, ledger_account, reservation, reservation_account, ledger_entry "
        "TO app_user, app_worker"
    )
    # The gateway reads a reservation's recorded price version to recompute its worst case.
    op.execute("GRANT SELECT ON reservation TO gw")
    op.execute("GRANT USAGE ON SCHEMA public TO ledger_definer, sweeper")
    op.execute("GRANT EXECUTE ON FUNCTION current_tenant_id() TO ledger_definer")
    op.execute("GRANT SELECT ON budget TO ledger_definer")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ledger_account, reservation TO ledger_definer")
    op.execute("GRANT SELECT, INSERT ON reservation_account TO ledger_definer")
    op.execute("GRANT INSERT ON ledger_entry TO ledger_definer")

    # The sweeper sees which reservations are due across tenants, and nothing else about them.
    op.execute("CREATE POLICY sweeper_scan ON reservation FOR SELECT TO sweeper USING (true)")
    op.execute("GRANT SELECT (id, tenant_id, state, expires_at) ON reservation TO sweeper")

    op.execute("CREATE SCHEMA ledger AUTHORIZATION ledger_definer")
    op.execute("CREATE SCHEMA sweep AUTHORIZATION sweeper")

    op.execute("SET ROLE ledger_definer")
    _create_ledger_functions()
    op.execute("RESET ROLE")

    op.execute("SET ROLE sweeper")
    op.execute(f"""
        CREATE FUNCTION sweep.due_for_expiry(p_limit integer)
        RETURNS TABLE (tenant_id uuid, reservation_id uuid)
        LANGUAGE sql STABLE {DEFINER}
        AS $$
            SELECT r.tenant_id, r.id
            FROM public.reservation r
            WHERE r.state IN ('active', 'started') AND r.expires_at <= now()
            ORDER BY r.expires_at
            LIMIT least(greatest(p_limit, 0), 1000)
        $$
    """)
    op.execute("GRANT USAGE ON SCHEMA sweep TO app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION sweep.due_for_expiry(integer) TO app_worker")
    op.execute("RESET ROLE")


def _create_ledger_functions() -> None:
    # Periods are calendar periods in UTC; weeks start on Monday.
    op.execute("""
        CREATE FUNCTION ledger.period_start(p_period text, p_at timestamptz)
        RETURNS timestamptz
        LANGUAGE sql IMMUTABLE STRICT
        SET search_path = pg_catalog, pg_temp
        AS $$ SELECT date_trunc(p_period, p_at, 'UTC') $$
    """)

    op.execute("""
        CREATE FUNCTION ledger.require_tenant() RETURNS uuid
        LANGUAGE plpgsql STABLE
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_tenant uuid := public.current_tenant_id();
        BEGIN
            IF v_tenant IS NULL THEN
                RAISE EXCEPTION 'ledger functions need a tenant context'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN v_tenant;
        END
        $$
    """)

    # Locks a reservation's accounts in the global order (§6.1) and returns their ids.
    op.execute("""
        CREATE FUNCTION ledger.lock_accounts(p_reservation_id uuid) RETURNS uuid[]
        LANGUAGE plpgsql
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_accounts uuid[];
        BEGIN
            SELECT array_agg(a.id ORDER BY a.lock_rank, a.id) INTO v_accounts
            FROM public.reservation_account ra
            JOIN public.ledger_account a ON a.id = ra.ledger_account_id
            WHERE ra.reservation_id = p_reservation_id;
            PERFORM 1 FROM public.ledger_account a
            WHERE a.id = ANY (v_accounts)
            ORDER BY a.lock_rank, a.id
            FOR UPDATE;
            RETURN v_accounts;
        END
        $$
    """)

    # BUD-03, BUD-10: hold the worst case against every applicable account in one transaction,
    # or hold nothing. A retry with the same id returns the first outcome.
    op.execute(f"""
        CREATE FUNCTION ledger.reserve(
            p_reservation_id uuid,
            p_principal_id uuid,
            p_assignment_id uuid,
            p_amount_micro_usd bigint,
            p_price_version_id uuid,
            p_purpose text,
            p_ttl interval
        )
        RETURNS TABLE (status text, blocking_budget_ids uuid[])
        LANGUAGE plpgsql {DEFINER}
        AS $$
        #variable_conflict use_column
        DECLARE
            v_tenant uuid := ledger.require_tenant();
            v_now timestamptz := now();
            v_leaf text := CASE WHEN p_assignment_id IS NULL THEN 'workspace' ELSE 'assignment' END;
            v_existing public.reservation;
            v_budgets uuid[];
            v_accounts uuid[];
            v_blocking uuid[];
        BEGIN
            IF p_amount_micro_usd IS NULL OR p_amount_micro_usd <= 0 THEN
                RAISE EXCEPTION 'a reservation must be for a positive amount'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF p_ttl IS NULL OR p_ttl < interval '1 second' OR p_ttl > interval '48 hours' THEN
                RAISE EXCEPTION 'reservation lifetime must be between 1 second and 48 hours'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            SELECT * INTO v_existing FROM public.reservation r WHERE r.id = p_reservation_id;
            IF FOUND THEN
                IF v_existing.principal_id = p_principal_id
                        AND v_existing.assignment_id IS NOT DISTINCT FROM p_assignment_id
                        AND v_existing.amount_micro_usd = p_amount_micro_usd
                        AND v_existing.price_version_id = p_price_version_id THEN
                    RETURN QUERY SELECT 'reserved'::text, NULL::uuid[];
                    RETURN;
                END IF;
                RAISE EXCEPTION 'reservation % was already made with other terms', p_reservation_id
                    USING ERRCODE = 'unique_violation';
            END IF;

            SELECT array_agg(b.id) INTO v_budgets
            FROM public.budget b
            WHERE b.scope = 'organization'
               OR (b.scope = 'user' AND b.principal_id = p_principal_id)
               OR (b.scope = 'workspace' AND v_leaf = 'workspace'
                   AND b.principal_id = p_principal_id)
               OR (b.scope = 'assignment' AND b.assignment_id = p_assignment_id);

            -- Every reservation needs an organization budget and one for what it's charged to.
            IF NOT EXISTS (SELECT 1 FROM public.budget b
                           WHERE b.id = ANY (v_budgets) AND b.scope = 'organization')
               OR NOT EXISTS (SELECT 1 FROM public.budget b
                              WHERE b.id = ANY (v_budgets) AND b.scope = v_leaf) THEN
                RETURN QUERY SELECT 'no_budget'::text, NULL::uuid[];
                RETURN;
            END IF;

            INSERT INTO public.ledger_account
                (tenant_id, budget_id, period_start, limit_micro_usd, lock_rank)
            SELECT b.tenant_id, b.id, ledger.period_start(b.period, v_now),
                   b.limit_micro_usd, b.lock_rank
            FROM public.budget b
            WHERE b.id = ANY (v_budgets)
            ORDER BY b.lock_rank, b.id
            ON CONFLICT (budget_id, period_start) DO NOTHING;

            SELECT array_agg(a.id ORDER BY a.lock_rank, a.id) INTO v_accounts
            FROM public.ledger_account a
            JOIN public.budget b ON b.id = a.budget_id
            WHERE a.budget_id = ANY (v_budgets)
              AND a.period_start = ledger.period_start(b.period, v_now);

            PERFORM 1 FROM public.ledger_account a
            WHERE a.id = ANY (v_accounts)
            ORDER BY a.lock_rank, a.id
            FOR UPDATE;

            SELECT array_agg(a.budget_id ORDER BY a.lock_rank, a.id) INTO v_blocking
            FROM public.ledger_account a
            WHERE a.id = ANY (v_accounts)
              AND a.spent_micro_usd + a.reserved_micro_usd + p_amount_micro_usd
                  > a.limit_micro_usd;
            IF v_blocking IS NOT NULL THEN
                RETURN QUERY SELECT 'insufficient'::text, v_blocking;
                RETURN;
            END IF;

            UPDATE public.ledger_account a
            SET reserved_micro_usd = a.reserved_micro_usd + p_amount_micro_usd
            WHERE a.id = ANY (v_accounts);

            INSERT INTO public.reservation
                (id, tenant_id, principal_id, assignment_id, price_version_id, purpose,
                 amount_micro_usd, expires_at)
            VALUES
                (p_reservation_id, v_tenant, p_principal_id, p_assignment_id,
                 p_price_version_id, p_purpose, p_amount_micro_usd, v_now + p_ttl);

            INSERT INTO public.reservation_account (tenant_id, reservation_id, ledger_account_id)
            SELECT v_tenant, p_reservation_id, account_id FROM unnest(v_accounts) AS account_id;

            INSERT INTO public.ledger_entry
                (tenant_id, ledger_account_id, reservation_id, kind, reserved_delta, spent_delta)
            SELECT v_tenant, account_id, p_reservation_id, 'reserve', p_amount_micro_usd, 0
            FROM unnest(v_accounts) AS account_id;

            RETURN QUERY SELECT 'reserved'::text, NULL::uuid[];
        END
        $$
    """)

    # The gateway marks a reservation started just before dispatch, after checking it covers
    # the gateway's own worst-case figure (§3.2). Nothing billable starts any other way.
    op.execute(f"""
        CREATE FUNCTION ledger.start(
            p_reservation_id uuid,
            p_principal_id uuid,
            p_assignment_id uuid,
            p_required_micro_usd bigint,
            p_settle_within interval
        )
        RETURNS text
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := ledger.require_tenant();
            r public.reservation;
        BEGIN
            IF p_required_micro_usd IS NULL OR p_required_micro_usd <= 0 THEN
                RAISE EXCEPTION 'a billable operation must have a positive worst case'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF p_settle_within IS NULL OR p_settle_within < interval '1 minute'
                    OR p_settle_within > interval '48 hours' THEN
                RAISE EXCEPTION 'settlement window must be between 1 minute and 48 hours'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            SELECT * INTO r FROM public.reservation WHERE id = p_reservation_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            IF r.principal_id <> p_principal_id
                    OR r.assignment_id IS DISTINCT FROM p_assignment_id THEN
                RETURN 'mismatch';
            END IF;
            IF r.state <> 'active' THEN
                RETURN 'not_active';
            END IF;
            IF r.expires_at <= now() THEN
                RETURN 'expired';
            END IF;
            IF r.amount_micro_usd < p_required_micro_usd THEN
                RETURN 'insufficient';
            END IF;

            UPDATE public.reservation
            SET state = 'started', started_at = now(), expires_at = now() + p_settle_within
            WHERE id = p_reservation_id;
            RETURN 'started';
        END
        $$
    """)

    # Settle a started reservation to its actual cost, or correct one charged in full at expiry.
    op.execute(f"""
        CREATE FUNCTION ledger.settle(p_reservation_id uuid, p_actual_micro_usd bigint)
        RETURNS text
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := ledger.require_tenant();
            r public.reservation;
            v_accounts uuid[];
        BEGIN
            IF p_actual_micro_usd IS NULL OR p_actual_micro_usd < 0 THEN
                RAISE EXCEPTION 'actual cost can''t be negative'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            SELECT * INTO r FROM public.reservation WHERE id = p_reservation_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            IF r.state NOT IN ('started', 'charged') THEN
                RETURN 'not_started';
            END IF;

            v_accounts := ledger.lock_accounts(p_reservation_id);
            IF r.state = 'started' THEN
                UPDATE public.ledger_account a
                SET reserved_micro_usd = a.reserved_micro_usd - r.amount_micro_usd,
                    spent_micro_usd = a.spent_micro_usd + p_actual_micro_usd
                WHERE a.id = ANY (v_accounts);
                INSERT INTO public.ledger_entry
                    (tenant_id, ledger_account_id, reservation_id, kind,
                     reserved_delta, spent_delta)
                SELECT v_tenant, account_id, p_reservation_id, 'settle',
                       -r.amount_micro_usd, p_actual_micro_usd
                FROM unnest(v_accounts) AS account_id;
            ELSE
                UPDATE public.ledger_account a
                SET spent_micro_usd = a.spent_micro_usd + p_actual_micro_usd - r.amount_micro_usd
                WHERE a.id = ANY (v_accounts);
                INSERT INTO public.ledger_entry
                    (tenant_id, ledger_account_id, reservation_id, kind,
                     reserved_delta, spent_delta)
                SELECT v_tenant, account_id, p_reservation_id, 'reconcile',
                       0, p_actual_micro_usd - r.amount_micro_usd
                FROM unnest(v_accounts) AS account_id;
            END IF;

            UPDATE public.reservation
            SET state = 'settled', actual_micro_usd = p_actual_micro_usd, finished_at = now()
            WHERE id = p_reservation_id;

            -- An actual cost above the reservation means the cost model is wrong (§6.2).
            RETURN CASE WHEN p_actual_micro_usd > r.amount_micro_usd
                        THEN 'settled_overrun' ELSE 'settled' END;
        END
        $$
    """)

    # Give back an active reservation whose operation will not run.
    op.execute(f"""
        CREATE FUNCTION ledger.release(p_reservation_id uuid)
        RETURNS text
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := ledger.require_tenant();
            r public.reservation;
            v_accounts uuid[];
        BEGIN
            SELECT * INTO r FROM public.reservation WHERE id = p_reservation_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            IF r.state <> 'active' THEN
                RETURN 'not_active';
            END IF;

            v_accounts := ledger.lock_accounts(p_reservation_id);
            UPDATE public.ledger_account a
            SET reserved_micro_usd = a.reserved_micro_usd - r.amount_micro_usd
            WHERE a.id = ANY (v_accounts);
            INSERT INTO public.ledger_entry
                (tenant_id, ledger_account_id, reservation_id, kind, reserved_delta, spent_delta)
            SELECT v_tenant, account_id, p_reservation_id, 'release', -r.amount_micro_usd, 0
            FROM unnest(v_accounts) AS account_id;
            UPDATE public.reservation SET state = 'released', finished_at = now()
            WHERE id = p_reservation_id;
            RETURN 'released';
        END
        $$
    """)

    # At expiry (BUD-03): release what never started; charge in full what may have.
    op.execute(f"""
        CREATE FUNCTION ledger.expire(p_reservation_id uuid)
        RETURNS text
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := ledger.require_tenant();
            r public.reservation;
            v_accounts uuid[];
        BEGIN
            SELECT * INTO r FROM public.reservation WHERE id = p_reservation_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            IF r.state NOT IN ('active', 'started') OR r.expires_at > now() THEN
                RETURN 'not_due';
            END IF;

            v_accounts := ledger.lock_accounts(p_reservation_id);
            IF r.state = 'active' THEN
                UPDATE public.ledger_account a
                SET reserved_micro_usd = a.reserved_micro_usd - r.amount_micro_usd
                WHERE a.id = ANY (v_accounts);
                INSERT INTO public.ledger_entry
                    (tenant_id, ledger_account_id, reservation_id, kind,
                     reserved_delta, spent_delta)
                SELECT v_tenant, account_id, p_reservation_id, 'release', -r.amount_micro_usd, 0
                FROM unnest(v_accounts) AS account_id;
                UPDATE public.reservation SET state = 'released', finished_at = now()
                WHERE id = p_reservation_id;
                RETURN 'released';
            END IF;

            UPDATE public.ledger_account a
            SET reserved_micro_usd = a.reserved_micro_usd - r.amount_micro_usd,
                spent_micro_usd = a.spent_micro_usd + r.amount_micro_usd
            WHERE a.id = ANY (v_accounts);
            INSERT INTO public.ledger_entry
                (tenant_id, ledger_account_id, reservation_id, kind, reserved_delta, spent_delta)
            SELECT v_tenant, account_id, p_reservation_id, 'charge',
                   -r.amount_micro_usd, r.amount_micro_usd
            FROM unnest(v_accounts) AS account_id;
            UPDATE public.reservation SET state = 'charged', finished_at = now()
            WHERE id = p_reservation_id;
            RETURN 'charged';
        END
        $$
    """)

    op.execute("GRANT USAGE ON SCHEMA ledger TO app_user, app_worker, gw")
    reserve = "ledger.reserve(uuid, uuid, uuid, bigint, uuid, text, interval)"
    start = "ledger.start(uuid, uuid, uuid, bigint, interval)"
    op.execute(f"GRANT EXECUTE ON FUNCTION {reserve} TO app_user, app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION ledger.release(uuid) TO app_user, app_worker, gw")
    op.execute(f"GRANT EXECUTE ON FUNCTION {start} TO gw")
    op.execute("GRANT EXECUTE ON FUNCTION ledger.settle(uuid, bigint) TO gw")
    op.execute("GRANT EXECUTE ON FUNCTION ledger.expire(uuid) TO app_worker")


def downgrade() -> None:
    raise NotImplementedError("Collicity migrations are forward-only")
