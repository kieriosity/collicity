"""Runs: admission (RUN-01 to RUN-03), §7.6 transitions, honest outcomes (RUN-08), the run kill
switch (ASG-13) and the outbox relay (§9)."""

import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import pytest
from sqlalchemy import Connection, insert, text
from sqlalchemy.exc import DBAPIError

from collicity_assignments import (
    CheckStatus,
    IssueStatus,
    accept,
    check_step_grant,
    create_assignment,
    create_version,
    issue_step_grant,
    pause,
)
from collicity_core import TenantContext
from collicity_db import Role, make_engine, tenant_connection
from collicity_db.models import RoleGrant
from collicity_grants.models import CapabilityGrant
from collicity_runs import (
    Admission,
    OutboxEvent,
    State,
    TriggerKind,
    admit,
    cancel_run,
    claim,
    get,
    pause_run,
    relay_once,
    requeue_after_acceptance,
    resume_run,
    transition,
)
from collicity_testing.postgres import MigratedDatabase, SeededTenant, seed_tenant

READ = CapabilityGrant(
    connector="graph",
    manifest_version="2026.10.1",
    action="excel.read_rows",
    resources={"workbook": frozenset({"w1"}), "table": frozenset({"t1"})},
    read_fields=frozenset({"Qty"}),
)
Policy = Literal["skip", "queue", "replace", "concurrent"]


@dataclass
class Scene:
    database: MigratedDatabase
    tenant: SeededTenant
    owner: UUID
    other: UUID
    admin: UUID
    keys: list[int] = field(default_factory=list[int])

    @contextmanager
    def as_(self, role: Role, person: UUID | None = None) -> Generator[Connection]:
        ctx = TenantContext(self.tenant.id, person)
        with tenant_connection(self.database.engine(role), ctx) as conn:
            yield conn

    def assignment(
        self,
        policy: Policy = "concurrent",
        max_running: int = 5,
        max_queued: int = 100,
        *,
        accepted: bool = True,
    ) -> tuple[UUID, UUID]:
        with self.as_(Role.APP_USER, self.owner) as conn:
            assignment = create_assignment(
                conn,
                owner_id=self.owner,
                name="Stock",
                concurrency_policy=policy,
                max_running=max_running,
                max_queued=max_queued,
            )
            version = create_version(
                conn, assignment_id=assignment, number=1, grants=[READ], created_by=self.owner
            )
            if accepted:
                accept(conn, version)
        return assignment, version

    def trigger(
        self,
        assignment: UUID,
        kind: TriggerKind = TriggerKind.MAIL,
        key: str | None = None,
        **kw: Any,
    ) -> tuple[Admission, UUID | None]:
        self.keys.append(len(self.keys))
        with self.as_(Role.APP_WORKER) as conn:
            result = admit(
                conn,
                assignment_id=assignment,
                kind=kind,
                dedup_key=key or f"msg-{len(self.keys)}",
                **kw,
            )
        return result.status, result.run_id

    def state(self, run: UUID) -> State:
        with self.as_(Role.APP_WORKER) as conn:
            return get(conn, run).state

    def move(self, run: UUID, to: State, **kw: Any) -> int:
        with self.as_(Role.APP_WORKER) as conn:
            version = get(conn, run).version
            return transition(conn, run, expected_version=version, to=to, reason="test", **kw)

    def effects(self, run: UUID, n: int) -> None:
        # Stands in for the effect journal (collicity-xrd.5.7), which will own this counter.
        with self.database.engine(Role.MIGRATOR).begin() as conn:
            conn.execute(
                text("UPDATE run SET effects_applied = :n WHERE id = :r"), {"n": n, "r": run}
            )


@pytest.fixture
def scene(database: MigratedDatabase) -> Scene:
    tenant = seed_tenant(database, principals=3)
    owner, other, admin = tenant.principal_ids
    with database.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            insert(RoleGrant).values(tenant_id=tenant.id, principal_id=admin, role="admin")
        )
    return Scene(database, tenant, owner, other, admin)


def _events(scene: Scene, run: UUID) -> list[tuple[str, dict[str, Any]]]:
    with scene.database.admin_engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT kind, payload FROM outbox_event WHERE subject_id = :r "
                "ORDER BY created_at, id"
            ),
            {"r": run},
        ).all()
    return [(r.kind, r.payload) for r in rows]


# ---- Admission ------------------------------------------------------------------------------


def test_a_trigger_starts_a_run_once(scene: Scene):
    assignment, _ = scene.assignment()
    status, run = scene.trigger(assignment, key="AAMkAGI2-immutable-id")
    assert status is Admission.STARTED
    assert run is not None
    assert scene.state(run) is State.RUNNING
    assert ("run.segment_ready", {"segment": 1, "version": 1}) in _events(scene, run)
    assert scene.trigger(assignment, key="AAMkAGI2-immutable-id") == (Admission.DUPLICATE, None)


def test_concurrent_runs_are_capped_and_the_rest_queue_in_order(scene: Scene):
    assignment, _ = scene.assignment(max_running=2)
    first, second, third, fourth = (scene.trigger(assignment) for _ in range(4))
    assert [s for s, _ in (first, second, third, fourth)] == [
        Admission.STARTED,
        Admission.STARTED,
        Admission.QUEUED,
        Admission.QUEUED,
    ]
    assert first[1]
    assert third[1]
    assert fourth[1]
    scene.move(first[1], State.SUCCEEDED)  # a slot frees; the oldest queued run starts
    assert scene.state(third[1]) is State.RUNNING
    assert scene.state(fourth[1]) is State.QUEUED


def test_a_full_queue_refuses_new_triggers(scene: Scene):
    assignment, _ = scene.assignment(max_running=1, max_queued=1)
    assert [scene.trigger(assignment)[0] for _ in range(3)] == [
        Admission.STARTED,
        Admission.QUEUED,
        Admission.QUEUE_FULL,
    ]


def test_skip_drops_a_trigger_while_a_run_is_active(scene: Scene):
    assignment, _ = scene.assignment(policy="skip", max_running=1)
    _, first = scene.trigger(assignment, kind=TriggerKind.SCHEDULE)
    assert scene.trigger(assignment, kind=TriggerKind.SCHEDULE) == (Admission.SKIPPED, None)
    assert first is not None
    scene.move(first, State.SUCCEEDED)
    assert scene.trigger(assignment, kind=TriggerKind.SCHEDULE)[0] is Admission.STARTED


def test_replace_cancels_queued_runs_and_asks_running_ones_to_stop(scene: Scene):
    assignment, _ = scene.assignment(policy="replace", max_running=1)
    _, running = scene.trigger(assignment)
    _, queued = scene.trigger(assignment)
    assert running
    assert queued
    status, newest = scene.trigger(assignment)
    assert status is Admission.QUEUED
    assert scene.state(queued) is State.CANCELLED
    with scene.as_(Role.APP_WORKER) as conn:
        assert get(conn, running).cancel_requested
    assert newest is not None
    scene.move(running, State.CANCELLED)
    assert scene.state(newest) is State.RUNNING


def test_a_run_carrying_a_decided_approval_goes_first(scene: Scene):
    assignment, _ = scene.assignment(max_running=1, max_queued=1)
    _, first = scene.trigger(assignment)
    _, waiting = scene.trigger(assignment)
    status, action = scene.trigger(assignment, kind=TriggerKind.ACTION)
    assert status is Admission.QUEUED  # a full queue doesn't refuse it
    assert first
    assert waiting
    assert action
    scene.move(first, State.SUCCEEDED)
    assert scene.state(action) is State.RUNNING
    assert scene.state(waiting) is State.QUEUED


def test_without_an_accepted_version_runs_wait_and_start_once_one_is_accepted(scene: Scene):
    assignment, version = scene.assignment(accepted=False)
    status, run = scene.trigger(assignment)
    assert status is Admission.AWAITING_ACCEPTANCE
    assert run is not None
    assert scene.state(run) is State.PAUSED_REACCEPTANCE
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        accept(conn, version)
    with scene.as_(Role.APP_WORKER) as conn:
        assert requeue_after_acceptance(conn, assignment) == 1
    assert scene.state(run) is State.RUNNING


def test_an_event_older_than_its_dedup_window_is_refused(scene: Scene):
    assignment, _ = scene.assignment()
    old = datetime.now(UTC) - timedelta(days=31)
    assert scene.trigger(assignment, event_at=old, keep=timedelta(days=30)) == (
        Admission.TOO_OLD,
        None,
    )


def test_nothing_starts_while_the_assignment_is_paused(scene: Scene):
    assignment, _ = scene.assignment()
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        pause(conn, "assignment", assignment)
    status, run = scene.trigger(assignment)
    assert status is Admission.QUEUED
    assert run is not None
    assert scene.state(run) is State.QUEUED


def test_admission_never_exceeds_the_cap_under_concurrency(scene: Scene):
    assignment, _ = scene.assignment(max_running=3, max_queued=4)
    engine = make_engine(scene.database.urls[Role.APP_WORKER], pool_size=12, max_overflow=0)
    start = threading.Barrier(12)
    ctx = TenantContext(scene.tenant.id)

    def fire(n: int) -> Admission:
        start.wait()
        with tenant_connection(engine, ctx) as conn:
            return admit(
                conn, assignment_id=assignment, kind=TriggerKind.WEBHOOK, dedup_key=f"race-{n}"
            ).status

    try:
        with ThreadPoolExecutor(12) as pool:
            outcomes = list(pool.map(fire, range(12)))
    finally:
        engine.dispose()
    with scene.database.admin_engine().connect() as conn:
        counts = dict(
            conn.execute(
                text("SELECT state, count(*) FROM run WHERE assignment_id = :a GROUP BY state"),
                {"a": assignment},
            ).all()
        )
    assert counts == {"running": 3, "queued": 4}
    assert outcomes.count(Admission.QUEUE_FULL) == 5


# ---- Transitions and honest outcomes ----------------------------------------------------------


def test_a_stale_version_loses(scene: Scene):
    assignment, _ = scene.assignment()
    _, run = scene.trigger(assignment)
    assert run is not None
    with scene.as_(Role.APP_WORKER) as conn:
        version = get(conn, run).version
    scene.move(run, State.WAITING_PERSON)
    with pytest.raises(DBAPIError, match="is at version"), scene.as_(Role.APP_WORKER) as conn:
        transition(conn, run, expected_version=version, to=State.SUCCEEDED, reason="late")


@pytest.mark.parametrize(
    ("to", "effects", "message"),
    [
        (State.CANCELLED, 1, "applied side effects"),
        (State.FAILED, 2, "applied side effects"),
        (State.COMPLETED_PARTIAL, 0, "applied no side effects"),
        (State.ROLLED_BACK, 0, "applied no side effects"),
        (State.QUEUED, 0, "can't go from running to queued"),
    ],
)
def test_outcomes_say_what_happened(scene: Scene, to: State, effects: int, message: str):
    assignment, _ = scene.assignment()
    _, run = scene.trigger(assignment)
    assert run is not None
    scene.effects(run, effects)
    with pytest.raises(DBAPIError, match=message):
        scene.move(run, to)


def test_a_run_that_started_resumes_rather_than_requeues(scene: Scene):
    assignment, _ = scene.assignment()
    _, run = scene.trigger(assignment)
    assert run is not None
    scene.move(run, State.PAUSED_BUDGET)
    with pytest.raises(DBAPIError, match="resumes; it doesn't requeue"):
        scene.move(run, State.QUEUED)


def test_waits_end_segments_and_resumes_start_new_ones(scene: Scene):
    assignment, _ = scene.assignment(max_running=1)
    _, run = scene.trigger(assignment)
    _, next_run = scene.trigger(assignment)
    assert run
    assert next_run
    scene.move(run, State.RUNNING, continuation={"step": "lookup"})  # a checkpoint
    with scene.as_(Role.APP_WORKER) as conn:
        assert (get(conn, run).segment, get(conn, run).continuation) == (1, {"step": "lookup"})
    scene.move(run, State.WAITING_PERSON)
    assert scene.state(next_run) is State.RUNNING  # a waiting run holds no slot (RUN-01)
    scene.move(run, State.RUNNING)  # and resumes without one
    with scene.as_(Role.APP_WORKER) as conn:
        assert get(conn, run).segment == 2
    segments = [p["segment"] for k, p in _events(scene, run) if k == "run.segment_ready"]
    assert segments == [1, 2]


# ---- The run kill switch (ASG-13) -------------------------------------------------------------


def _grant(scene: Scene, assignment: UUID, version: UUID, run: UUID) -> UUID:
    with scene.as_(Role.APP_WORKER) as conn:
        issued = issue_step_grant(
            conn, assignment_id=assignment, version_id=version, run_id=run, step_key="read"
        )
    assert issued.status is IssueStatus.ISSUED
    return issued.grant_id


def _check(scene: Scene, grant: UUID) -> CheckStatus:
    with scene.as_(Role.GW) as conn:
        return check_step_grant(conn, grant).status


def test_pausing_a_run_stops_its_grants_for_good(scene: Scene):
    assignment, version = scene.assignment()
    _, run = scene.trigger(assignment)
    assert run is not None
    before = _grant(scene, assignment, version, run)
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        assert pause_run(conn, run) is State.PAUSED_BY_USER
    assert _check(scene, before) is CheckStatus.RUN_PAUSED
    with scene.as_(Role.APP_WORKER) as conn:
        refused = issue_step_grant(
            conn, assignment_id=assignment, version_id=version, run_id=run, step_key="read"
        )
    assert refused.status is IssueStatus.RUN_NOT_RUNNING
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        assert resume_run(conn, run) is State.RUNNING
    assert _check(scene, before) is CheckStatus.RUN_PAUSED
    assert _check(scene, _grant(scene, assignment, version, run)) is CheckStatus.VALID


def test_a_grant_ends_when_its_run_leaves_running(scene: Scene):
    assignment, version = scene.assignment()
    _, run = scene.trigger(assignment)
    assert run is not None
    grant = _grant(scene, assignment, version, run)
    scene.move(run, State.WAITING_PERSON)
    assert _check(scene, grant) is CheckStatus.RUN_NOT_RUNNING


def test_only_the_owner_or_an_admin_works_a_run_and_an_admins_pause_is_theirs(scene: Scene):
    assignment, _ = scene.assignment()
    _, run = scene.trigger(assignment)
    assert run is not None
    with (
        pytest.raises(DBAPIError, match="not allowed"),
        scene.as_(Role.APP_USER, scene.other) as conn,
    ):
        pause_run(conn, run)
    with scene.as_(Role.APP_USER, scene.admin) as conn:
        pause_run(conn, run)
    with (
        pytest.raises(DBAPIError, match="not allowed"),
        scene.as_(Role.APP_USER, scene.owner) as conn,
    ):
        resume_run(conn, run)
    with scene.as_(Role.APP_USER, scene.admin) as conn:
        assert resume_run(conn, run) is State.RUNNING


def test_a_run_paused_before_its_first_step_goes_back_to_the_queue(scene: Scene):
    assignment, _ = scene.assignment(max_running=1)
    _, first = scene.trigger(assignment)
    _, queued = scene.trigger(assignment)
    assert first
    assert queued
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        pause_run(conn, queued)
        assert resume_run(conn, queued) is State.QUEUED


def test_cancel_ends_a_run_with_no_effects_and_rolls_back_one_with_effects(scene: Scene):
    assignment, _ = scene.assignment()
    _, clean = scene.trigger(assignment)
    _, touched = scene.trigger(assignment)
    assert clean
    assert touched
    scene.effects(touched, 1)
    scene.move(touched, State.WAITING_PERSON)
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        assert cancel_run(conn, clean) is State.CANCELLED
        assert cancel_run(conn, touched) is State.RUNNING  # compensations next (RUN-06)
    with scene.as_(Role.APP_WORKER) as conn:
        assert get(conn, touched).cancel_requested
    with scene.database.admin_engine().connect() as conn:
        audited = conn.execute(
            text(
                "SELECT count(*) FROM audit.record WHERE kind = 'kill_switch' "
                "AND detail ->> 'scope' = 'run' AND tenant_id = :t"
            ),
            {"t": scene.tenant.id},
        ).scalar_one()
    assert audited == 2


# ---- Outbox relay ------------------------------------------------------------------------------


def test_events_are_numbered_in_commit_order_so_a_late_commit_is_never_skipped(scene: Scene):
    # Two assignments, so the two transitions don't wait on the same assignment lock.
    first_assignment, _ = scene.assignment()
    second_assignment, _ = scene.assignment()
    _, slow = scene.trigger(first_assignment)
    _, fast = scene.trigger(second_assignment)
    assert slow
    assert fast
    with scene.as_(Role.APP_WORKER) as conn:
        claimed = claim(conn, scene.tenant.id)
    first_batch = [e.seq for e in claimed]
    assert first_batch == list(range(1, len(first_batch) + 1))

    worker = scene.database.engine(Role.APP_WORKER)
    ctx = TenantContext(scene.tenant.id)
    with tenant_connection(worker, ctx) as slow_conn:  # opened first, commits last
        version = get(slow_conn, slow).version
        transition(slow_conn, slow, expected_version=version, to=State.WAITING_PERSON, reason="t")
        scene.move(fast, State.WAITING_PERSON)
        with scene.as_(Role.APP_WORKER) as conn:
            middle = claim(conn, scene.tenant.id)
    with scene.as_(Role.APP_WORKER) as conn:
        last = claim(conn, scene.tenant.id)
    fast_seq = max(e.seq for e in middle if e.subject_id == fast)
    slow_seq = max(e.seq for e in last if e.subject_id == slow)
    assert slow_seq > fast_seq  # created earlier, published later
    seqs = sorted({e.seq for e in [*claimed, *middle, *last]})
    assert seqs == list(range(1, len(seqs) + 1))


def test_the_relay_delivers_at_least_once(scene: Scene):
    assignment, _ = scene.assignment()
    for _ in range(3):
        scene.trigger(assignment)
    worker = scene.database.engine(Role.APP_WORKER)
    seen: list[OutboxEvent] = []

    def flaky(event: OutboxEvent) -> None:
        if (
            event.tenant_id == scene.tenant.id
            and len([e for e in seen if e.tenant_id == scene.tenant.id]) == 4
        ):
            seen.append(event)
            raise RuntimeError("the workflow service is down")
        seen.append(event)

    with pytest.raises(RuntimeError):
        relay_once(worker, flaky)
    ours = [e for e in seen if e.tenant_id == scene.tenant.id]
    assert len(ours) == 5
    retried = ours[-1]

    again: list[OutboxEvent] = []
    relay_once(worker, again.append)
    redelivered = [e for e in again if e.tenant_id == scene.tenant.id]
    assert redelivered[0].id == retried.id  # the failed event comes again, first
    with scene.as_(Role.APP_WORKER) as conn:
        assert claim(conn, scene.tenant.id) == []
