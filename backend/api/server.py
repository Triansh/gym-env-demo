"""
Milestone 2 FastAPI Server.
Thin HTTP interface over the in-memory JobStore and asyncio worker pool.
State is in-memory (per M2 spec — no database required).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List, Optional, Union

from pydantic import BaseModel, ConfigDict
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.configs import ARTIFACTS_ROOT, MAX_ATTEMPTS, MAX_CONCURRENT_ROLLOUTS
from backend.jobs import TaskValidationError, create_job, load_and_validate_tasks
from backend.models import JobStatus, RolloutStatus
from backend.db import SQLiteStore
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

store = SQLiteStore()

TASKS_JSON_PATH = Path(__file__).parent.parent.parent / "tasks.json"

class JobCreateRequest(BaseModel):
    tasks: Optional[Union[List[Any], str]] = None
    attempts: Optional[int] = None
    attempts_per_task: Optional[int] = None
    task_file: Optional[str] = None
    filename: Optional[str] = None
    task_file_name: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


@app.get("/")
async def serve_index():
    if FRONTEND_INDEX.exists():
        return FileResponse(str(FRONTEND_INDEX))
    return {"message": "Metabase RL Workbench API Server"}


@app.get("/tasks.json")
async def get_default_tasks_file():
    if TASKS_JSON_PATH.exists():
        return FileResponse(str(TASKS_JSON_PATH), media_type="application/json")
    raise HTTPException(status_code=404, detail="tasks.json not found")


@app.get("/api/config")
async def get_config():
    return {
        "max_attempts": MAX_ATTEMPTS,
        "max_concurrent_rollouts": MAX_CONCURRENT_ROLLOUTS,
    }

@app.get("/api/jobs")
async def list_jobs():
    jobs = store.list_jobs()
    return [j.to_dict() for j in jobs]


@app.get("/api/jobs/history")
async def get_jobs_history(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
):
    """
    Returns aggregated run history statistics and job list for the Job History dashboard.
    Supports filtering by status (COMPLETED, RUNNING, FAILED, CANCELLED, QUEUED) and search query.
    """
    return store.get_job_history(status=status, search=search, limit=limit, offset=offset)


@app.post("/api/jobs", status_code=201)
async def create_new_job(
    request: Request,
    file: Optional[UploadFile] = File(None),
    tasks_file: Optional[UploadFile] = File(None),
    attempts: Optional[int] = Form(None),
    attempts_per_task: Optional[int] = Form(None),
):
    content_type = request.headers.get("content-type", "")
    attempts_val = attempts if attempts is not None else (attempts_per_task if attempts_per_task is not None else 1)
    uploaded_file = file or tasks_file

    task_file_name = "tasks.json"
    if uploaded_file is not None and uploaded_file.filename:
        task_file_name = uploaded_file.filename

    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e}")

        if isinstance(body, dict):
            req = JobCreateRequest.model_validate(body)
            attempts_val = req.attempts or req.attempts_per_task or attempts_val
            task_file_name = req.task_file or req.filename or req.task_file_name or task_file_name
            contents = req.tasks if req.tasks is not None else body
            if isinstance(contents, str):
                contents = contents.encode("utf-8")
        elif isinstance(body, list):
            contents = body
        else:
            raise HTTPException(status_code=400, detail="JSON body must be an array or object")
    elif uploaded_file is not None and uploaded_file.filename:
        contents = await uploaded_file.read()
    else:
        default_tasks_path = TASKS_JSON_PATH
        if default_tasks_path.exists():
            contents = default_tasks_path.read_bytes()
        else:
            contents = b"[]"

    if attempts_val < 1 or attempts_val > MAX_ATTEMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"attempts must be between 1 and {MAX_ATTEMPTS}",
        )

    try:
        raw_tasks = load_and_validate_tasks(contents, attempts_val)
    except TaskValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job, rollouts = create_job(raw_tasks, attempts_val, task_file=task_file_name)
    store.create_job(job, rollouts)

    # Save uploaded/submitted task JSON file to job artifacts directory
    try:
        job_artifact_dir = ARTIFACTS_ROOT / job.id
        job_artifact_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            (job_artifact_dir / "tasks.json").write_bytes(contents)
        else:
            import json as json_module
            (job_artifact_dir / "tasks.json").write_text(json_module.dumps(contents, indent=2))
    except Exception as e:
        logger.warning(f"Could not write tasks.json artifact for job {job.id}: {e}")

    # Dispatch rollouts in a background asyncio task
    import asyncio
    asyncio.create_task(run_job(job, rollouts, store))

    logger.info(f"Created job {job.id} with {len(rollouts)} rollouts")

    return {
        "job_id": job.id,
        "total_rollouts": len(rollouts),
        "attempts_per_task": attempts_val,
        "status": job.status.value if hasattr(job.status, "value") else str(job.status),
    }


@app.get("/api/jobs/{job_id}/tasks.json")
async def get_job_tasks_json(job_id: str):
    """Retrieve the original tasks.json file submitted for a specific job."""
    task_file_path = ARTIFACTS_ROOT / job_id / "tasks.json"
    if task_file_path.exists():
        return FileResponse(str(task_file_path), media_type="application/json")

    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    tasks_data = [
        {"id": t.id, "task": t.prompt, "answer": t.expected_answer}
        for t in job.tasks
    ]
    return tasks_data


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/rollouts")
async def get_job_rollouts(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    rollouts = store.get_rollouts_for_job(job_id)
    return [r.to_summary() for r in rollouts]


@app.get("/api/jobs/{job_id}/tasks")
async def get_job_tasks(job_id: str):
    """Per-task aggregated summaries (for the frontend task view)."""
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    rollouts = store.get_rollouts_for_job(job_id)

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
            if passed > 0:
                task_status = "SUCCEEDED"
            else:
                task_status = "FAILED"
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
            "rollouts": [{"id": r.id, "status": r.status.value if hasattr(r.status, "value") else str(r.status)} for r in rlist],
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
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel a job with status {job.status}",
        )
    cancelled = store.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=400, detail="Job could not be cancelled")
    return {"status": "CANCELLED", "job_id": job_id}


# ---------------------------------------------------------------------------
# Rollouts
# ---------------------------------------------------------------------------

@app.get("/api/rollouts/{rollout_id}")
async def get_rollout(rollout_id: str):
    rollout = store.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(status_code=404, detail="Rollout not found")
    return rollout.to_dict()


@app.get("/api/rollouts/{rollout_id}/screenshots")
async def get_rollout_screenshots(rollout_id: str):
    rollout = store.get_rollout(rollout_id)
    if not rollout:
        raise HTTPException(status_code=404, detail="Rollout not found")
    return {
        "rollout_id": rollout_id,
        "screenshots": rollout.screenshots,
        "artifact_path": rollout.artifact_path,
    }


@app.on_event("startup")
async def on_startup():
    pass


# ---------------------------------------------------------------------------
# Artifacts & Frontend Static Mount
# ---------------------------------------------------------------------------
@app.get("/api/artifacts/{path:path}")
async def get_artifact(path: str):
    safe_path = (ARTIFACTS_ROOT / path).resolve()
    if not str(safe_path).startswith(str(ARTIFACTS_ROOT.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(str(safe_path))


from fastapi.staticfiles import StaticFiles

frontend_path = Path(__file__).parent.parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")


