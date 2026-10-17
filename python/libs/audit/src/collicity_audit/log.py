"""Writing, sealing, verifying, searching and shredding the audit log (ADM-07, SEC-07, MR-16).

Use a connection from ``collicity_db.tenant_connection``: every function here acts on the
tenant in context, and the database refuses to run without one. ``append`` runs in the caller's
transaction, so an intent record commits with the work it describes, or not at all (the
write-ahead rule of §5.1).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, TEXT

from collicity_audit import crypto
from collicity_core import new_id


class Kind(StrEnum):
    CONNECTOR_CALL_INTENT = "connector_call_intent"
    CONNECTOR_CALL_RESULT = "connector_call_result"
    CONNECTOR_CALL_DENIED = "connector_call_denied"
    MODEL_CALL = "model_call"
    DATA_ACCESS = "data_access"
    APPROVAL_DECIDED = "approval_decided"
    APPROVAL_CONSUMED = "approval_consumed"
    BUDGET_AUTHORIZATION = "budget_authorization"
    ADMIN_CHANGE = "admin_change"
    VERSION_ACCEPTED = "version_accepted"
    REVOCATION = "revocation"
    KILL_SWITCH = "kill_switch"
    SNAPSHOT_EVENT = "snapshot_event"
    SHARE_EVENT = "share_event"
    ERASURE = "erasure"


class PayloadError(ValueError):
    """A detail value looks like content rather than an identifier or a hash (SEC-07)."""


DETAIL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
MAX_DETAIL_TEXT = 128
MAX_DETAIL_ITEMS = 32


def _check_scalar(key: str, value: Any) -> None:
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, str):
        if len(value) > MAX_DETAIL_TEXT:
            raise PayloadError(f"detail {key} is {len(value)} characters; identifiers only")
        return
    raise PayloadError(f"detail {key} must be an identifier, number or flag")


def _clean(key: str, value: object) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list | tuple):
        items = [str(v) if isinstance(v, UUID) else v for v in cast(Sequence[object], value)]
        if len(items) > MAX_DETAIL_ITEMS:
            raise PayloadError(f"detail {key} has too many items")
        for item in items:
            _check_scalar(key, item)
        return items
    _check_scalar(key, value)
    return value


def check_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only short identifiers, hashes, numbers and flags: never payloads or prompt text."""
    clean: dict[str, Any] = {}
    for key, value in detail.items():
        if not DETAIL_KEY.match(key):
            raise PayloadError(f"detail key {key!r} isn't a plain identifier")
        clean[key] = _clean(key, value)
    return clean


@dataclass(frozen=True)
class Event:
    """One audit record. Personal data goes only in ``personal``, which is encrypted."""

    kind: Kind
    outcome: str | None = None
    actor_id: UUID | None = None
    user_id: UUID | None = None
    agent_id: UUID | None = None
    run_id: UUID | None = None
    correlation_id: UUID | None = None
    connector: str | None = None
    action: str | None = None
    resource_ids: Sequence[str] = ()
    detail: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # Whose personal data ``personal`` is; its keys named in ``searchable`` get blind indexes.
    subject_id: UUID | None = None
    personal: Mapping[str, str] = field(default_factory=dict[str, str])
    searchable: frozenset[str] = frozenset()
    id: UUID = field(default_factory=new_id)


class Keys:
    """Unwraps and caches a tenant's audit data keys for the length of one unit of work."""

    def __init__(self, wrapper: crypto.KeyWrapper, tenant_id: UUID) -> None:
        self._wrapper = wrapper
        self.tenant_id = tenant_id
        self._people: dict[UUID, tuple[int, bytes] | None] = {}
        self._blind: bytes | None = None

    def person(self, conn: Connection, principal_id: UUID) -> tuple[int, bytes] | None:
        """The person's current (version, key), created on first use; None once erased."""
        if principal_id not in self._people:
            row = self._read_person(conn, principal_id)
            if row is None:
                wrapped = self._wrapper.wrap(self.tenant_id, "person", crypto.new_key())
                created = conn.execute(
                    text("SELECT audit.add_person_key(:p, :w)"), {"p": principal_id, "w": wrapped}
                ).scalar_one()
                row = self._read_person(conn, principal_id) if created else None
            self._people[principal_id] = row
        return self._people[principal_id]

    def existing_person(self, conn: Connection, principal_id: UUID) -> tuple[int, bytes] | None:
        """The person's key if one exists, without creating it."""
        return self._read_person(conn, principal_id)

    def _read_person(self, conn: Connection, principal_id: UUID) -> tuple[int, bytes] | None:
        row = conn.execute(
            text("SELECT version, wrapped_key FROM audit.person_key(:p)"), {"p": principal_id}
        ).first()
        if row is None:
            return None
        return int(row.version), self._wrapper.unwrap(
            self.tenant_id, "person", bytes(row.wrapped_key)
        )

    def blind(self, conn: Connection) -> bytes:
        if self._blind is None:
            wrapped = conn.execute(text("SELECT audit.blind_key()")).scalar_one_or_none()
            if wrapped is None:
                candidate = self._wrapper.wrap(self.tenant_id, "blind_index", crypto.new_key())
                conn.execute(text("SELECT audit.add_blind_key(:w)"), {"w": candidate})
                wrapped = conn.execute(text("SELECT audit.blind_key()")).scalar_one()
            self._blind = self._wrapper.unwrap(self.tenant_id, "blind_index", bytes(wrapped))
        return self._blind


_APPEND = text("SELECT audit.append(:event, :ciphertext, :fields, :tokens)").bindparams(
    bindparam("event", type_=JSONB),
    bindparam("ciphertext", type_=BYTEA),
    bindparam("fields", type_=ARRAY(TEXT)),
    bindparam("tokens", type_=ARRAY(BYTEA)),
)


def append(conn: Connection, keys: Keys, event: Event) -> bytes:
    """Write ``event`` in the caller's transaction and return its digest.

    Personal fields are encrypted under the subject's key before they leave this process. If
    the subject has been erased, they are dropped: an erased person's data isn't written again.
    """
    if event.searchable - set(event.personal):
        raise ValueError("searchable fields must be personal fields")
    if event.personal and event.subject_id is None:
        raise ValueError("personal fields need a subject")

    tenant_id = keys.tenant_id
    ciphertext: bytes | None = None
    subject_id = version = None
    fields: list[str] = []
    tokens: list[bytes] = []
    if event.personal and event.subject_id is not None:
        person = keys.person(conn, event.subject_id)
        if person is not None:
            version, key = person
            subject_id = event.subject_id
            ciphertext = crypto.encrypt_fields(
                key,
                tenant_id=tenant_id,
                record_id=event.id,
                subject_id=subject_id,
                version=version,
                fields=dict(event.personal),
            )
            blind_key = keys.blind(conn) if event.searchable else b""
            for name in sorted(event.searchable):
                fields.append(name)
                tokens.append(crypto.blind_token(blind_key, name, event.personal[name]))

    payload = {
        "id": str(event.id),
        "kind": Kind(event.kind).value,
        "outcome": event.outcome,
        "actor_id": _str(event.actor_id),
        "user_id": _str(event.user_id),
        "agent_id": _str(event.agent_id),
        "run_id": _str(event.run_id),
        "correlation_id": _str(event.correlation_id),
        "connector": event.connector,
        "action": event.action,
        "resource_ids": list(event.resource_ids),
        "detail": check_detail(event.detail),
        "subject_id": _str(subject_id),
        "key_version": version,
    }
    return bytes(
        conn.execute(
            _APPEND,
            {"event": payload, "ciphertext": ciphertext, "fields": fields, "tokens": tokens},
        ).scalar_one()
    )


def _str(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def read_personal(conn: Connection, keys: Keys, record_id: UUID) -> dict[str, Any] | None:
    """The record's personal fields, or None if it has none or its subject was shredded."""
    row = conn.execute(
        text(
            "SELECT tenant_id, subject_id, key_version, ciphertext FROM audit.record WHERE id = :id"
        ),
        {"id": record_id},
    ).first()
    if row is None or row.subject_id is None:
        return None
    person = keys.existing_person(conn, row.subject_id)
    if person is None or person[0] != row.key_version:
        return None
    return crypto.decrypt_fields(
        person[1],
        bytes(row.ciphertext),
        tenant_id=row.tenant_id,
        record_id=record_id,
        subject_id=row.subject_id,
        version=row.key_version,
    )


def find(conn: Connection, keys: Keys, field_name: str, value: str) -> list[UUID]:
    """Records whose searchable personal field equals ``value`` (normalized)."""
    token = crypto.blind_token(keys.blind(conn), field_name, value)
    return list(
        conn.execute(
            text("SELECT * FROM audit.find(:f, :t)"), {"f": field_name, "t": token}
        ).scalars()
    )


def seal(conn: Connection, limit: int = 1000) -> int:
    """Link this tenant's pending records into its chain; returns how many were sealed."""
    return int(conn.execute(text("SELECT audit.seal(:n)"), {"n": limit}).scalar_one())


def tenants_to_seal(conn: Connection, limit: int = 500) -> list[UUID]:
    """Tenants with records waiting to be sealed. Needs no tenant context (the sweeper's view)."""
    return list(
        conn.execute(
            text("SELECT * FROM sweep.tenants_with_unsealed_audit(:n)"), {"n": limit}
        ).scalars()
    )


@dataclass(frozen=True)
class Verification:
    status: str  # "ok", or the first problem found
    chain_seq: int | None  # the last sequence checked, or where the problem is

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def verify(conn: Connection) -> Verification:
    """Recompute every digest and link in this tenant's chain."""
    row = conn.execute(text("SELECT status, chain_seq FROM audit.verify()")).one()
    return Verification(row.status, row.chain_seq)


@dataclass(frozen=True)
class Shredded:
    keys_destroyed: int
    index_entries_deleted: int


def shred_person(conn: Connection, principal_id: UUID) -> Shredded:
    """Crypto-shred a person's audit data (SEC-07): their keys and blind-index entries go; their
    records stay, unreadable; the chain still verifies."""
    row = conn.execute(
        text("SELECT keys_destroyed, index_entries_deleted FROM audit.shred_person(:p)"),
        {"p": principal_id},
    ).one()
    return Shredded(row.keys_destroyed, row.index_entries_deleted)
