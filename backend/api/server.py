"""
Milestone 2 FastAPI Server.
Thin HTTP interface over the in-memory JobStore and asyncio worker pool.
State is in-memory (per M2 spec — no database required).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.configs import ARTIFACTS_ROOT, MAX_ATTEMPTS, MAX_CONCURRENT_ROLLOUTS, MOCK_ROLLOUTS
from backend.jobs import TaskValidationError, create_job, load_and_validate_tasks
from backend.models import JobStatus, RolloutStatus
from backend.store import JobStore
from backend.workers import run_job

logger = logging.getLogger("backend.api")

app = FastAPI(title="Metabase RL Workbench API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

store = JobStore()


@app.get("/api/config")
async def get_config():
    return {
        "max_attempts": MAX_ATTEMPTS,
        "max_concurrent_rollouts": MAX_CONCURRENT_ROLLOUTS,
        "mock_rollouts": MOCK_ROLLOUTS,
    }

@app.get("/api/jobs")
async def list_jobs():
    jobs = await store.list_jobs()
    return [j.to_dict() for j in jobs]

@app.post("/api/jobs", status_code=201)
async def create_new_job(
    request: Request,
    file: Optional[UploadFile] = File(None),
    attempts: Optional[int] = Form(None),
):
    content_type = request.headers.get("content-type", "")
    attempts_val = attempts if attempts is not None else 1

    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

        if isinstance(body, dict):
            raw_tasks_data = body.get("tasks")
            attempts_val = body.get("attempts", body.get("attempts_per_task", attempts_val))
            if raw_tasks_data is None:
                contents = json.dumps(body).encode("utf-8")
            elif isinstance(raw_tasks_data, list):
                contents = json.dumps(raw_tasks_data).encode("utf-8")
            elif isinstance(raw_tasks_data, str):
                contents = raw_tasks_data.encode("utf-8")
            else:
                contents = json.dumps(body).encode("utf-8")
        elif isinstance(body, list):
            contents = json.dumps(body).encode("utf-8")
        else:
            raise HTTPException(status_code=400, detail="JSON body must be an array or object")
    elif file is not None:
        contents = await file.read()
    else:
        contents = await request.body()

    if attempts_val < 1 or attempts_val > MAX_ATTEMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"attempts must be between 1 and {MAX_ATTEMPTS}",
        )

    try:
        raw_tasks = load_and_validate_tasks(contents, attempts_val)
    except TaskValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job, rollouts = create_job(raw_tasks, attempts_val)
    await store.create_job(job, rollouts)

    # Dispatch rollouts in a background asyncio task
    import asyncio
    asyncio.create_task(run_job(job, rollouts, store, mock=MOCK_ROLLOUTS))

    logger.info(f"Created job {job.id} with {len(rollouts)} rollouts (mock={MOCK_ROLLOUTS})")

    return {
        "job_id": job.id,
        "total_rollouts": len(rollouts),
        "attempts_per_task": attempts_val,
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


@app.on_event("startup")
async def on_startup():
    await store.load_from_disk()
    await sync_disk_artifacts_to_store()


async def sync_disk_artifacts_to_store():
    """Scan ARTIFACTS_ROOT for existing result.json files and load into store if missing."""
    if not ARTIFACTS_ROOT.exists():
        return
    updated = False
    for result_path in ARTIFACTS_ROOT.glob("*/*/*/result.json"):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            job_id = data.get("job_id")
            rollout_id = data.get("rollout_id")
            if not job_id or not rollout_id:
                continue

            existing_job = await store.get_job(job_id)
            task_id = data.get("task_id", "problem1")
            raw_status = data.get("status", "PASSED")
            try:
                r_status = RolloutStatus(raw_status)
            except ValueError:
                r_status = RolloutStatus.PASSED

            from backend.models import Job, Rollout, Task

            rollout_obj = Rollout(
                id=rollout_id,
                job_id=job_id,
                task_id=task_id,
                attempt_number=data.get("attempt_number", 1),
                status=r_status,
                reward=data.get("reward", 0.0),
                duration_seconds=data.get("duration_seconds", 0.0),
                started_at=data.get("started_at"),
                completed_at=data.get("completed_at"),
                error_message=data.get("error") or data.get("error_message"),
                termination_reason=data.get("termination_reason"),
                artifact_path=data.get("artifact_path", f"rollout_artifacts/{job_id}/{task_id}/1"),
                transcript_file=data.get("transcript_file", "transcript.json"),
                screenshots=data.get("screenshots", []),
            )

            if not existing_job:
                synthetic_task = Task(id=task_id, prompt=f"Task {task_id}", expected_answer="")
                synthetic_job = Job(
                    id=job_id,
                    tasks=[synthetic_task],
                    rollout_ids=[rollout_id],
                    attempts_per_task=1,
                    status=JobStatus.COMPLETED,
                    created_at=data.get("started_at", ""),
                    completed_at=data.get("completed_at", ""),
                    total=1,
                    passed=1 if r_status == RolloutStatus.PASSED else 0,
                    failed=1 if r_status == RolloutStatus.FAILED else 0,
                )
                await store.create_job(synthetic_job, [rollout_obj])
                updated = True
            else:
                existing_rollouts = await store.get_rollouts_for_job(job_id)
                if not any(r.id == rollout_id for r in existing_rollouts):
                    async with store._lock:
                        store._rollouts[rollout_id] = rollout_obj
                        if rollout_id not in existing_job.rollout_ids:
                            existing_job.rollout_ids.append(rollout_id)
                        store._recompute_job_progress(job_id)
                        store._save_to_disk_unlocked()
                    updated = True
        except Exception as e:
            logger.warning(f"Error syncing disk artifact {result_path}: {e}")

    if updated:
        await store.save_to_disk()


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


