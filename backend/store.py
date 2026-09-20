"""
Milestone 2 In-Memory Job Store.
Thread/asyncio-safe dictionary-backed store for Jobs and Rollouts.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from backend.configs import STATE_FILE
from backend.models import ErrorType, Job, JobStatus, Rollout, RolloutStatus

logger = logging.getLogger(__name__)


class JobStore:
    """Async-safe in-memory store for Jobs and Rollouts with disk persistence support."""

    def __init__(self, state_file: Optional[Union[Path, str]] = None):
        self._jobs: Dict[str, Job] = {}
        self._rollouts: Dict[str, Rollout] = {}
        self._lock = asyncio.Lock()
        self.state_file = Path(state_file) if state_file else STATE_FILE

    # ------------------------------------------------------------------
    # Persistence operations
    # ------------------------------------------------------------------

    async def save_to_disk(self, file_path: Optional[Union[Path, str]] = None) -> None:
        """Save current in-memory jobs and rollouts to JSON file atomically."""
        async with self._lock:
            self._save_to_disk_unlocked(file_path)

    def _save_to_disk_unlocked(self, file_path: Optional[Union[Path, str]] = None) -> None:
        path = Path(file_path) if file_path else self.state_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "jobs": {jid: j.to_dict() for jid, j in self._jobs.items()},
                "rollouts": {rid: r.to_dict() for rid, r in self._rollouts.items()},
            }
            tmp_path = path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(path)
            logger.debug(f"Saved {len(self._jobs)} jobs and {len(self._rollouts)} rollouts to {path}")
        except Exception as e:
            logger.error(f"Failed to save job state to disk at {path}: {e}")

    async def load_from_disk(self, file_path: Optional[Union[Path, str]] = None) -> None:
        """Load jobs and rollouts from JSON file into memory."""
        path = Path(file_path) if file_path else self.state_file
        if not path.exists():
            logger.info(f"State file {path} does not exist. Starting with empty store.")
            return

        async with self._lock:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if not content:
                    return
                data = json.loads(content)
                raw_jobs = data.get("jobs", {})
                raw_rollouts = data.get("rollouts", {})

                loaded_jobs = {}
                for jid, jdata in raw_jobs.items():
                    loaded_jobs[jid] = Job.from_dict(jdata)

                loaded_rollouts = {}
                for rid, rdata in raw_rollouts.items():
                    loaded_rollouts[rid] = Rollout.from_dict(rdata)

                self._jobs = loaded_jobs
                self._rollouts = loaded_rollouts
                logger.info(f"Loaded {len(self._jobs)} jobs and {len(self._rollouts)} rollouts from {path}")
            except Exception as e:
                logger.error(f"Failed to load job state from {path}: {e}")

    # ------------------------------------------------------------------
    # Job operations
    # ------------------------------------------------------------------

    async def create_job(self, job: Job, rollouts: List[Rollout]) -> None:
        async with self._lock:
            self._jobs[job.id] = job
            for r in rollouts:
                self._rollouts[r.id] = r
            self._save_to_disk_unlocked()

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
            self._save_to_disk_unlocked()
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
            self._save_to_disk_unlocked()

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

