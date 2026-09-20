"""
Milestone 2 FastAPI Server.
Thin HTTP interface over the in-memory JobStore and asyncio worker pool.
State is in-memory (per M2 spec — no database required).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.configs import ARTIFACTS_ROOT, MAX_ATTEMPTS, MOCK_ROLLOUTS, MAX_CONCURRENT_ROLLOUTS
from backend.jobs import TaskValidationError, create_job, load_and_validate_tasks
from backend.models import JobStatus, RolloutStatus
from backend.store import JobStore
from backend.workers import run_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
store = JobStore()

app = FastAPI(title="Metabase RL Evaluation API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    logger.info(f"Mock mode: {MOCK_ROLLOUTS} | Max concurrent rollouts: {MAX_CONCURRENT_ROLLOUTS}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@app.get("/api/config")
async def get_config():
    return {
        "max_attempts": MAX_ATTEMPTS,
        "mock_mode": MOCK_ROLLOUTS,
        "max_concurrent_rollouts": MAX_CONCURRENT_ROLLOUTS,
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
async def list_jobs():
    jobs = await store.list_jobs()
    return [j.to_dict() for j in jobs]


@app.post("/api/jobs", status_code=201)
async def create_new_job(
    file: UploadFile = File(...),
    attempts: int = Form(1),
):
    if attempts < 1 or attempts > MAX_ATTEMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"attempts must be between 1 and {MAX_ATTEMPTS}",
        )

    contents = await file.read()

    try:
        raw_tasks = load_and_validate_tasks(contents, attempts)
    except TaskValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job, rollouts = create_job(raw_tasks, attempts)
    await store.create_job(job, rollouts)

    # Dispatch rollouts in a background asyncio task
    import asyncio
    asyncio.create_task(run_job(job, rollouts, store, mock=MOCK_ROLLOUTS))

    logger.info(f"Created job {job.id} with {len(rollouts)} rollouts (mock={MOCK_ROLLOUTS})")

    return {
        "job_id": job.id,
        "total_rollouts": len(rollouts),
        "attempts_per_task": attempts,
        "tasks_count": len(raw_tasks),
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/rollouts")
async def get_job_rollouts(job_id: str):
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    rollouts = await store.get_rollouts_for_job(job_id)
    return [r.to_summary() for r in rollouts]


@app.get("/api/jobs/{job_id}/tasks")
async def get_job_tasks(job_id: str):
    """Per-task aggregated summaries (for the frontend task view)."""
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    rollouts = await store.get_rollouts_for_job(job_id)

    # Group rollouts by task_id
    from collections import defaultdict
    task_rollouts: Dict[str, List] = defaultdict(list)
    for r in rollouts:
        task_rollouts[r.task_id].append(r)

    summaries = []
    for task in job.tasks:
        rlist = task_rollouts.get(task.id, [])
        statuses = [r.status for r in rlist]
        passed = sum(1 for s in statuses if s == RolloutStatus.PASSED)
        failed = sum(1 for s in statuses if s == RolloutStatus.FAILED)
        errors = sum(1 for s in statuses if s == RolloutStatus.ERROR)
        timeouts = sum(1 for s in statuses if s == RolloutStatus.TIMEOUT)
        running = sum(1 for s in statuses if s == RolloutStatus.RUNNING)
        queued = sum(1 for s in statuses if s == RolloutStatus.QUEUED)
        completed = passed + failed + errors + timeouts

        total = len(rlist)
        if total > 0 and all(r.is_terminal for r in rlist):
            task_status = "COMPLETE"
        elif any(s == RolloutStatus.RUNNING for s in statuses):
            task_status = "RUNNING"
        elif all(s == RolloutStatus.QUEUED for s in statuses):
            task_status = "QUEUED"
        else:
            task_status = "RUNNING"

        success_rate = round(passed / total * 100, 1) if total > 0 else 0.0

        summaries.append({
            "task_id": task.id,
            "task": task.prompt,
            "rollout_ids": [r.id for r in rlist],
            "total": total,
            "completed": completed,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "timeouts": timeouts,
            "running": running,
            "queued": queued,
            "status": task_status,
            "success_rate": success_rate,
        })

    return summaries


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel a job with status {job.status}",
        )
    cancelled = await store.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Job could not be cancelled")
    return {"status": "CANCELLED", "job_id": job_id}


# ---------------------------------------------------------------------------
# Rollouts
# ---------------------------------------------------------------------------

@app.get("/api/rollouts/{rollout_id}")
async def get_rollout(rollout_id: str):
    rollout = await store.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(status_code=404, detail="Rollout not found")
    return rollout.to_dict()


@app.get("/api/rollouts/{rollout_id}/screenshots")
async def get_rollout_screenshots(rollout_id: str):
    rollout = await store.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(status_code=404, detail="Rollout not found")
    return {
        "rollout_id": rollout_id,
        "screenshots": rollout.screenshots,
        "artifact_path": rollout.artifact_path,
    }


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@app.get("/api/artifacts/{path:path}")
async def get_artifact(path: str):
    safe_path = (ARTIFACTS_ROOT / path).resolve()
    if not str(safe_path).startswith(str(ARTIFACTS_ROOT.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(str(safe_path))
