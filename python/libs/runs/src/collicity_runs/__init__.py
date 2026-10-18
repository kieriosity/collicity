"""Runs: admission, state transitions, the run kill switch and the outbox relay.

Postgres owns every run's state (technical design §4.1). These are thin wrappers over the
``runtime`` schema's functions; run them on a ``collicity_db.tenant_connection``. Who may call
what is enforced by database grants:

- triggers (worker) and people (app) admit runs; nothing else creates one (RUN-09);
- the worker moves a run between states, with optimistic concurrency on its version;
- people pause, resume and cancel runs through the app (ASG-13);
- the worker relays outbox events, at least once.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from collicity_core import TenantContext, new_id
from collicity_db import tenant_connection


class State(StrEnum):
    """Run states (spec §7.6)."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PERSON = "waiting_person"
    WAITING_TIMER = "waiting_timer"
    PAUSED_BUDGET = "paused_budget"
    PAUSED_REAUTHORIZATION = "paused_reauthorization"
    PAUSED_REACCEPTANCE = "paused_reacceptance"
    PAUSED_OUTCOME_UNKNOWN = "paused_outcome_unknown"
    PAUSED_BY_USER = "paused_by_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    COMPLETED_PARTIAL = "completed_partial"
    CANCELLED = "cancelled"


TERMINAL = frozenset(
    {State.SUCCEEDED, State.FAILED, State.ROLLED_BACK, State.COMPLETED_PARTIAL, State.CANCELLED}
)


class TriggerKind(StrEnum):
    SCHEDULE = "schedule"
    MAIL = "mail"
    WEBHOOK = "webhook"
    MANUAL = "manual"
    ACTION = "action"  # carries out a decided approval; admitted first (RUN-01)


class Admission(StrEnum):
    STARTED = "started"
    QUEUED = "queued"
    AWAITING_ACCEPTANCE = "awaiting_acceptance"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    QUEUE_FULL = "queue_full"
    TOO_OLD = "too_old"
    ARCHIVED = "archived"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class Admitted:
    status: Admission
    run_id: UUID | None


def admit(
    conn: Connection,
    *,
    assignment_id: UUID,
    kind: TriggerKind,
    dedup_key: str,
    event_at: datetime | None = None,
    keep: timedelta = timedelta(days=30),
    run_id: UUID | None = None,
    trigger_id: UUID | None = None,
) -> Admitted:
    """Admit one trigger event (RUN-01 to RUN-03).

    ``dedup_key`` is the provider's event or delivery ID, or the mail provider's immutable
    message ID, never a sender-chosen Message-ID (RUN-02). It is kept for ``keep``: at least as
    long as the source can redeliver, and 90 days for mail in the beta.
    """
    row = conn.execute(
        text(
            "SELECT status, run_id FROM runtime.admit("
            ":trigger, :run, :assignment, :kind, :key, :event_at, :keep, :priority)"
        ),
        {
            "trigger": trigger_id or new_id(),
            "run": run_id or new_id(),
            "assignment": assignment_id,
            "kind": TriggerKind(kind).value,
            "key": dedup_key,
            "event_at": event_at,
            "keep": keep,
            "priority": 1 if kind == TriggerKind.ACTION else 0,
        },
    ).one()
    return Admitted(Admission(row.status), row.run_id)


@dataclass(frozen=True)
class RunState:
    state: State
    version: int
    segment: int
    continuation: Mapping[str, Any]
    cancel_requested: bool
    effects_applied: int


def get(conn: Connection, run_id: UUID) -> RunState:
    row = conn.execute(
        text(
            "SELECT state, state_version, segment_no, continuation, "
            "cancel_requested_at IS NOT NULL AS cancel_requested, effects_applied "
            "FROM run WHERE id = :id"
        ),
        {"id": run_id},
    ).one()
    return RunState(
        State(row.state),
        row.state_version,
        row.segment_no,
        row.continuation,
        row.cancel_requested,
        row.effects_applied,
    )


# none_as_null: an omitted continuation is SQL NULL (keep the current one), not JSON null.
_TRANSITION = text(
    "SELECT runtime.transition(:run, :version, :to, :continuation, :reason)"
).bindparams(bindparam("continuation", type_=JSONB(none_as_null=True)))


def transition(
    conn: Connection,
    run_id: UUID,
    *,
    expected_version: int,
    to: State,
    reason: str,
    continuation: Mapping[str, Any] | None = None,
) -> int:
    """Move a run to ``to`` if it is still at ``expected_version``; returns the new version.

    Raises if another transition got there first, or §7.6 doesn't allow the move, or the
    outcome would misstate what the run did to the world (RUN-08).
    """
    return int(
        conn.execute(
            _TRANSITION,
            {
                "run": run_id,
                "version": expected_version,
                "to": State(to).value,
                "continuation": None if continuation is None else dict(continuation),
                "reason": reason,
            },
        ).scalar_one()
    )


def requeue_after_acceptance(conn: Connection, assignment_id: UUID) -> int:
    """Return runs parked for an accepted version to the queue (RUN-03)."""
    return int(
        conn.execute(
            text("SELECT runtime.requeue_after_acceptance(:a)"), {"a": assignment_id}
        ).scalar_one()
    )


def pause_run(conn: Connection, run_id: UUID) -> State:
    return State(conn.execute(text("SELECT runtime.pause_run(:r)"), {"r": run_id}).scalar_one())


def resume_run(conn: Connection, run_id: UUID) -> State:
    return State(conn.execute(text("SELECT runtime.resume_run(:r)"), {"r": run_id}).scalar_one())


def cancel_run(conn: Connection, run_id: UUID) -> State:
    """Cancel now if the run applied nothing; otherwise roll back at its next step (RUN-06)."""
    return State(conn.execute(text("SELECT runtime.cancel_run(:r)"), {"r": run_id}).scalar_one())


# ---- Outbox relay ----------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxEvent:
    id: UUID
    tenant_id: UUID
    kind: str
    subject_id: UUID
    payload: Mapping[str, Any]
    seq: int


Dispatch = Callable[[OutboxEvent], None]


def claim(conn: Connection, tenant_id: UUID, limit: int = 100) -> list[OutboxEvent]:
    """Publish this tenant's new events (assigning sequence numbers in commit order) and return
    every published event not yet delivered, oldest first."""
    rows = conn.execute(
        text("SELECT id, kind, subject_id, payload, seq FROM runtime.claim_outbox(:n)"),
        {"n": limit},
    ).all()
    return [OutboxEvent(r.id, tenant_id, r.kind, r.subject_id, r.payload, r.seq) for r in rows]


def mark_delivered(conn: Connection, event_ids: Sequence[UUID]) -> int:
    if not event_ids:
        return 0
    return int(
        conn.execute(
            text("SELECT runtime.mark_delivered(:ids)"), {"ids": list(event_ids)}
        ).scalar_one()
    )


def relay_once(engine: Engine, dispatch: Dispatch, limit: int = 100) -> int:
    """One pass of the relay over every tenant with undelivered events (worker role).

    Delivery is at least once: an event is marked delivered only after ``dispatch`` returns,
    so a crash or an exception means it is dispatched again. Handlers must be idempotent
    (a segment start uses the workflow ID run:{id}:{segment}).
    """
    with engine.connect() as conn:
        tenants = list(
            conn.execute(text("SELECT * FROM sweep.tenants_with_outbox(:n)"), {"n": 1000}).scalars()
        )
    delivered = 0
    for tenant_id in tenants:
        ctx = TenantContext(tenant_id)
        with tenant_connection(engine, ctx) as conn:
            events = claim(conn, tenant_id, limit)
        done: list[UUID] = []
        try:
            for event in events:
                dispatch(event)
                done.append(event.id)
        finally:
            with tenant_connection(engine, ctx) as conn:
                delivered += mark_delivered(conn, done)
    return delivered
