"""ADM-07, SEC-07, MR-16: an append-only, hash-chained log that holds no personal data in clear."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from collicity_audit import (
    Event,
    Keys,
    Kind,
    append,
    find,
    read_personal,
    seal,
    shred_person,
    verify,
)
from collicity_audit.log import PayloadError, check_detail, tenants_to_seal
from collicity_core import TenantContext, new_id
from collicity_db import Role, make_engine, tenant_connection
from collicity_testing.audit import WRAPPER, AuditTenant
from collicity_testing.postgres import MigratedDatabase, seed_tenant


def _intent(**fields: object) -> Event:
    base: dict[str, object] = {
        "kind": Kind.CONNECTOR_CALL_INTENT,
        "outcome": "allowed",
        "connector": "graph",
        "action": "excel.update_row",
        "resource_ids": ["drive-item-inventory", "table-stock"],
        "detail": {"params_hash": "ab" * 32, "grant": "step-1", "records": 1},
    }
    return Event(**(base | fields))  # type: ignore[arg-type]


def test_appended_records_are_sealed_into_a_chain_that_verifies(audit_tenant: AuditTenant):
    with audit_tenant.open(Role.GW) as (conn, keys):
        for _ in range(5):
            append(conn, keys, _intent())
    with audit_tenant.open(Role.APP_WORKER) as (conn, _):
        assert seal(conn, limit=3) == 3
        assert seal(conn) == 2
        assert seal(conn) == 0
        result = verify(conn)
    assert result.ok
    assert result.chain_seq == 5


def test_a_record_commits_with_its_transaction_or_not_at_all(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    event = _intent()

    def call_that_fails() -> None:
        with audit_tenant.open(Role.GW) as (conn, keys):
            append(conn, keys, event)
            raise RuntimeError("the call was never made")

    with pytest.raises(RuntimeError):
        call_that_fails()
    with database.admin_engine().connect() as conn:
        found = conn.execute(
            text("SELECT count(*) FROM audit.record WHERE id = :id"), {"id": event.id}
        ).scalar_one()
    assert found == 0


def test_the_database_sets_the_time_and_the_digest(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    event = _intent()
    with audit_tenant.open(Role.GW) as (conn, keys):
        digest = append(conn, keys, event)
    with database.admin_engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT occurred_at, digest, sha256(audit.canonical(r)) AS again "
                "FROM audit.record r WHERE id = :id"
            ),
            {"id": event.id},
        ).one()
    assert row.digest == digest == row.again
    assert row.occurred_at is not None


def test_verification_does_not_depend_on_the_session_time_zone(audit_tenant: AuditTenant):
    with audit_tenant.open(Role.GW) as (conn, keys):
        append(conn, keys, _intent())
    for zone in ("UTC", "Pacific/Chatham", "America/St_Johns"):
        with audit_tenant.open(Role.APP_WORKER) as (conn, _):
            conn.execute(text(f"SET LOCAL TIME ZONE '{zone}'"))
            seal(conn)
            assert verify(conn).ok, zone


def test_personal_fields_are_encrypted_readable_and_searchable(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    person = audit_tenant.tenant.principal_ids[1]
    event = Event(
        kind=Kind.APPROVAL_DECIDED,
        outcome="approved",
        actor_id=person,
        subject_id=person,
        personal={"email": "Dana.Approver@Example.com", "ip": "203.0.113.9"},
        searchable=frozenset({"email"}),
        detail={"approval_id": str(new_id()), "auth_method": "webauthn"},
    )
    with audit_tenant.open(Role.APP_USER) as (conn, keys):
        append(conn, keys, event)

    with database.admin_engine().connect() as conn:
        stored = conn.execute(
            text("SELECT ciphertext, detail::text FROM audit.record WHERE id = :id"),
            {"id": event.id},
        ).one()
    assert b"Dana" not in stored.ciphertext
    assert b"203.0.113.9" not in stored.ciphertext
    assert "Dana" not in stored.detail

    with audit_tenant.open(Role.APP_USER) as (conn, keys):
        assert read_personal(conn, keys, event.id) == {
            "email": "Dana.Approver@Example.com",
            "ip": "203.0.113.9",
        }
        assert find(conn, keys, "email", "  dana.approver@example.COM ") == [event.id]
        assert find(conn, keys, "email", "someone@else.example") == []
        assert find(conn, keys, "ip", "203.0.113.9") == []  # not searchable


def test_after_crypto_shredding_the_data_is_gone_and_the_chain_still_verifies(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    person = audit_tenant.tenant.principal_ids[1]
    event = Event(
        kind=Kind.APPROVAL_DECIDED,
        subject_id=person,
        personal={"email": "dana@example.com"},
        searchable=frozenset({"email"}),
    )
    with audit_tenant.open(Role.APP_USER) as (conn, keys):
        append(conn, keys, event)
        append(conn, keys, _intent())
    with audit_tenant.open(Role.APP_WORKER) as (conn, _):
        seal(conn)
        shredded = shred_person(conn, person)
    assert (shredded.keys_destroyed, shredded.index_entries_deleted) == (1, 1)

    with audit_tenant.open(Role.APP_USER) as (conn, keys):
        assert read_personal(conn, keys, event.id) is None
        assert find(conn, keys, "email", "dana@example.com") == []
        # An erased person's data is never written again.
        later = Event(kind=Kind.ADMIN_CHANGE, subject_id=person, personal={"email": "dana@x"})
        append(conn, keys, later)
    with database.admin_engine().connect() as conn:
        assert conn.execute(
            text("SELECT ciphertext IS NULL FROM audit.record WHERE id = :id"), {"id": later.id}
        ).scalar_one()

    with audit_tenant.open(Role.APP_WORKER) as (conn, _):
        seal(conn)
        assert verify(conn).ok


def test_a_ciphertext_cannot_be_read_as_another_record(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    person = audit_tenant.tenant.principal_ids[1]
    first = Event(kind=Kind.ADMIN_CHANGE, subject_id=person, personal={"email": "a@x.example"})
    second = Event(kind=Kind.ADMIN_CHANGE, subject_id=person, personal={"email": "b@x.example"})
    with audit_tenant.open(Role.APP_USER) as (conn, keys):
        append(conn, keys, first)
        append(conn, keys, second)
    with database.admin_engine().begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            text(
                "UPDATE audit.record SET ciphertext = "
                "(SELECT ciphertext FROM audit.record WHERE id = :a) WHERE id = :b"
            ),
            {"a": first.id, "b": second.id},
        )
    with pytest.raises(InvalidTag), audit_tenant.open(Role.APP_USER) as (conn, keys):
        read_personal(conn, keys, second.id)


def test_tenants_seal_and_verify_their_own_chains(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    other = seed_tenant(database)
    with audit_tenant.open(Role.GW) as (conn, keys):
        append(conn, keys, _intent())
    gw = database.engine(Role.GW)
    with tenant_connection(gw, TenantContext(other.id)) as conn:
        append(conn, Keys(WRAPPER, other.id), _intent())

    with database.engine(Role.APP_WORKER).connect() as conn:
        due = set(tenants_to_seal(conn))
    assert {audit_tenant.tenant.id, other.id} <= due

    with audit_tenant.open(Role.APP_WORKER) as (conn, _):
        assert seal(conn) == 1  # only its own record
    worker = database.engine(Role.APP_WORKER)
    with tenant_connection(worker, TenantContext(other.id)) as conn:
        assert seal(conn) == 1
        assert verify(conn).ok


def test_concurrent_writers_and_sealers_build_one_gapless_chain(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    writers, per_writer = 6, 25
    ctx = TenantContext(audit_tenant.tenant.id)
    gw = make_engine(database.urls[Role.GW], pool_size=writers, max_overflow=0)
    worker = make_engine(database.urls[Role.APP_WORKER], pool_size=2, max_overflow=0)
    done = threading.Event()

    def write(_: int) -> None:
        for _ in range(per_writer):
            with tenant_connection(gw, ctx) as conn:
                append(conn, Keys(WRAPPER, ctx.tenant_id), _intent())

    def seal_until_done() -> int:
        sealed = 0
        while not done.is_set():
            with tenant_connection(worker, ctx) as conn:
                sealed += seal(conn, limit=7)
        return sealed

    try:
        with ThreadPoolExecutor(writers + 2) as pool:
            sealers = [pool.submit(seal_until_done) for _ in range(2)]
            list(pool.map(write, range(writers)))
            done.set()
            sealed = sum(s.result() for s in sealers)
        with tenant_connection(worker, ctx) as conn:
            sealed += seal(conn, limit=10_000)
            result = verify(conn)
    finally:
        gw.dispose()
        worker.dispose()

    assert sealed == writers * per_writer
    assert result.ok
    assert result.chain_seq == writers * per_writer


@pytest.mark.parametrize(
    ("role", "statement"),
    [
        (role, statement)
        for role in (Role.MIGRATOR,)
        for statement in (
            "UPDATE audit.record SET outcome = 'denied'",
            "DELETE FROM audit.record",
            "TRUNCATE audit.record CASCADE",
            "UPDATE audit.seal SET sealed_at = now()",
            "DELETE FROM audit.seal",
            "TRUNCATE audit.seal",
        )
    ],
)
def test_nobody_can_change_or_remove_audit_rows_not_even_migrator(
    database: MigratedDatabase, audit_tenant: AuditTenant, role: Role, statement: str
):
    with audit_tenant.open(Role.GW) as (conn, keys):
        append(conn, keys, _intent())
    with audit_tenant.open(Role.APP_WORKER) as (conn, _):
        seal(conn)
    with (
        pytest.raises(ProgrammingError, match="immutable"),
        database.engine(role).begin() as conn,
    ):
        conn.execute(text(statement))


# ---- What a record may hold -----------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        {"prompt": "Ignore previous instructions and " + "x" * 200},
        {"payload": {"nested": "object"}},
        {"rows": list(range(100))},
        {"Bad-Key": "x"},
        {"blob": b"bytes"},
    ],
)
def test_details_hold_identifiers_and_hashes_never_payloads(detail: dict[str, object]):
    with pytest.raises(PayloadError):
        check_detail(detail)


def test_details_accept_identifiers_hashes_numbers_and_flags():
    request_id = new_id()
    assert check_detail(
        {"request": request_id, "hash": "f" * 64, "count": 3, "dry_run": False, "ids": ["a", "b"]}
    ) == {
        "request": str(request_id),
        "hash": "f" * 64,
        "count": 3,
        "dry_run": False,
        "ids": ["a", "b"],
    }


def test_personal_fields_need_a_subject():
    with pytest.raises(ValueError, match="subject"):
        append(None, None, Event(kind=Kind.ADMIN_CHANGE, personal={"email": "x"}))  # type: ignore[arg-type]
