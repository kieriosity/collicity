"""Write-ahead audit log: append-only records and a per-tenant hash chain (ADM-07, SEC-07, MR-16).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17

Technical design §10. Records hold identifiers and hashes, never payloads or prompt text.
Personal fields arrive already encrypted under the subject's own data key; the database never
sees them in clear. Each record's digest covers the ciphertext, so destroying a person's key
(crypto-shredding) leaves the chain verifiable.

Services write only through ``audit.append``, which computes the digest; nobody can update or
delete a record or a seal, migrator included. ``audit.seal`` links each tenant's records into a
hash chain, one sealer per tenant at a time; ``audit.verify`` recomputes every digest and link.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICE_ROLES = "app_user, app_worker, gw"
TENANT_ROLES = "app_user, app_worker, gw, audit_definer"
DEFINER = "SECURITY DEFINER SET search_path = pg_catalog, pg_temp"
GENESIS = r"'\x" + "00" * 32 + "'::bytea"

KINDS = (
    "connector_call_intent",
    "connector_call_result",
    "connector_call_denied",
    "model_call",
    "data_access",
    "approval_decided",
    "approval_consumed",
    "budget_authorization",
    "admin_change",
    "version_accepted",
    "revocation",
    "kill_switch",
    "snapshot_event",
    "share_event",
    "erasure",
)


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
    name = table.rsplit(".", maxsplit=1)[-1]
    op.execute(
        f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION public.reject_change()"
    )
    op.execute(
        f"CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE ON {table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_change()"
    )


def upgrade() -> None:
    op.execute("CREATE SCHEMA audit")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE audit_definer REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )

    kinds = ", ".join(f"'{kind}'" for kind in KINDS)
    op.execute(f"""
        CREATE TABLE audit.record (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES public.tenant (id),
            occurred_at timestamptz NOT NULL,
            kind text NOT NULL,
            outcome text,
            actor_id uuid,          -- who acted: a person, or NULL for the system
            user_id uuid,           -- whose access was used (CON-07); a delegate acts for an owner
            agent_id uuid,          -- the assignment or instance (MR-04)
            run_id uuid,
            correlation_id uuid,    -- links an intent to its result, an approval to its use
            connector text,
            action text,
            resource_ids text[] NOT NULL DEFAULT '{{}}',
            detail jsonb NOT NULL DEFAULT '{{}}',   -- identifiers and hashes only
            subject_id uuid,        -- whose personal fields the ciphertext holds
            key_version integer,
            ciphertext bytea,       -- AES-256-GCM: 12-byte nonce, then ciphertext and tag
            digest bytea NOT NULL,  -- sha256 of audit.canonical(record)
            CONSTRAINT record_tenant_id_id_key UNIQUE (tenant_id, id),
            CONSTRAINT record_kind_check CHECK (kind IN ({kinds})),
            CONSTRAINT record_personal_check CHECK (
                (subject_id IS NULL) = (ciphertext IS NULL)
                AND (subject_id IS NULL) = (key_version IS NULL)
            ),
            CONSTRAINT record_detail_check CHECK (jsonb_typeof(detail) = 'object')
        )
    """)
    op.execute(
        "CREATE INDEX record_tenant_id_occurred_at_idx ON audit.record (tenant_id, occurred_at)"
    )
    op.execute("CREATE INDEX record_tenant_id_run_id_idx ON audit.record (tenant_id, run_id)")

    # Records committed but not yet linked into the chain: the sealer's work queue.
    op.execute("""
        CREATE TABLE audit.pending (
            tenant_id uuid NOT NULL,
            record_id uuid PRIMARY KEY,
            CONSTRAINT pending_tenant_id_record_id_fkey
                FOREIGN KEY (tenant_id, record_id) REFERENCES audit.record (tenant_id, id)
        )
    """)
    op.execute("CREATE INDEX pending_tenant_id_idx ON audit.pending (tenant_id)")

    # hash = sha256(prev_hash || chain_seq as int8 || record id || record digest)
    op.execute("""
        CREATE TABLE audit.seal (
            tenant_id uuid NOT NULL,
            chain_seq bigint NOT NULL,
            record_id uuid NOT NULL,
            prev_hash bytea NOT NULL,
            hash bytea NOT NULL,
            sealed_at timestamptz NOT NULL,
            CONSTRAINT seal_pkey PRIMARY KEY (tenant_id, chain_seq),
            CONSTRAINT seal_record_id_key UNIQUE (record_id),
            CONSTRAINT seal_tenant_id_record_id_fkey
                FOREIGN KEY (tenant_id, record_id) REFERENCES audit.record (tenant_id, id),
            CONSTRAINT seal_chain_seq_check CHECK (chain_seq > 0)
        )
    """)

    # The head of each tenant's chain. Its row lock serializes sealers.
    op.execute("""
        CREATE TABLE audit.chain (
            tenant_id uuid PRIMARY KEY REFERENCES public.tenant (id),
            last_seq bigint NOT NULL,
            last_hash bytea NOT NULL
        )
    """)

    # Per-person data keys, wrapped by the tenant's audit key. Deleting a person's rows is the
    # crypto-shred.
    op.execute("""
        CREATE TABLE audit.person_key (
            tenant_id uuid NOT NULL,
            principal_id uuid NOT NULL,
            version integer NOT NULL,
            wrapped_key bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT person_key_pkey PRIMARY KEY (tenant_id, principal_id, version),
            CONSTRAINT person_key_tenant_id_principal_id_fkey
                FOREIGN KEY (tenant_id, principal_id) REFERENCES public.principal (tenant_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE audit.erased (
            tenant_id uuid NOT NULL,
            principal_id uuid NOT NULL,
            erased_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT erased_pkey PRIMARY KEY (tenant_id, principal_id)
        )
    """)
    # The tenant's key for blind indexes, wrapped by its audit key.
    op.execute("""
        CREATE TABLE audit.tenant_key (
            tenant_id uuid NOT NULL REFERENCES public.tenant (id),
            purpose text NOT NULL,
            wrapped_key bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT tenant_key_pkey PRIMARY KEY (tenant_id, purpose),
            CONSTRAINT tenant_key_purpose_check CHECK (purpose IN ('blind_index'))
        )
    """)
    # Keyed hashes of searchable personal fields, outside the chain so shredding can delete them.
    op.execute("""
        CREATE TABLE audit.blind_index (
            tenant_id uuid NOT NULL,
            principal_id uuid NOT NULL,
            record_id uuid NOT NULL,
            field text NOT NULL,
            token bytea NOT NULL,
            CONSTRAINT blind_index_pkey PRIMARY KEY (record_id, field),
            CONSTRAINT blind_index_tenant_id_record_id_fkey
                FOREIGN KEY (tenant_id, record_id) REFERENCES audit.record (tenant_id, id)
        )
    """)
    op.execute(
        "CREATE INDEX blind_index_tenant_id_field_token_idx "
        "ON audit.blind_index (tenant_id, field, token)"
    )
    op.execute(
        "CREATE INDEX blind_index_tenant_id_principal_id_idx "
        "ON audit.blind_index (tenant_id, principal_id)"
    )

    _immutable("audit.record")
    _immutable("audit.seal")
    for table in (
        "audit.record",
        "audit.pending",
        "audit.seal",
        "audit.chain",
        "audit.person_key",
        "audit.erased",
        "audit.tenant_key",
        "audit.blind_index",
    ):
        _force_rls(table)

    op.execute("GRANT USAGE ON SCHEMA audit TO app_user, app_worker, gw, audit_definer, sweeper")
    op.execute("GRANT CREATE ON SCHEMA audit TO audit_definer")
    op.execute("GRANT USAGE ON SCHEMA public TO audit_definer")
    op.execute("GRANT EXECUTE ON FUNCTION public.current_tenant_id() TO audit_definer")
    op.execute(
        "GRANT SELECT, INSERT ON audit.record, audit.seal, audit.tenant_key TO audit_definer"
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON audit.pending TO audit_definer")
    op.execute("GRANT SELECT, INSERT, UPDATE ON audit.chain TO audit_definer")
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON audit.person_key, audit.blind_index TO audit_definer"
    )
    op.execute("GRANT SELECT, INSERT ON audit.erased TO audit_definer")
    # The admin console reads records (ADM-06, ADM-07); personal fields stay encrypted.
    op.execute("GRANT SELECT ON audit.record, audit.seal TO app_user")

    # The sweeper finds tenants with records waiting to be sealed, and nothing else.
    op.execute("CREATE POLICY sweeper_scan ON audit.pending FOR SELECT TO sweeper USING (true)")
    op.execute("GRANT SELECT (tenant_id) ON audit.pending TO sweeper")

    op.execute("SET ROLE audit_definer")
    _create_audit_functions()
    op.execute("RESET ROLE")

    op.execute("SET ROLE sweeper")
    op.execute(f"""
        CREATE FUNCTION sweep.tenants_with_unsealed_audit(p_limit integer)
        RETURNS SETOF uuid
        LANGUAGE sql STABLE {DEFINER}
        AS $$
            SELECT DISTINCT p.tenant_id FROM audit.pending p
            LIMIT least(greatest(p_limit, 0), 1000)
        $$
    """)
    op.execute("GRANT EXECUTE ON FUNCTION sweep.tenants_with_unsealed_audit(integer) TO app_worker")
    op.execute("RESET ROLE")


def _create_audit_functions() -> None:
    op.execute("""
        CREATE FUNCTION audit.require_tenant() RETURNS uuid
        LANGUAGE plpgsql STABLE
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            v_tenant uuid := public.current_tenant_id();
        BEGIN
            IF v_tenant IS NULL THEN
                RAISE EXCEPTION 'audit functions need a tenant context'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN v_tenant;
        END
        $$
    """)

    # The bytes a record's digest covers: every column but the digest, in a fixed order, with
    # the timestamp as UTC microseconds so the session time zone can't change it.
    op.execute("""
        CREATE FUNCTION audit.canonical(r audit.record) RETURNS bytea
        LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT convert_to(jsonb_build_array(
                r.id, r.tenant_id, (extract(epoch FROM r.occurred_at) * 1000000)::bigint,
                r.kind, r.outcome, r.actor_id, r.user_id, r.agent_id, r.run_id,
                r.correlation_id, r.connector, r.action, to_jsonb(r.resource_ids), r.detail,
                r.subject_id, r.key_version, encode(r.ciphertext, 'hex')
            )::text, 'UTF8')
        $$
    """)

    op.execute("""
        CREATE FUNCTION audit.link(p_prev bytea, p_seq bigint, p_record uuid, p_digest bytea)
        RETURNS bytea
        LANGUAGE sql IMMUTABLE
        SET search_path = pg_catalog, pg_temp
        AS $$ SELECT sha256(p_prev || int8send(p_seq) || uuid_send(p_record) || p_digest) $$
    """)

    # Append one record (write-ahead: callers append the intent before anything leaves). The
    # caller supplies the id; the database sets the time and computes the digest.
    op.execute(f"""
        CREATE FUNCTION audit.append(
            p_event jsonb,
            p_ciphertext bytea,
            p_blind_fields text[],
            p_blind_tokens bytea[]
        )
        RETURNS bytea
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := audit.require_tenant();
            r audit.record;
        BEGIN
            r.id := (p_event ->> 'id')::uuid;
            r.tenant_id := v_tenant;
            r.occurred_at := clock_timestamp();
            r.kind := p_event ->> 'kind';
            r.outcome := p_event ->> 'outcome';
            r.actor_id := (p_event ->> 'actor_id')::uuid;
            r.user_id := (p_event ->> 'user_id')::uuid;
            r.agent_id := (p_event ->> 'agent_id')::uuid;
            r.run_id := (p_event ->> 'run_id')::uuid;
            r.correlation_id := (p_event ->> 'correlation_id')::uuid;
            r.connector := p_event ->> 'connector';
            r.action := p_event ->> 'action';
            r.resource_ids := coalesce(
                ARRAY(SELECT jsonb_array_elements_text(p_event -> 'resource_ids')), '{{}}'
            );
            r.detail := coalesce(p_event -> 'detail', '{{}}');
            r.subject_id := (p_event ->> 'subject_id')::uuid;
            r.key_version := (p_event ->> 'key_version')::integer;
            r.ciphertext := p_ciphertext;
            r.digest := sha256(audit.canonical(r));

            INSERT INTO audit.record SELECT r.*;
            INSERT INTO audit.pending (tenant_id, record_id) VALUES (v_tenant, r.id);

            IF coalesce(cardinality(p_blind_fields), 0) <> coalesce(cardinality(p_blind_tokens), 0)
                    OR (cardinality(p_blind_fields) > 0 AND r.subject_id IS NULL) THEN
                RAISE EXCEPTION 'blind index entries need a subject and one token per field'
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            INSERT INTO audit.blind_index (tenant_id, principal_id, record_id, field, token)
            SELECT v_tenant, r.subject_id, r.id, f.field, f.token
            FROM unnest(p_blind_fields, p_blind_tokens) AS f (field, token);

            RETURN r.digest;
        END
        $$
    """)

    # Link this tenant's pending records into its chain. The head row's lock means one sealer
    # per tenant at a time; records whose transactions haven't committed wait for the next run.
    op.execute(f"""
        CREATE FUNCTION audit.seal(p_limit integer) RETURNS integer
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := audit.require_tenant();
            v_seq bigint;
            v_hash bytea;
            v_count integer := 0;
            p record;
        BEGIN
            INSERT INTO audit.chain (tenant_id, last_seq, last_hash)
            VALUES (v_tenant, 0, {GENESIS})
            ON CONFLICT (tenant_id) DO NOTHING;
            SELECT c.last_seq, c.last_hash INTO v_seq, v_hash
            FROM audit.chain c WHERE c.tenant_id = v_tenant FOR UPDATE;

            FOR p IN
                SELECT r.id, r.digest
                FROM audit.pending q JOIN audit.record r ON r.id = q.record_id
                ORDER BY r.id
                LIMIT least(greatest(p_limit, 0), 10000)
            LOOP
                INSERT INTO audit.seal (tenant_id, chain_seq, record_id, prev_hash, hash, sealed_at)
                VALUES (
                    v_tenant, v_seq + 1, p.id, v_hash,
                    audit.link(v_hash, v_seq + 1, p.id, p.digest), clock_timestamp()
                );
                v_seq := v_seq + 1;
                v_hash := audit.link(v_hash, v_seq, p.id, p.digest);
                DELETE FROM audit.pending WHERE record_id = p.id;
                v_count := v_count + 1;
            END LOOP;

            UPDATE audit.chain SET last_seq = v_seq, last_hash = v_hash
            WHERE tenant_id = v_tenant;
            RETURN v_count;
        END
        $$
    """)

    # Recompute every digest and link of this tenant's chain. Returns 'ok', or the first
    # problem and the sequence number where it was found.
    op.execute(f"""
        CREATE FUNCTION audit.verify()
        RETURNS TABLE (status text, chain_seq bigint)
        LANGUAGE plpgsql STABLE {DEFINER}
        AS $$
        #variable_conflict use_column
        DECLARE
            v_tenant uuid := audit.require_tenant();
            v_prev bytea := {GENESIS};
            v_expected bigint := 1;
            v_head audit.chain;
            s record;
        BEGIN
            FOR s IN
                SELECT k.chain_seq AS seq, k.record_id, k.prev_hash, k.hash,
                       r.id AS found, r.digest, sha256(audit.canonical(r)) AS recomputed
                FROM audit.seal k LEFT JOIN audit.record r ON r.id = k.record_id
                ORDER BY k.chain_seq
            LOOP
                IF s.seq <> v_expected THEN
                    RETURN QUERY SELECT 'gap'::text, v_expected; RETURN;
                END IF;
                IF s.prev_hash <> v_prev THEN
                    RETURN QUERY SELECT 'broken_link'::text, s.seq; RETURN;
                END IF;
                IF s.found IS NULL THEN
                    RETURN QUERY SELECT 'record_missing'::text, s.seq; RETURN;
                END IF;
                IF s.recomputed <> s.digest THEN
                    RETURN QUERY SELECT 'record_altered'::text, s.seq; RETURN;
                END IF;
                IF audit.link(v_prev, s.seq, s.record_id, s.digest) <> s.hash THEN
                    RETURN QUERY SELECT 'seal_altered'::text, s.seq; RETURN;
                END IF;
                v_prev := s.hash;
                v_expected := v_expected + 1;
            END LOOP;

            SELECT * INTO v_head FROM audit.chain c WHERE c.tenant_id = v_tenant;
            IF coalesce(v_head.last_seq, 0) <> v_expected - 1
                    OR coalesce(v_head.last_hash, {GENESIS}) <> v_prev THEN
                RETURN QUERY SELECT 'head_mismatch'::text, v_expected - 1; RETURN;
            END IF;
            IF EXISTS (
                SELECT 1 FROM audit.record r
                WHERE NOT EXISTS (SELECT 1 FROM audit.seal k WHERE k.record_id = r.id)
                  AND NOT EXISTS (SELECT 1 FROM audit.pending q WHERE q.record_id = r.id)
            ) THEN
                RETURN QUERY SELECT 'unsealed_record'::text, NULL::bigint; RETURN;
            END IF;
            RETURN QUERY SELECT 'ok'::text, v_expected - 1;
        END
        $$
    """)

    # Keys. A person's data key is created on first use; an erased person never gets one again.
    op.execute(f"""
        CREATE FUNCTION audit.person_key(p_principal uuid)
        RETURNS TABLE (version integer, wrapped_key bytea)
        LANGUAGE sql STABLE {DEFINER}
        AS $$
            SELECT k.version, k.wrapped_key FROM audit.person_key k
            WHERE k.principal_id = p_principal AND k.tenant_id = audit.require_tenant()
            ORDER BY k.version DESC LIMIT 1
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION audit.add_person_key(p_principal uuid, p_wrapped bytea)
        RETURNS boolean
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := audit.require_tenant();
        BEGIN
            IF EXISTS (SELECT 1 FROM audit.erased e WHERE e.principal_id = p_principal) THEN
                RETURN false;
            END IF;
            INSERT INTO audit.person_key (tenant_id, principal_id, version, wrapped_key)
            VALUES (v_tenant, p_principal, 1, p_wrapped)
            ON CONFLICT DO NOTHING;
            RETURN true;
        END
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION audit.blind_key() RETURNS bytea
        LANGUAGE sql STABLE {DEFINER}
        AS $$
            SELECT k.wrapped_key FROM audit.tenant_key k
            WHERE k.purpose = 'blind_index' AND k.tenant_id = audit.require_tenant()
        $$
    """)
    op.execute(f"""
        CREATE FUNCTION audit.add_blind_key(p_wrapped bytea) RETURNS void
        LANGUAGE sql {DEFINER}
        AS $$
            INSERT INTO audit.tenant_key (tenant_id, purpose, wrapped_key)
            VALUES (audit.require_tenant(), 'blind_index', p_wrapped)
            ON CONFLICT DO NOTHING
        $$
    """)

    op.execute(f"""
        CREATE FUNCTION audit.find(p_field text, p_token bytea) RETURNS SETOF uuid
        LANGUAGE sql STABLE {DEFINER}
        AS $$
            SELECT b.record_id FROM audit.blind_index b
            WHERE b.tenant_id = audit.require_tenant() AND b.field = p_field AND b.token = p_token
        $$
    """)

    # Crypto-shred (SEC-07): destroy the person's keys and blind-index entries. Their records
    # stay, unreadable, and the chain still verifies because it covers only ciphertext.
    op.execute(f"""
        CREATE FUNCTION audit.shred_person(p_principal uuid)
        RETURNS TABLE (keys_destroyed integer, index_entries_deleted integer)
        LANGUAGE plpgsql {DEFINER}
        AS $$
        DECLARE
            v_tenant uuid := audit.require_tenant();
            v_keys integer;
            v_entries integer;
        BEGIN
            DELETE FROM audit.person_key WHERE principal_id = p_principal;
            GET DIAGNOSTICS v_keys = ROW_COUNT;
            DELETE FROM audit.blind_index WHERE principal_id = p_principal;
            GET DIAGNOSTICS v_entries = ROW_COUNT;
            INSERT INTO audit.erased (tenant_id, principal_id) VALUES (v_tenant, p_principal)
            ON CONFLICT DO NOTHING;
            RETURN QUERY SELECT v_keys, v_entries;
        END
        $$
    """)

    writers = "app_user, app_worker, gw"
    op.execute(
        f"GRANT EXECUTE ON FUNCTION audit.append(jsonb, bytea, text[], bytea[]) TO {writers}"
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION audit.person_key(uuid) TO {writers}")
    op.execute(f"GRANT EXECUTE ON FUNCTION audit.add_person_key(uuid, bytea) TO {writers}")
    op.execute(f"GRANT EXECUTE ON FUNCTION audit.blind_key() TO {writers}")
    op.execute(f"GRANT EXECUTE ON FUNCTION audit.add_blind_key(bytea) TO {writers}")
    op.execute("GRANT EXECUTE ON FUNCTION audit.find(text, bytea) TO app_user")
    op.execute("GRANT EXECUTE ON FUNCTION audit.seal(integer) TO app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION audit.verify() TO app_worker")
    op.execute("GRANT EXECUTE ON FUNCTION audit.shred_person(uuid) TO app_worker")


def downgrade() -> None:
    raise NotImplementedError("Collicity migrations are forward-only")
