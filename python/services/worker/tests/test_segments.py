"""Run segments on a real Temporal dev server and Postgres (technical design §4.1).

Each test runs its own worker on its own task queue. Step kinds that exist only for tests are
registered here: ``marker`` records an audit row inside the step's transaction, which is how a
test counts how many times a step really happened.
"""

import asyncio
import json
import os
import threading
from collections.abc import AsyncGenerator, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import Connection, insert, text
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from collicity_assignments import accept, create_assignment, create_version, pause
from collicity_core import TenantContext, new_id
from collicity_db import Role, tenant_connection
from collicity_db.models import RoleGrant
from collicity_grants.models import CapabilityGrant
from collicity_runs import OutboxEvent, State, TriggerKind, admit, cancel_run, get
from collicity_runs import transition as move
from collicity_testing.postgres import MigratedDatabase, SeededTenant, seed_tenant
from collicity_worker.relay import Relay
from collicity_worker.segments import (
    Continue,
    Fail,
    Handler,
    RunSegment,
    SegmentActivities,
    SegmentInput,
    StepContext,
    StepInput,
    StepResult,
    workflow_id,
)

pytestmark = pytest.mark.asyncio

READ = CapabilityGrant(
    connector="graph",
    manifest_version="2026.10.1",
    action="excel.read_rows",
    resources={"workbook": frozenset({"w1"}), "table": frozenset({"t1"})},
)


def _marker(conn: Connection, ctx: StepContext) -> StepResult:
    event = {
        "id": str(new_id()),
        "kind": "data_access",
        "run_id": str(ctx.run_id),
        "detail": {"marker": ctx.step.key},
    }
    conn.execute(
        text("SELECT audit.append(CAST(:e AS jsonb), NULL, NULL, NULL)"), {"e": json.dumps(event)}
    )
    return Continue()


def _fail(_: Connection, ctx: StepContext) -> StepResult:
    return Fail(ctx.step.params.get("code", "boom"))


@pytest_asyncio.fixture(scope="session")
async def temporal() -> AsyncGenerator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_local() as env:
        yield env


@dataclass
class Scene:
    database: MigratedDatabase
    tenant: SeededTenant
    owner: UUID
    admin: UUID
    env: WorkflowEnvironment
    queue: str

    @contextmanager
    def as_(self, role: Role, person: UUID | None = None) -> Generator[Connection]:
        ctx = TenantContext(self.tenant.id, person)
        with tenant_connection(self.database.engine(role), ctx) as conn:
            yield conn

    def assignment(self, steps: list[dict[str, Any]], *, accepted: bool = True) -> UUID:
        with self.as_(Role.APP_USER, self.owner) as conn:
            assignment = create_assignment(conn, owner_id=self.owner, name="Segments")
            version = create_version(
                conn,
                assignment_id=assignment,
                number=1,
                grants=[READ],
                task_graph={"steps": steps},
                created_by=self.owner,
            )
            if accepted:
                accept(conn, version)
        return assignment

    def trigger(self, assignment: UUID) -> UUID:
        with self.as_(Role.APP_WORKER) as conn:
            admitted = admit(
                conn, assignment_id=assignment, kind=TriggerKind.MANUAL, dedup_key=str(new_id())
            )
        assert admitted.run_id is not None
        return admitted.run_id

    def state(self, run: UUID) -> State:
        with self.as_(Role.APP_WORKER) as conn:
            return get(conn, run).state

    def markers(self, run: UUID) -> list[str]:
        with self.database.admin_engine().connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT detail ->> 'marker' FROM audit.record "
                        "WHERE run_id = :r AND detail ? 'marker' ORDER BY occurred_at"
                    ),
                    {"r": run},
                ).scalars()
            )

    @asynccontextmanager
    async def worker(
        self, handlers: Mapping[str, Handler] | None = None
    ) -> AsyncGenerator[tuple[Relay, SegmentActivities]]:
        engine = self.database.engine(Role.APP_WORKER)
        activities = SegmentActivities(
            engine, {"marker": _marker, "fail": _fail, **(handlers or {})}
        )
        relay = Relay(engine, self.env.client, step_timeout_seconds=30, task_queue=self.queue)
        with ThreadPoolExecutor(max_workers=8) as pool:
            async with Worker(
                self.env.client,
                task_queue=self.queue,
                workflows=[RunSegment],
                activities=[activities.next_step, activities.execute_step],
                activity_executor=pool,
            ):
                yield relay, activities

    async def segment(self, relay: Relay, run: UUID, number: int) -> str:
        """Relay until segment ``number`` of ``run`` has started, then wait for it to end."""
        await relay.pass_once()
        handle = self.env.client.get_workflow_handle(workflow_id(run, number))
        return await handle.result()


@pytest.fixture
def scene(database: MigratedDatabase, temporal: WorkflowEnvironment) -> Scene:
    tenant = seed_tenant(database, principals=2)
    owner, admin = tenant.principal_ids
    with database.engine(Role.MIGRATOR).begin() as conn:
        conn.execute(
            insert(RoleGrant).values(tenant_id=tenant.id, principal_id=admin, role="admin")
        )
    return Scene(database, tenant, owner, admin, temporal, f"segments-{new_id()}")


async def test_a_run_executes_its_steps_in_one_segment_and_succeeds(scene: Scene):
    assignment = scene.assignment(
        [{"key": "lookup", "kind": "marker"}, {"key": "draft", "kind": "marker"}]
    )
    async with scene.worker() as (relay, _):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "completed"
    assert scene.state(run) is State.SUCCEEDED
    assert scene.markers(run) == ["lookup", "draft"]


async def test_a_wait_ends_the_segment_and_the_next_segment_carries_on(scene: Scene):
    assignment = scene.assignment(
        [
            {"key": "lookup", "kind": "marker"},
            {"key": "review", "kind": "wait_person", "params": {"reason": "review"}},
            {"key": "draft", "kind": "marker"},
        ]
    )
    async with scene.worker() as (relay, _):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "waiting"
        assert scene.state(run) is State.WAITING_PERSON
        assert scene.markers(run) == ["lookup"]
        with scene.as_(Role.APP_WORKER) as conn:  # the person responds
            move(
                conn,
                run,
                expected_version=get(conn, run).version,
                to=State.RUNNING,
                reason="reviewed",
            )
        assert await scene.segment(relay, run, 2) == "completed"
    assert scene.state(run) is State.SUCCEEDED
    assert scene.markers(run) == ["lookup", "draft"]


async def test_a_failing_step_fails_the_run_and_says_why(scene: Scene):
    assignment = scene.assignment(
        [
            {"key": "lookup", "kind": "marker"},
            {"key": "check", "kind": "fail", "params": {"code": "no_match"}},
        ]
    )
    async with scene.worker() as (relay, _):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "failed"
    with scene.as_(Role.APP_WORKER) as conn:
        state = get(conn, run)
    assert state.state is State.FAILED
    assert state.continuation["error"] == "no_match"


async def test_an_unknown_step_kind_fails_the_run_cleanly(scene: Scene):
    assignment = scene.assignment([{"key": "mystery", "kind": "teleport"}])
    async with scene.worker() as (relay, _):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "failed"
    with scene.as_(Role.APP_WORKER) as conn:
        assert get(conn, run).continuation["error"] == "unknown_step_kind"


async def test_a_retry_before_the_step_commits_runs_it_once(scene: Scene):
    attempts: list[int] = []

    def flaky(conn: Connection, ctx: StepContext) -> StepResult:
        _marker(conn, ctx)  # rolled back with the failed attempt
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the worker died mid-step")
        return Continue()

    assignment = scene.assignment([{"key": "write", "kind": "flaky"}])
    async with scene.worker({"flaky": flaky}) as (relay, _):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "completed"
    assert len(attempts) == 2
    assert scene.markers(run) == ["write"]


async def test_a_retry_after_the_step_committed_does_not_repeat_it(scene: Scene):
    assignment = scene.assignment(
        [{"key": "write", "kind": "marker"}, {"key": "after", "kind": "marker"}]
    )
    lost: list[StepInput] = []

    def lose_the_reply(step: StepInput) -> None:
        if step.index == 0 and not lost:
            lost.append(step)
            raise RuntimeError("the reply was lost after the commit")

    async with scene.worker() as (relay, activities):
        activities.after_commit = lose_the_reply
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "completed"
    assert lost
    assert scene.markers(run) == ["write", "after"]


async def test_starting_a_segment_twice_does_nothing(scene: Scene):
    assignment = scene.assignment([{"key": "write", "kind": "marker"}])
    async with scene.worker() as (relay, _):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "completed"
        again = OutboxEvent(new_id(), scene.tenant.id, "run.segment_ready", run, {"segment": 1}, 0)
        await relay.handle(again)  # the relay delivered it twice
        await relay.handle(again)
        await asyncio.sleep(0.5)
    assert scene.markers(run) == ["write"]


@contextmanager
def _gate() -> Generator[tuple[threading.Event, threading.Event, Handler]]:
    entered, release = threading.Event(), threading.Event()

    def held(conn: Connection, ctx: StepContext) -> StepResult:
        entered.set()
        release.wait(timeout=30)
        return _marker(conn, ctx)

    try:
        yield entered, release, held
    finally:
        release.set()


async def test_a_paused_assignment_stops_its_run_at_the_next_step(scene: Scene):
    assignment = scene.assignment(
        [{"key": "slow", "kind": "held"}, {"key": "next", "kind": "marker"}]
    )
    with _gate() as (entered, release, held):
        async with scene.worker({"held": held}) as (relay, _):
            run = scene.trigger(assignment)
            await relay.pass_once()
            await asyncio.to_thread(entered.wait, 30)
            with scene.as_(Role.APP_USER, scene.owner) as conn:
                pause(conn, "assignment", assignment)
            release.set()
            handle = scene.env.client.get_workflow_handle(workflow_id(run, 1))
            assert await handle.result() == "paused"
    assert scene.state(run) is State.PAUSED_BY_USER
    assert scene.markers(run) == ["slow"]  # the step under way finished; nothing after it


async def test_a_run_cancelled_mid_step_stops_without_its_step(scene: Scene):
    assignment = scene.assignment([{"key": "slow", "kind": "held"}])
    with _gate() as (entered, release, held):
        async with scene.worker({"held": held}) as (relay, _):
            run = scene.trigger(assignment)
            await relay.pass_once()
            await asyncio.to_thread(entered.wait, 30)
            with scene.as_(Role.APP_USER, scene.owner) as conn:
                assert cancel_run(conn, run) is State.CANCELLED
            release.set()
            handle = scene.env.client.get_workflow_handle(workflow_id(run, 1))
            assert await handle.result() == "stale"
    assert scene.state(run) is State.CANCELLED
    assert scene.markers(run) == []  # its checkpoint lost, so its work rolled back


async def test_accepting_a_version_starts_the_runs_that_waited_for_it(scene: Scene):
    assignment = scene.assignment([{"key": "only", "kind": "marker"}], accepted=False)
    run = scene.trigger(assignment)
    assert scene.state(run) is State.PAUSED_REACCEPTANCE
    with scene.as_(Role.APP_USER, scene.owner) as conn:
        version = conn.execute(
            text("SELECT id FROM assignment_version WHERE assignment_id = :a"), {"a": assignment}
        ).scalar_one()
        accept(conn, version)
    async with scene.worker() as (relay, _):
        await relay.pass_once()  # version.accepted: requeue, which starts the run
        assert await scene.segment(relay, run, 1) == "completed"
    assert scene.state(run) is State.SUCCEEDED


async def test_a_segment_that_fails_for_good_fails_its_workflow(scene: Scene):
    def broken(_: Connection, __: StepContext) -> StepResult:
        raise RuntimeError("always")

    assignment = scene.assignment([{"key": "x", "kind": "broken"}])
    async with scene.worker({"broken": broken}) as (relay, _):
        run = scene.trigger(assignment)
        await relay.pass_once()
        handle = scene.env.client.get_workflow_handle(workflow_id(run, 1))
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert scene.state(run) is State.RUNNING  # stuck: the sweeper's job (collicity-xrd.7.3)


# ---- Replay: workflow changes must not break segments already in flight ------------------------

HISTORIES = Path(__file__).parent / "histories"
RECORD = os.environ.get("COLLICITY_RECORD_HISTORIES") == "1"


@pytest.mark.skipif(not RECORD, reason="set COLLICITY_RECORD_HISTORIES=1 to re-record")
async def test_record_histories(scene: Scene):
    """Record one history per segment shape. Re-record only for a deliberate workflow change."""
    assignment = scene.assignment(
        [
            {"key": "lookup", "kind": "marker"},
            {"key": "review", "kind": "wait_person"},
            {"key": "draft", "kind": "flaky"},
            {"key": "check", "kind": "fail"},
        ]
    )
    attempts: list[int] = []

    def flaky(conn: Connection, ctx: StepContext) -> StepResult:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("retried once")
        return _marker(conn, ctx)

    async with scene.worker({"flaky": flaky}) as (relay, _):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "waiting"
        with scene.as_(Role.APP_WORKER) as conn:
            move(conn, run, expected_version=get(conn, run).version, to=State.RUNNING, reason="r")
        assert await scene.segment(relay, run, 2) == "failed"
    for number, name in ((1, "waits"), (2, "retries_then_fails")):
        handle = scene.env.client.get_workflow_handle(workflow_id(run, number))
        history = await handle.fetch_history()
        (HISTORIES / f"segment_{name}.json").write_text(
            history.to_json(), encoding="utf-8", newline="
"
        )


async def test_recorded_histories_still_replay():
    files = sorted(HISTORIES.glob("*.json"))
    assert files, "no recorded histories; run with COLLICITY_RECORD_HISTORIES=1"
    replayer = Replayer(workflows=[RunSegment])
    for path in files:
        history = WorkflowHistory.from_json(path.stem, path.read_text(encoding="utf-8"))
        await replayer.replay_workflow(history)


async def test_a_stale_segment_leaves_a_run_that_moved_on_alone(scene: Scene):
    """A segment that outlived its run's move (here: the run now waits for a person) must not act
    for it, even when there is something to act on (a paused assignment)."""
    assignment = scene.assignment(
        [{"key": "a", "kind": "marker"}, {"key": "review", "kind": "wait_person"}]
    )
    async with scene.worker() as (relay, activities):
        run = scene.trigger(assignment)
        assert await scene.segment(relay, run, 1) == "waiting"
        with scene.as_(Role.APP_USER, scene.owner) as conn:
            pause(conn, "assignment", assignment)
        decision = await asyncio.to_thread(
            activities.next_step, SegmentInput(str(scene.tenant.id), str(run), 1)
        )
    assert decision.reason == "stale"
    assert scene.state(run) is State.WAITING_PERSON
