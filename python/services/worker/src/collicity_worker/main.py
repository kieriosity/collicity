"""The worker service: Temporal run segments and the outbox relay.

Run locally with ``python -m collicity_worker.main`` (``just worker``). It connects to Postgres
as ``app_worker`` and to Temporal; in AWS it has no internet route (§3.1).
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio.client import Client
from temporalio.worker import Worker

from collicity_core.redaction import configure_logging
from collicity_db import make_engine
from collicity_worker.relay import Relay
from collicity_worker.segments import TASK_QUEUE, RunSegment, SegmentActivities


class WorkerSettings(BaseSettings):
    """Configuration for ``worker``, from ``COLLICITY_WORKER_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="COLLICITY_WORKER_", frozen=True)

    database_url: SecretStr  # app_worker; never migrator
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    step_timeout_seconds: int = 600
    activity_threads: int = 16
    environment: Literal["local", "dev", "staging", "prod"] = "local"


async def run(settings: WorkerSettings) -> None:
    engine = make_engine(settings.database_url.get_secret_value())
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    activities = SegmentActivities(engine)
    relay = Relay(engine, client, step_timeout_seconds=settings.step_timeout_seconds)
    with ThreadPoolExecutor(max_workers=settings.activity_threads) as pool:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[RunSegment],
            activities=[activities.next_step, activities.execute_step],
            activity_executor=pool,
        )
        await asyncio.gather(worker.run(), relay.run_forever())


def main() -> None:
    configure_logging()
    asyncio.run(run(WorkerSettings()))  # pyright: ignore[reportCallIssue]


if __name__ == "__main__":
    main()
