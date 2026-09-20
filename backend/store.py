"""
Milestone 2 In-Memory Job Store.
Thread/asyncio-safe dictionary-backed store for Jobs and Rollouts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

from backend.models import ErrorType, Job, JobStatus, Rollout, RolloutStatus

logger = logging.getLogger(__name__)


class JobStore:
    """Async-safe in-memory store for Jobs and Rollouts."""

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._rollouts: Dict[str, Rollout] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Job operations
    # ------------------------------------------------------------------

    async def create_job(self, job: Job, rollouts: List[Rollout]) -> None:
        async with self._lock:
            self._jobs[job.id] = job
            for r in rollouts:
                self._rollouts[r.id] = r

    async def get_job(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def list_jobs(self) -> List[Job]:
        async with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    async def cancel_job(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                return False
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.utcnow().isoformat()
            # Mark all non-terminal rollouts as CANCELLED
            for rid in job.rollout_ids:
                r = self._rollouts.get(rid)
                if r and not r.is_terminal:
                    r.status = RolloutStatus.CANCELLED
                    r.termination_reason = "job_cancelled"
                    r.completed_at = datetime.utcnow().isoformat()
            return True

    # ------------------------------------------------------------------
    # Rollout operations
    # ------------------------------------------------------------------

    async def get_rollout(self, rollout_id: str) -> Optional[Rollout]:
        async with self._lock:
            return self._rollouts.get(rollout_id)

    async def get_rollouts_for_job(self, job_id: str) -> List[Rollout]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return []
            return [self._rollouts[rid] for rid in job.rollout_ids if rid in self._rollouts]

    async def update_rollout(self, rollout_id: str, **kwargs) -> None:
        """Update arbitrary fields on a Rollout, then recompute parent Job progress."""
        async with self._lock:
            rollout = self._rollouts.get(rollout_id)
            if not rollout:
                logger.warning(f"update_rollout: rollout {rollout_id} not found")
                return
            for k, v in kwargs.items():
                if hasattr(rollout, k):
                    setattr(rollout, k, v)
                else:
                    logger.warning(f"update_rollout: unknown field '{k}' on Rollout")
            self._recompute_job_progress(rollout.job_id)

    def _recompute_job_progress(self, job_id: str) -> None:
        """Recompute job progress counters (must be called under lock)."""
        job = self._jobs.get(job_id)
        if not job:
            return

        rollouts = [self._rollouts[rid] for rid in job.rollout_ids if rid in self._rollouts]
        passed = sum(1 for r in rollouts if r.status == RolloutStatus.PASSED)
        failed = sum(1 for r in rollouts if r.status == RolloutStatus.FAILED)
        errors = sum(1 for r in rollouts if r.status == RolloutStatus.ERROR)
        timeouts = sum(1 for r in rollouts if r.status == RolloutStatus.TIMEOUT)
        cancelled = sum(1 for r in rollouts if r.status == RolloutStatus.CANCELLED)
        completed = passed + failed + errors + timeouts + cancelled

        job.passed = passed
        job.failed = failed
        job.errors = errors
        job.timeouts = timeouts
        job.completed = completed
        job.total = len(rollouts)

        if completed == job.total and job.total > 0:
            if job.status not in (JobStatus.CANCELLED,):
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.utcnow().isoformat()
        elif job.status == JobStatus.QUEUED:
            job.status = JobStatus.RUNNING
            job.started_at = datetime.utcnow().isoformat()
