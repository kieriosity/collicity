"""Run segments: the Temporal workflow and the activities it calls (technical design §4.1).

A segment is a short, deterministic loop: ``next_step`` decides what happens next from the
run's recorded state, and ``execute_step`` runs one step. Postgres owns the run; Temporal holds
nothing it can't lose. Payloads carry identifiers and numbers only.

Each step's database work commits in the same transaction as the checkpoint that moves the run
past it, so a step runs once however often Temporal retries the activity. A segment ends when
the run waits, pauses or finishes; the next one starts from the outbox (workflow ID
``run:{id}:{n}``).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from collicity_core import TenantContext
    from collicity_db import tenant_connection
    from collicity_runs import State, transition
    from collicity_runs.graph import Step, TaskGraph

TASK_QUEUE = "segments"
WORKFLOW = "RunSegment"
MAX_STEPS_PER_SEGMENT = 200


def workflow_id(run_id: UUID | str, segment: int) -> str:
    return f"run:{run_id}:{segment}"


# ---- Payloads: identifiers and numbers only ---------------------------------------------------


@dataclass(frozen=True)
class SegmentInput:
    tenant_id: str
    run_id: str
    segment: int
    step_timeout_seconds: int = 600


@dataclass(frozen=True)
class StepInput:
    tenant_id: str
    run_id: str
    segment: int
    index: int


@dataclass(frozen=True)
class Decision:
    action: Literal["execute", "stop"]
    index: int = -1
    reason: str = ""


# ---- Step handlers ------------------------------------------------------------------------------


@dataclass(frozen=True)
class StepContext:
    tenant_id: UUID
    run_id: UUID
    assignment_id: UUID
    version_id: UUID
    owner_id: UUID
    index: int
    step: Step


@dataclass(frozen=True)
class Continue:
    pass


@dataclass(frozen=True)
class Wait:
    state: Literal[State.WAITING_PERSON, State.WAITING_TIMER]
    reason: str
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Fail:
    code: str


StepResult = Continue | Wait | Fail
# A handler runs inside the step's transaction, so its database work commits with the checkpoint.
Handler = Callable[[Connection, StepContext], StepResult]


def _wait_person(_: Connection, ctx: StepContext) -> StepResult:
    return Wait(State.WAITING_PERSON, ctx.step.params.get("reason", "input"))


def _wait_timer(_: Connection, ctx: StepContext) -> StepResult:
    seconds = int(ctx.step.params.get("seconds", 0))
    return Wait(State.WAITING_TIMER, "timer", {"wait_seconds": seconds})


BUILT_IN: dict[str, Handler] = {
    "wait_person": _wait_person,  # a generic human step (ASG-10)
    "wait_timer": _wait_timer,
}


# ---- Activities -----------------------------------------------------------------------------


class SegmentActivities:
    """The activities, bound to the worker's database engine (role ``app_worker``)."""

    def __init__(self, engine: Engine, handlers: Mapping[str, Handler] | None = None) -> None:
        self._engine = engine
        self._handlers = {**BUILT_IN, **(handlers or {})}
        # Tests use this to fail between a step's commit and the activity's return.
        self.after_commit: Callable[[StepInput], None] = lambda _: None

    def _tenant(self, tenant_id: str) -> Any:
        return tenant_connection(self._engine, TenantContext(UUID(tenant_id)))

    @activity.defn(name="next_step")
    def next_step(self, segment: SegmentInput) -> Decision:
        run_id = UUID(segment.run_id)
        with self._tenant(segment.tenant_id) as conn:
            row = _run_row(conn, run_id)
            if row is None:
                return Decision("stop", reason="missing")
            if row.segment_no != segment.segment or row.state != State.RUNNING:
                return Decision("stop", reason="stale")
            version = row.state_version

            if row.cancel_requested:
                # With no effects the run simply ends. With effects, compensations need their
                # own approvals in the beta (RUN-06); until approvals exist, the run ends with
                # its effects honestly reported (collicity-xrd.7.5).
                to = State.CANCELLED if row.effects_applied == 0 else State.COMPLETED_PARTIAL
                transition(conn, run_id, expected_version=version, to=to, reason="cancelled")
                return Decision("stop", reason="cancelled")
            if row.scope_paused:
                transition(
                    conn,
                    run_id,
                    expected_version=version,
                    to=State.PAUSED_BY_USER,
                    reason="scope_paused",
                )
                return Decision("stop", reason="paused")

            steps = TaskGraph.model_validate(row.task_graph).steps
            index = int(row.continuation.get("next", 0))
            if index >= len(steps):
                transition(
                    conn, run_id, expected_version=version, to=State.SUCCEEDED, reason="completed"
                )
                return Decision("stop", reason="completed")
            return Decision("execute", index=index)

    @activity.defn(name="execute_step")
    def execute_step(self, step: StepInput) -> Decision:
        run_id = UUID(step.run_id)
        with self._tenant(step.tenant_id) as conn:
            row = _run_row(conn, run_id)
            if row is None or row.segment_no != step.segment or row.state != State.RUNNING:
                return Decision("stop", reason="stale")
            done = int(row.continuation.get("next", 0))
            if done > step.index:
                return Decision("execute", reason="already_done")  # a retry after commit
            if done < step.index:
                return Decision("stop", reason="stale")

            graph = TaskGraph.model_validate(row.task_graph)
            current = graph.steps[step.index]
            handler = self._handlers.get(current.kind)
            ctx = StepContext(
                tenant_id=UUID(step.tenant_id),
                run_id=run_id,
                assignment_id=row.assignment_id,
                version_id=row.version_id,
                owner_id=row.owner_id,
                index=step.index,
                step=current,
            )
            result = handler(conn, ctx) if handler else Fail("unknown_step_kind")
            reason = _apply(conn, row, step.index, result)
        self.after_commit(step)
        return Decision("execute" if reason == "continue" else "stop", reason=reason)


def _apply(conn: Connection, row: Any, index: int, result: StepResult) -> str:
    """Record the step's outcome as the run's next state, in the step's transaction."""
    after = {**row.continuation, "next": index + 1}
    match result:
        case Continue():
            transition(
                conn,
                row.id,
                expected_version=row.state_version,
                to=State.RUNNING,
                reason="step_done",
                continuation=after,
            )
            return "continue"
        case Wait(state=state, reason=reason, details=details):
            transition(
                conn,
                row.id,
                expected_version=row.state_version,
                to=state,
                reason=reason,
                continuation={**after, **(details or {})},
            )
            return "waiting"
        case Fail(code=code):
            to = State.FAILED if row.effects_applied == 0 else State.COMPLETED_PARTIAL
            transition(
                conn,
                row.id,
                expected_version=row.state_version,
                to=to,
                reason=code,
                continuation={**row.continuation, "failed_step": index, "error": code},
            )
            return "failed"


def _run_row(conn: Connection, run_id: UUID) -> Any:
    return conn.execute(
        text(
            "SELECT r.id, r.state, r.state_version, r.segment_no, r.continuation, "
            "       r.effects_applied, r.assignment_id, r.version_id, r.owner_id, "
            "       r.cancel_requested_at IS NOT NULL AS cancel_requested, "
            "       v.task_graph, "
            "       (t.runs_paused_at IS NOT NULL OR p.runs_paused_at IS NOT NULL "
            "        OR a.runs_paused_at IS NOT NULL) AS scope_paused "
            "FROM run r "
            "JOIN assignment_version v ON v.id = r.version_id "
            "JOIN assignment a ON a.id = r.assignment_id "
            "JOIN principal p ON p.id = r.owner_id "
            "JOIN tenant t ON t.id = r.tenant_id "
            "WHERE r.id = :id"
        ),
        {"id": run_id},
    ).one_or_none()


# ---- Workflow --------------------------------------------------------------------------------

# Retries cover a lost worker or a database hiccup. A step that keeps failing ends its segment's
# workflow within about 15 seconds; the run stays Running for the sweeper to find.
_DB_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=5,
)


@workflow.defn(name=WORKFLOW)
class RunSegment:
    """One segment of a run: decide, execute, repeat, until the run waits, pauses or ends."""

    @workflow.run
    async def run(self, segment: SegmentInput) -> str:
        for _ in range(MAX_STEPS_PER_SEGMENT):
            decision = await workflow.execute_activity(
                "next_step",
                segment,
                result_type=Decision,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_DB_RETRY,
            )
            if decision.action != "execute":
                return decision.reason
            outcome = await workflow.execute_activity(
                "execute_step",
                StepInput(segment.tenant_id, segment.run_id, segment.segment, decision.index),
                result_type=Decision,
                start_to_close_timeout=timedelta(seconds=segment.step_timeout_seconds),
                retry_policy=_DB_RETRY,
            )
            if outcome.action != "execute":
                return outcome.reason
        # Keep each history short; the run's state is in Postgres, so nothing is carried over.
        workflow.continue_as_new(segment)
