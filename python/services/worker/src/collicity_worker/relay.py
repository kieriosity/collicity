"""The outbox relay: turns committed events into work (technical design §4.1, §9).

Delivery is at least once, so every handler is idempotent. Starting a segment uses the workflow
ID ``run:{id}:{segment}`` with duplicate IDs rejected, so a repeat start does nothing.
"""

import asyncio
import contextlib
import logging
from uuid import UUID

from sqlalchemy import Engine, text
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from collicity_core import TenantContext
from collicity_db import tenant_connection
from collicity_runs import OutboxEvent, claim, mark_delivered, requeue_after_acceptance
from collicity_worker.segments import TASK_QUEUE, WORKFLOW, SegmentInput, workflow_id

log = logging.getLogger(__name__)


class Relay:
    def __init__(
        self,
        engine: Engine,
        client: Client,
        *,
        step_timeout_seconds: int = 600,
        task_queue: str = TASK_QUEUE,
    ) -> None:
        self._engine = engine
        self._client = client
        self._step_timeout = step_timeout_seconds
        self._task_queue = task_queue

    async def handle(self, event: OutboxEvent) -> None:
        match event.kind:
            case "run.segment_ready":
                await self._start_segment(event)
            case "version.accepted":
                await asyncio.to_thread(self._requeue, event.tenant_id, event.subject_id)
            case _:
                pass  # state changes feed live updates once the web client lands (P0.8)

    async def _start_segment(self, event: OutboxEvent) -> None:
        segment = int(event.payload["segment"])
        # Already started, or finished: delivery is at least once, so a repeat does nothing.
        with contextlib.suppress(WorkflowAlreadyStartedError):
            await self._client.start_workflow(
                WORKFLOW,
                SegmentInput(
                    str(event.tenant_id), str(event.subject_id), segment, self._step_timeout
                ),
                id=workflow_id(event.subject_id, segment),
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )

    def _requeue(self, tenant_id: UUID, assignment_id: UUID) -> None:
        with tenant_connection(self._engine, TenantContext(tenant_id)) as conn:
            requeue_after_acceptance(conn, assignment_id)

    async def pass_once(self, limit: int = 100) -> int:
        """Deliver every tenant's published events once; returns how many were delivered."""
        tenants = await asyncio.to_thread(self._tenants)
        delivered = 0
        for tenant_id in tenants:
            events = await asyncio.to_thread(self._claim, tenant_id, limit)
            done: list[UUID] = []
            try:
                for event in events:
                    await self.handle(event)
                    done.append(event.id)
            finally:
                delivered += await asyncio.to_thread(self._mark, tenant_id, done)
        return delivered

    async def run_forever(self, interval: float = 0.5) -> None:
        while True:
            try:
                await self.pass_once()
            except Exception:  # keep relaying; the event is retried next pass
                log.exception("relay pass failed")
            await asyncio.sleep(interval)

    def _tenants(self) -> list[UUID]:
        with self._engine.connect() as conn:
            return list(
                conn.execute(text("SELECT * FROM sweep.tenants_with_outbox(1000)")).scalars()
            )

    def _claim(self, tenant_id: UUID, limit: int) -> list[OutboxEvent]:
        with tenant_connection(self._engine, TenantContext(tenant_id)) as conn:
            return claim(conn, tenant_id, limit)

    def _mark(self, tenant_id: UUID, ids: list[UUID]) -> int:
        with tenant_connection(self._engine, TenantContext(tenant_id)) as conn:
            return mark_delivered(conn, ids)
