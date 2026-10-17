"""Assignments and their immutable versions, step grants, and the kill switch.

Every function runs on a connection from ``collicity_db.tenant_connection``, in the caller's
transaction. Which role may call which is enforced by database grants:

- the app creates assignments and drafts versions as their owner, accepts versions, and works
  the kill switch;
- the worker reads versions, and issues and ends step grants;
- the gateway checks a step grant before every call.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from collicity_core import new_id
from collicity_grants import widens
from collicity_grants.models import CapabilityGrant


def create_assignment(
    conn: Connection, *, owner_id: UUID, name: str, assignment_id: UUID | None = None
) -> UUID:
    assignment_id = assignment_id or new_id()
    conn.execute(
        text(
            "INSERT INTO assignment (id, tenant_id, owner_id, name) "
            "VALUES (:id, current_tenant_id(), :owner, :name)"
        ),
        {"id": assignment_id, "owner": owner_id, "name": name},
    )
    return assignment_id


_INSERT_VERSION = text(
    "INSERT INTO assignment_version "
    "(id, tenant_id, assignment_id, number, grants, task_graph, taint, created_by) "
    "VALUES (:id, current_tenant_id(), :assignment, :number, :grants, :graph, :taint, :by)"
).bindparams(bindparam("grants", type_=JSONB), bindparam("graph", type_=JSONB))


def create_version(
    conn: Connection,
    *,
    assignment_id: UUID,
    number: int,
    grants: Sequence[CapabilityGrant],
    created_by: UUID,
    task_graph: Mapping[str, Any] | None = None,
    taint: Literal["trusted", "untrusted"] = "trusted",
    version_id: UUID | None = None,
) -> UUID:
    """Save a draft version. It's immutable from here; only acceptance makes it current."""
    version_id = version_id or new_id()
    conn.execute(
        _INSERT_VERSION,
        {
            "id": version_id,
            "assignment": assignment_id,
            "number": number,
            "grants": [g.model_dump(mode="json") for g in grants],
            "graph": dict(task_graph or {}),
            "taint": taint,
            "by": created_by,
        },
    )
    return version_id


def version_grants(conn: Connection, version_id: UUID) -> list[CapabilityGrant]:
    raw = conn.execute(
        text("SELECT grants FROM assignment_version WHERE id = :id"), {"id": version_id}
    ).scalar_one()
    return [CapabilityGrant.model_validate(g) for g in raw]


def current_version(conn: Connection, assignment_id: UUID) -> UUID | None:
    return conn.execute(
        text("SELECT current_version_id FROM assignment WHERE id = :id"), {"id": assignment_id}
    ).scalar_one()


_ACCEPT = text("SELECT standing.accept_version(:id, :widening)").bindparams(
    bindparam("widening", type_=JSONB)
)


def accept(conn: Connection, version_id: UUID) -> list[str]:
    """Accept a version as its owner and make it current (RUN-03, §8.2).

    Returns, and records, every way it widens the version accepted before it; that is what the
    acceptance screen shows.
    """
    assignment_id = conn.execute(
        text("SELECT assignment_id FROM assignment_version WHERE id = :id"), {"id": version_id}
    ).scalar_one()
    previous = current_version(conn, assignment_id)
    before = version_grants(conn, previous) if previous else []
    reasons = widens(before, version_grants(conn, version_id))
    conn.execute(_ACCEPT, {"id": version_id, "widening": reasons})
    return reasons


# ---- Step grants (CON-04) --------------------------------------------------------------------


class IssueStatus(StrEnum):
    ISSUED = "issued"
    NOT_FOUND = "not_found"
    NOT_ACCEPTED = "not_accepted"
    NOT_IN_VERSION = "not_in_version"
    ARCHIVED = "archived"
    TENANT_INACTIVE = "tenant_inactive"
    TENANT_PAUSED = "tenant_paused"
    OWNER_INACTIVE = "owner_inactive"
    OWNER_PAUSED = "owner_paused"
    ASSIGNMENT_PAUSED = "assignment_paused"


class CheckStatus(StrEnum):
    VALID = "valid"
    NOT_FOUND = "not_found"
    ENDED = "ended"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    TENANT_INACTIVE = "tenant_inactive"
    TENANT_PAUSED = "tenant_paused"
    OWNER_INACTIVE = "owner_inactive"
    OWNER_PAUSED = "owner_paused"
    ASSIGNMENT_PAUSED = "assignment_paused"


@dataclass(frozen=True)
class Issued:
    status: IssueStatus
    grant_id: UUID


@dataclass(frozen=True)
class StepGrant:
    """What the gateway learns from a valid step grant; empty unless ``status`` is VALID."""

    status: CheckStatus
    owner_id: UUID | None = None
    assignment_id: UUID | None = None
    version_id: UUID | None = None
    run_id: UUID | None = None
    grants: list[CapabilityGrant] = field(default_factory=list[CapabilityGrant])
    dry_run: bool = False

    @property
    def valid(self) -> bool:
        return self.status is CheckStatus.VALID


def issue_step_grant(
    conn: Connection,
    *,
    assignment_id: UUID,
    version_id: UUID,
    step_key: str,
    attempt: int = 1,
    actions: Sequence[str] | None = None,
    dry_run: bool = False,
    ttl: timedelta = timedelta(minutes=10),
    run_id: UUID | None = None,
    grant_id: UUID | None = None,
) -> Issued:
    """Issue a grant for one step and attempt, limited to ``actions`` of the accepted version.

    The database records the tenant, owner and assignment epochs; the caller supplies none.
    """
    grant_id = grant_id or new_id()
    status = conn.execute(
        text(
            "SELECT runtime.issue_step_grant(:id, :assignment, :version, :run, :step, "
            ":attempt, :actions, :dry_run, :ttl)"
        ),
        {
            "id": grant_id,
            "assignment": assignment_id,
            "version": version_id,
            "run": run_id,
            "step": step_key,
            "attempt": attempt,
            "actions": None if actions is None else list(actions),
            "dry_run": dry_run,
            "ttl": ttl,
        },
    ).scalar_one()
    return Issued(IssueStatus(status), grant_id)


def check_step_grant(conn: Connection, grant_id: UUID) -> StepGrant:
    """The gateway's check before a call (§5.1 step 1). Fails once any recorded epoch moves."""
    row = conn.execute(
        text(
            "SELECT status, owner_id, assignment_id, version_id, run_id, grants, dry_run "
            "FROM runtime.check_step_grant(:id)"
        ),
        {"id": grant_id},
    ).one()
    status = CheckStatus(row.status)
    if status is not CheckStatus.VALID:
        return StepGrant(status)
    return StepGrant(
        status,
        owner_id=row.owner_id,
        assignment_id=row.assignment_id,
        version_id=row.version_id,
        run_id=row.run_id,
        grants=[CapabilityGrant.model_validate(g) for g in row.grants],
        dry_run=row.dry_run,
    )


def end_step_grant(conn: Connection, grant_id: UUID) -> bool:
    """End a grant when its step ends. Returns False if it had already ended."""
    return bool(
        conn.execute(text("SELECT runtime.end_step_grant(:id)"), {"id": grant_id}).scalar_one()
    )


# ---- Kill switch (ASG-13) --------------------------------------------------------------------

Scope = Literal["tenant", "principal", "assignment"]
EPOCH_CHANNEL = "collicity_epochs"


def pause(conn: Connection, scope: Scope, target: UUID | None = None) -> None:
    """Pause runs for the tenant (admins), a person (admins, or that person) or an assignment
    (admins, or its owner). Every step grant issued before stops working at once."""
    _switch(conn, "pause", scope, target)


def resume(conn: Connection, scope: Scope, target: UUID | None = None) -> None:
    """Lift a pause. A person can lift only a pause they set themselves."""
    _switch(conn, "resume", scope, target)


def _switch(conn: Connection, action: str, scope: Scope, target: UUID | None) -> None:
    if scope == "tenant":
        conn.execute(text(f"SELECT runtime.{action}_tenant()"))
        return
    if target is None:
        raise ValueError(f"pausing a {scope} needs its id")
    conn.execute(text(f"SELECT runtime.{action}_{scope}(:target)"), {"target": target})
