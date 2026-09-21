"""
Bounded Async Worker Pool.
Dispatches rollouts from an asyncio.Queue using MAX_CONCURRENT_ROLLOUTS workers.
Each rollout runs in its own exception boundary — one failure never kills other workers.

Worker slot (0-based index) is passed to the real executor so each rollout
gets a unique Metabase port (ROLLOUT_PORT_BASE + slot) — no port conflicts.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List

from backend.models import Job, JobStatus, Rollout, RolloutStatus
from backend.db import SQLiteStore
from backend.configs import MAX_CONCURRENT_ROLLOUTS

logger = logging.getLogger(__name__)

class JobManager:
    _instance = None
    
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.queue = asyncio.Queue()
        self.executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ROLLOUTS, thread_name_prefix="rollout-worker")
        self.workers = []

    @classmethod
    def get_instance(cls, store: SQLiteStore = None) -> 'JobManager':
        if cls._instance is None:
            if store is None:
                raise ValueError("Store must be provided for initial initialization")
            cls._instance = cls(store)
        return cls._instance

    def start_workers(self):
        for i in range(MAX_CONCURRENT_ROLLOUTS):
            w = asyncio.create_task(self._worker(i))
            self.workers.append(w)
        logger.info(f"Started {MAX_CONCURRENT_ROLLOUTS} global rollout workers.")

    async def shutdown(self):
        for w in self.workers:
            w.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.executor.shutdown(wait=True)
        logger.info("Global JobManager shut down completely.")

    async def _worker(self, slot: int):
        while True:
            try:
                rollout_id: str = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                rollout = self.store.get_rollout(rollout_id)
                if rollout is None:
                    continue

                job = self.store.get_job(rollout.job_id)
                if job is not None and (job.status == JobStatus.CANCELLED or job.status == "CANCELLED"):
                    logger.info(f"Worker[{slot}]: rollout {rollout_id} skipped — job cancelled")
                    continue

                # execute_rollout is blocking; run in thread pool.
                from backend.rollout.runner import execute_rollout
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(self.executor, execute_rollout, rollout_id, self.store, slot)

            except Exception as exc:
                logger.error(f"Worker[{slot}]: unhandled exception for rollout {rollout_id}: {exc}", exc_info=True)
                try:
                    from datetime import datetime
                    from backend.models import ErrorType
                    self.store.update_rollout(
                        rollout_id,
                        status=RolloutStatus.ERROR,
                        error_type=ErrorType.AGENT_ERROR,
                        error_message=str(exc),
                        error_stage="worker",
                        completed_at=datetime.utcnow().isoformat(),
                        termination_reason="worker_exception",
                    )
                except Exception as store_exc:
                    logger.error(f"Worker[{slot}]: could not update rollout after exception: {store_exc}")
            finally:
                self.queue.task_done()


async def run_job(job: Job, rollouts: List[Rollout], store: SQLiteStore) -> None:
    manager = JobManager.get_instance(store)
    for rollout in rollouts:
        await manager.queue.put(rollout.id)
    logger.info(f"Job {job.id} enqueued {len(rollouts)} rollouts to global pool")
