"""
Milestone 2 API Server
FastAPI backend for the Metabase RL evaluation platform.
"""
import os
import uuid
import json
import time
import logging
import threading
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, Future

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

from backend.configs import (
    MOCK_ROLLOUTS,
    MAX_WORKERS,
    MAX_ATTEMPTS,
    ARTIFACTS_ROOT,
    STATE_FILE
)

# ---------------------------------------------------------------------------
# In-memory state (also persisted to STATE_FILE)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}    # job_id -> job record
_rollouts: Dict[str, Dict[str, Any]] = {}  # rollout_id -> rollout record

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
_futures: Dict[str, Future] = {}  # rollout_id -> future


def _save_state():
    """Persist jobs + rollouts to disk (best-effort)."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"jobs": _jobs, "rollouts": _rollouts}, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"Could not save state: {e}")


def _load_state():
    """Load persisted state from disk if available."""
    global _jobs, _rollouts
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _jobs = data.get("jobs", {})
            _rollouts = data.get("rollouts", {})
            logger.info(f"Loaded {len(_jobs)} jobs and {len(_rollouts)} rollouts from state file.")
        except Exception as e:
            logger.warning(f"Could not load state: {e}")


# ---------------------------------------------------------------------------
# Job/Rollout helpers
# ---------------------------------------------------------------------------

def _make_job(job_id: str, tasks: List[Dict], attempts: int) -> Dict:
    rollout_ids = []
    for t in tasks:
        for _ in range(attempts):
            rollout_ids.append(str(uuid.uuid4())[:12])
    return {
        "job_id": job_id,
        "status": "QUEUED",
        "created_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "tasks_count": len(tasks),
        "attempts_per_task": attempts,
        "total_rollouts": len(tasks) * attempts,
        "tasks": [{"task_id": t["id"], "task": t["task"]} for t in tasks],
        "rollout_ids": rollout_ids,
    }


def _make_rollout(rollout_id: str, task_id: str, task_prompt: str, job_id: str) -> Dict:
    return {
        "rollout_id": rollout_id,
        "job_id": job_id,
        "task_id": task_id,
        "task_prompt": task_prompt,
        "status": "QUEUED",
        "reward": None,
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "agent_claim": "",
        "termination_reason": "",
        "grader_result": {},
        "transcript": [],
        "screenshots": [],
        "artifacts_dir": "",
    }


def _job_stats(job: Dict) -> Dict:
    """Compute aggregated stats for a job."""
    rollout_ids = job.get("rollout_ids", [])
    statuses = [_rollouts.get(rid, {}).get("status", "QUEUED") for rid in rollout_ids]
    passed = sum(1 for s in statuses if s == "PASS")
    failed = sum(1 for s in statuses if s == "FAIL")
    errors = sum(1 for s in statuses if s == "ERROR")
    timeouts = sum(1 for s in statuses if s == "TIMEOUT")
    running = sum(1 for s in statuses if s == "RUNNING")
    queued = sum(1 for s in statuses if s == "QUEUED")
    completed = passed + failed + errors + timeouts

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "timeouts": timeouts,
        "running": running,
        "queued": queued,
        "completed": completed,
        "total": len(rollout_ids),
    }


def _task_summaries(job: Dict) -> List[Dict]:
    """Collapse rollouts into per-task summaries."""
    task_map: Dict[str, List[str]] = {}
    for t in job.get("tasks", []):
        task_map[t["task_id"]] = []
    for rid in job.get("rollout_ids", []):
        r = _rollouts.get(rid, {})
        tid = r.get("task_id", "unknown")
        if tid not in task_map:
            task_map[tid] = []
        task_map[tid].append(rid)

    summaries = []
    for t in job.get("tasks", []):
        tid = t["task_id"]
        rids = task_map.get(tid, [])
        rolls = [_rollouts.get(rid, {}) for rid in rids]
        statuses = [r.get("status", "QUEUED") for r in rolls]
        passed = sum(1 for s in statuses if s == "PASS")
        failed = sum(1 for s in statuses if s == "FAIL")
        errors = sum(1 for s in statuses if s == "ERROR")
        timeouts = sum(1 for s in statuses if s == "TIMEOUT")
        running_count = sum(1 for s in statuses if s == "RUNNING")
        queued_count = sum(1 for s in statuses if s == "QUEUED")
        completed = passed + failed + errors + timeouts

        if all(s in ("PASS", "FAIL", "ERROR", "TIMEOUT") for s in statuses) and statuses:
            task_status = "COMPLETE"
        elif any(s == "RUNNING" for s in statuses):
            task_status = "RUNNING"
        elif all(s == "QUEUED" for s in statuses):
            task_status = "QUEUED"
        else:
            task_status = "RUNNING"

        success_rate = round(passed / len(rids) * 100, 1) if rids else 0.0

        summaries.append({
            "task_id": tid,
            "task": t["task"],
            "rollout_ids": rids,
            "total": len(rids),
            "completed": completed,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "timeouts": timeouts,
            "running": running_count,
            "queued": queued_count,
            "status": task_status,
            "success_rate": success_rate,
        })
    return summaries


# ---------------------------------------------------------------------------
# Mock rollout executor
# ---------------------------------------------------------------------------

def _run_mock_rollout(rollout_id: str):
    """Simulate a rollout with random outcome (for demo without Docker/Metabase)."""
    with _lock:
        rollout = _rollouts.get(rollout_id)
        if not rollout:
            return
        rollout["status"] = "RUNNING"
        rollout["started_at"] = datetime.utcnow().isoformat()

    # Simulate variable duration
    duration = random.uniform(8, 45)
    steps = random.randint(5, 18)
    time.sleep(min(duration, 12))  # Capped for demo speed

    transcript = []
    for i in range(steps):
        event_types = ["screenshot", "click", "type", "scroll", "navigate", "screenshot"]
        etype = random.choice(event_types)
        if etype == "click":
            targets = ["Questions", "New", "Browse Data", "Orders", "Products", "Save", "Run Query", "Visualize"]
            transcript.append({"type": "action", "action": "click", "target": random.choice(targets), "timestamp": f"T+{i*3}s"})
        elif etype == "type":
            transcript.append({"type": "action", "action": "type", "text": "SELECT * FROM products", "timestamp": f"T+{i*3}s"})
        elif etype == "navigate":
            transcript.append({"type": "action", "action": "navigate", "url": f"http://localhost:3000/question/{random.randint(1,50)}", "timestamp": f"T+{i*3}s"})
        elif etype == "scroll":
            transcript.append({"type": "action", "action": "scroll", "direction": "down", "timestamp": f"T+{i*3}s"})
        else:
            transcript.append({"type": "screenshot", "timestamp": f"T+{i*3}s"})

    outcomes = ["PASS", "PASS", "FAIL", "FAIL", "ERROR", "TIMEOUT"]
    weights = [0.35, 0.25, 0.20, 0.12, 0.05, 0.03]
    outcome = random.choices(outcomes, weights=weights, k=1)[0]

    grader_result = {
        "passed": outcome == "PASS",
        "reward": 1.0 if outcome == "PASS" else 0.0,
        "reason": "All fields matched." if outcome == "PASS" else (
            "Field mismatch: expected different values." if outcome == "FAIL" else
            ("Evaluation infrastructure error." if outcome == "ERROR" else "Rollout exceeded time limit.")
        ),
        "predicted": {"mock": True},
        "ground_truth": {"mock": True},
    }

    termination_map = {
        "PASS": "agent_completed_task_passed",
        "FAIL": "agent_completed_task_failed",
        "ERROR": "grader_exception",
        "TIMEOUT": "max_duration_exceeded",
    }

    with _lock:
        rollout = _rollouts.get(rollout_id)
        if not rollout:
            return
        rollout["status"] = outcome
        rollout["reward"] = grader_result["reward"]
        rollout["finished_at"] = datetime.utcnow().isoformat()
        rollout["duration_seconds"] = round(duration, 2)
        rollout["agent_claim"] = f"Mock agent claim for {rollout['task_id']}: The answer is {{...}}" if outcome == "PASS" else "Mock agent could not determine the answer."
        rollout["termination_reason"] = termination_map.get(outcome, "unknown")
        rollout["grader_result"] = grader_result
        rollout["transcript"] = transcript
        rollout["screenshots"] = []  # No real screenshots in mock mode

        _update_job_status(rollout["job_id"])
        _save_state()


def _run_real_rollout(rollout_id: str):
    """Run a real rollout through the agent."""
    from backend.rollout.runner import run_rollout as _runner

    with _lock:
        rollout = _rollouts.get(rollout_id)
        if not rollout:
            return
        rollout["status"] = "RUNNING"
        rollout["started_at"] = datetime.utcnow().isoformat()
        task_id = rollout["task_id"]
        job_id = rollout["job_id"]

    try:
        result = _runner(task_id=task_id)
        artifacts_dir = Path(result.artifacts_dir)

        # Load transcript
        transcript_file = artifacts_dir / "transcript.json"
        transcript = []
        if transcript_file.exists():
            with open(transcript_file, "r") as f:
                raw = json.load(f)
            for item in raw:
                if isinstance(item, dict):
                    transcript.append(item)
                else:
                    transcript.append({"type": "raw", "content": str(item)})

        # List screenshots
        screenshots = sorted([p.name for p in artifacts_dir.glob("screenshot_*.png")])

        with _lock:
            rollout = _rollouts.get(rollout_id)
            if rollout:
                rollout.update({
                    "status": result.status,
                    "reward": result.reward,
                    "finished_at": result.finished_at,
                    "duration_seconds": result.duration_seconds,
                    "agent_claim": result.agent_claim,
                    "termination_reason": result.termination_reason,
                    "grader_result": result.grader_result,
                    "transcript": transcript,
                    "screenshots": screenshots,
                    "artifacts_dir": result.artifacts_dir,
                })
                _update_job_status(job_id)
                _save_state()

    except Exception as exc:
        logger.error(f"Real rollout {rollout_id} failed: {exc}")
        with _lock:
            rollout = _rollouts.get(rollout_id)
            if rollout:
                rollout.update({
                    "status": "ERROR",
                    "reward": 0.0,
                    "finished_at": datetime.utcnow().isoformat(),
                    "termination_reason": f"EXCEPTION: {exc}",
                })
                _update_job_status(job_id)
                _save_state()


def _update_job_status(job_id: str):
    """Recompute and update job status (called under _lock)."""
    job = _jobs.get(job_id)
    if not job:
        return
    stats = _job_stats(job)
    if stats["total"] == 0:
        return
    if stats["completed"] == stats["total"]:
        job["status"] = "COMPLETE"
        job["finished_at"] = datetime.utcnow().isoformat()
    elif job["status"] not in ("CANCELLED", "COMPLETE"):
        job["status"] = "RUNNING"


def _dispatch_rollouts(job_id: str, rollout_ids: List[str]):
    """Submit all rollouts to the thread pool."""
    runner_fn = _run_mock_rollout if MOCK_ROLLOUTS else _run_real_rollout
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job["status"] = "RUNNING"
    for rid in rollout_ids:
        future = _executor.submit(runner_fn, rid)
        _futures[rid] = future


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Metabase RL Evaluation API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    _load_state()
    # Mount static artifacts
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    logger.info(f"Mock mode: {MOCK_ROLLOUTS} | Max workers: {MAX_WORKERS}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@app.get("/api/config")
def get_config():
    return {"max_attempts": MAX_ATTEMPTS, "mock_mode": MOCK_ROLLOUTS, "max_workers": MAX_WORKERS}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
def list_jobs():
    with _lock:
        jobs_snapshot = list(_jobs.values())
    result = []
    for job in sorted(jobs_snapshot, key=lambda j: j.get("created_at", ""), reverse=True):
        stats = _job_stats(job)
        result.append({**job, **stats, "rollout_ids": job.get("rollout_ids", [])})
    return result


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), attempts: int = Form(3)):
    if attempts < 1 or attempts > MAX_ATTEMPTS:
        raise HTTPException(status_code=400, detail=f"attempts must be between 1 and {MAX_ATTEMPTS}")

    contents = await file.read()
    try:
        tasks = json.loads(contents)
        if not isinstance(tasks, list):
            raise ValueError("Expected a JSON array")
        for t in tasks:
            if "id" not in t or "task" not in t:
                raise ValueError("Each task must have 'id' and 'task' fields")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid tasks file: {e}")

    job_id = str(uuid.uuid4())[:12]
    job = _make_job(job_id, tasks, attempts)

    # Create rollout records
    rollout_ids = []
    for t in tasks:
        for _ in range(attempts):
            rid = str(uuid.uuid4())[:12]
            rollout_ids.append(rid)
            r = _make_rollout(rid, t["id"], t["task"], job_id)
            _rollouts[rid] = r

    job["rollout_ids"] = rollout_ids

    with _lock:
        _jobs[job_id] = job
        _save_state()

    # Dispatch in background
    _dispatch_rollouts(job_id, rollout_ids)

    stats = _job_stats(job)
    return {**job, **stats}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    stats = _job_stats(job)
    return {**job, **stats}


@app.get("/api/jobs/{job_id}/tasks")
def get_job_tasks(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _task_summaries(job)


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job["status"] not in ("QUEUED", "RUNNING"):
            raise HTTPException(status_code=400, detail=f"Cannot cancel a job with status {job['status']}")
        job["status"] = "CANCELLED"
        job["finished_at"] = datetime.utcnow().isoformat()
        # Mark queued/running rollouts as ERROR
        for rid in job.get("rollout_ids", []):
            r = _rollouts.get(rid)
            if r and r.get("status") in ("QUEUED", "RUNNING"):
                r["status"] = "ERROR"
                r["termination_reason"] = "job_cancelled"
        _save_state()
    return {"status": "CANCELLED"}


# ---------------------------------------------------------------------------
# Rollouts
# ---------------------------------------------------------------------------
@app.get("/api/rollouts/{rollout_id}")
def get_rollout(rollout_id: str):
    with _lock:
        rollout = _rollouts.get(rollout_id)
    if not rollout:
        raise HTTPException(status_code=404, detail="Rollout not found")
    return rollout


@app.get("/api/rollouts/{rollout_id}/screenshots")
def get_rollout_screenshots(rollout_id: str):
    with _lock:
        rollout = _rollouts.get(rollout_id)
    if not rollout:
        raise HTTPException(status_code=404, detail="Rollout not found")
    screenshots = rollout.get("screenshots", [])
    artifacts_dir = rollout.get("artifacts_dir", "")
    return {"rollout_id": rollout_id, "screenshots": screenshots, "artifacts_dir": artifacts_dir}


# ---------------------------------------------------------------------------
# Artifacts (screenshots etc.)
# ---------------------------------------------------------------------------
@app.get("/api/artifacts/{path:path}")
def get_artifact(path: str):
    # Security: only allow paths under ARTIFACTS_ROOT
    safe_path = (ARTIFACTS_ROOT / path).resolve()
    if not str(safe_path).startswith(str(ARTIFACTS_ROOT.resolve())):
        raise HTTPException(status_code=403, detail="Access denied")
    if not safe_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(str(safe_path))
