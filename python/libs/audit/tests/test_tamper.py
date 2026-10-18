"""ADM-07: verification finds every way a privileged attacker could edit the log in place.

Each case edits the stored log directly as the database superuser, with triggers and foreign
keys off (``session_replication_role = replica``), which is more than any application role can
do. A forger who rewrites every digest and link from some point on still passes ``verify``:
the hourly KMS-signed anchors outside the database are what catch that (collicity-xrd.5.6).
"""

import pytest
from sqlalchemy import text

from collicity_audit import Event, Kind, append, seal, verify
from collicity_core import new_id
from collicity_db import Role
from collicity_testing.audit import AuditTenant
from collicity_testing.postgres import MigratedDatabase

RECORDS = 5


def _sealed_log(audit_tenant: AuditTenant) -> list[str]:
    ids: list[str] = []
    with audit_tenant.open(Role.GW) as (conn, keys):
        for n in range(RECORDS):
            event = Event(kind=Kind.MODEL_CALL, outcome="settled", detail={"n": n})
            append(conn, keys, event)
            ids.append(str(event.id))
    with audit_tenant.open(Role.APP_WORKER) as (conn, _):
        seal(conn)
        assert verify(conn).ok
    return ids


def _tamper(database: MigratedDatabase, *statements: str, **params: object) -> None:
    with database.admin_engine().begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        for statement in statements:
            conn.execute(text(statement), params)


def _verify(audit_tenant: AuditTenant) -> tuple[str, int | None]:
    with audit_tenant.open(Role.APP_WORKER) as (conn, _):
        result = verify(conn)
    return result.status, result.chain_seq


def _seq(database: MigratedDatabase, record_id: str) -> int:
    with database.admin_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT chain_seq FROM audit.seal WHERE record_id = :r"), {"r": record_id}
            ).scalar_one()
        )


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE audit.record SET outcome = 'denied' WHERE id = :r",
        "UPDATE audit.record SET detail = '{\"n\": 99}' WHERE id = :r",
        "UPDATE audit.record SET actor_id = gen_random_uuid() WHERE id = :r",
        "UPDATE audit.record SET occurred_at = occurred_at - interval '1 day' WHERE id = :r",
        "UPDATE audit.record SET resource_ids = '{other}' WHERE id = :r",
    ],
)
def test_an_edited_record_is_found(
    database: MigratedDatabase, audit_tenant: AuditTenant, change: str
):
    ids = _sealed_log(audit_tenant)
    _tamper(database, change, r=ids[2])
    assert _verify(audit_tenant) == ("record_altered", _seq(database, ids[2]))


def test_an_edited_record_with_a_recomputed_digest_breaks_its_seal(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    ids = _sealed_log(audit_tenant)
    _tamper(
        database,
        "UPDATE audit.record SET outcome = 'denied' WHERE id = :r",
        "UPDATE audit.record r SET digest = sha256(audit.canonical(r)) WHERE id = :r",
        r=ids[2],
    )
    assert _verify(audit_tenant) == ("seal_altered", _seq(database, ids[2]))


def test_a_removed_seal_leaves_a_gap(database: MigratedDatabase, audit_tenant: AuditTenant):
    ids = _sealed_log(audit_tenant)
    seq = _seq(database, ids[2])
    _tamper(database, "DELETE FROM audit.seal WHERE record_id = :r", r=ids[2])
    assert _verify(audit_tenant) == ("gap", seq)


def test_removing_the_newest_seals_is_found_at_the_head(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    _sealed_log(audit_tenant)
    # The newest seal by chain position, not the last record written: records written in the
    # same millisecond have UUIDv7 ids in no particular order, and seals follow record ids.
    _tamper(
        database,
        "DELETE FROM audit.seal WHERE tenant_id = :t AND chain_seq = :n",
        t=audit_tenant.tenant.id,
        n=RECORDS,
    )
    assert _verify(audit_tenant) == ("head_mismatch", RECORDS - 1)


def test_a_removed_record_is_found(database: MigratedDatabase, audit_tenant: AuditTenant):
    ids = _sealed_log(audit_tenant)
    seq = _seq(database, ids[1])
    _tamper(database, "DELETE FROM audit.record WHERE id = :r", r=ids[1])
    assert _verify(audit_tenant) == ("record_missing", seq)


def test_a_relinked_seal_is_found(database: MigratedDatabase, audit_tenant: AuditTenant):
    ids = _sealed_log(audit_tenant)
    _tamper(
        database,
        "UPDATE audit.seal SET prev_hash = sha256(prev_hash) WHERE record_id = :r",
        r=ids[3],
    )
    assert _verify(audit_tenant) == ("broken_link", _seq(database, ids[3]))


def test_a_record_dropped_from_the_queue_before_sealing_is_found(
    database: MigratedDatabase, audit_tenant: AuditTenant
):
    _sealed_log(audit_tenant)
    event = Event(kind=Kind.ADMIN_CHANGE, detail={"setting": "budget"})
    with audit_tenant.open(Role.APP_USER) as (conn, keys):
        append(conn, keys, event)
    _tamper(database, "DELETE FROM audit.pending WHERE record_id = :r", r=str(event.id))
    assert _verify(audit_tenant)[0] == "unsealed_record"


def test_swapping_two_records_is_found(database: MigratedDatabase, audit_tenant: AuditTenant):
    ids = _sealed_log(audit_tenant)
    placeholder = str(new_id())
    _tamper(
        database,
        "UPDATE audit.seal SET record_id = :x WHERE record_id = :a",
        "UPDATE audit.seal SET record_id = :a WHERE record_id = :b",
        "UPDATE audit.seal SET record_id = :b WHERE record_id = :x",
        a=ids[1],
        b=ids[2],
        x=placeholder,
    )
    status, _ = _verify(audit_tenant)
    assert status == "seal_altered"
