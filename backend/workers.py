"""
Milestone 2 Bounded Async Worker Pool.
Dispatches rollouts from an asyncio.Queue using MAX_CONCURRENT_ROLLOUTS workers.
Each rollout runs in its own exception boundary — one failure never kills other workers.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

from backend.models import Job, Rollout, RolloutStatus
from backend.store import JobStore
from backend.configs import MAX_CONCURRENT_ROLLOUTS

logger = logging.getLogger(__name__)


async def _worker(queue: asyncio.Queue, store: JobStore, mock: bool) -> None:
    """A single worker that drains rollouts from the queue until exhausted."""
    while True:
        try:
            rollout_id: str = await queue.get()
        except asyncio.CancelledError:
            break

        try:
            rollout = await store.get_rollout(rollout_id)
            if rollout is None:
                logger.warning(f"Worker: rollout {rollout_id} not found in store, skipping")
                continue

            # Check if job was cancelled before we even start
            job = await store.get_job(rollout.job_id)
            if job is not None and str(job.status) == "CANCELLED":
                logger.info(f"Rollout {rollout_id} skipped — job cancelled")
                continue

            if mock:
                from backend.executor_mock import execute_mock_rollout
                await execute_mock_rollout(rollout_id, store)
            else:
                from backend.executor_real import execute_rollout
                # execute_rollout is synchronous (runs blocking agent); run in thread pool
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, execute_rollout, rollout_id, store)

        except Exception as exc:
            # Exception boundary: mark the rollout as ERROR, never propagate
            logger.error(f"Worker: unhandled exception for rollout {rollout_id}: {exc}", exc_info=True)
            try:
                from datetime import datetime
                from backend.models import ErrorType
                await store.update_rollout(
                    rollout_id,
                    status=RolloutStatus.ERROR,
                    error_type=ErrorType.AGENT_ERROR,
                    error_message=str(exc),
                    error_stage="worker",
                    completed_at=datetime.utcnow().isoformat(),
                    termination_reason="worker_exception",
                )
            except Exception as store_exc:
                logger.error(f"Worker: could not update rollout {rollout_id} after exception: {store_exc}")
        finally:
            queue.task_done()


async def run_job(job: Job, rollouts: List[Rollout], store: JobStore, mock: bool = True) -> None:
    """
    Populate the queue with rollout IDs and spin up MAX_CONCURRENT_ROLLOUTS workers.
    Returns after all rollouts have been processed.
    Runs as an asyncio background task.
    """
    queue: asyncio.Queue = asyncio.Queue()

    for rollout in rollouts:
        await queue.put(rollout.id)

    workers = [
        asyncio.create_task(_worker(queue, store, mock))
        for _ in range(min(MAX_CONCURRENT_ROLLOUTS, len(rollouts)))
    ]

    # Wait for queue to drain
    await queue.join()

    # Cancel idle workers
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    logger.info(f"Job {job.id} finished — all rollouts processed")
