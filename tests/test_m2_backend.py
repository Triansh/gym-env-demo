"""
Milestone 2 Backend Test Suite.
Tests task ingestion validation rules, in-memory store concurrency, failure isolation (Plan Sec 25),
parallel execution (Plan Sec 26), and FastAPI endpoint contracts.
"""
from __future__ import annotations

import asyncio
import json
import pytest
from fastapi.testclient import TestClient

from backend.api.server import app, store
from backend.jobs import TaskValidationError, create_job, load_and_validate_tasks
from backend.models import ErrorType, JobStatus, RolloutStatus
from backend.store import JobStore
from backend.workers import run_job, _worker


# ---------------------------------------------------------------------------
# 1. Task Ingestion & Validation Tests
# ---------------------------------------------------------------------------

def test_task_ingestion_valid():
    content = json.dumps([
        {"id": "prob1", "task": "Find total revenue", "answer": "1000"},
        {"id": "prob2", "task": "Count total orders", "answer": "50"},
    ]).encode("utf-8")
    
    tasks = load_and_validate_tasks(content, attempts=2)
    assert len(tasks) == 2
    
    job, rollouts = create_job(tasks, attempts=2)
    assert job.total == 4  # 2 tasks * 2 attempts = 4 rollouts
    assert len(rollouts) == 4
    assert job.attempts_per_task == 2


def test_task_ingestion_validation_failures():
    # Non-JSON
    with pytest.raises(TaskValidationError, match="Invalid JSON"):
        load_and_validate_tasks(b"invalid json", attempts=1)

    # Not array
    with pytest.raises(TaskValidationError, match="must be a JSON array"):
        load_and_validate_tasks(json.dumps({"id": "p1"}).encode("utf-8"), attempts=1)

    # Empty array
    with pytest.raises(TaskValidationError, match="at least one task"):
        load_and_validate_tasks(b"[]", attempts=1)

    # Missing fields
    missing_fields = json.dumps([{"id": "p1", "task": "Do something"}]).encode("utf-8")
    with pytest.raises(TaskValidationError, match="missing required field 'answer'"):
        load_and_validate_tasks(missing_fields, attempts=1)

    # Empty task prompt
    empty_prompt = json.dumps([{"id": "p1", "task": "   ", "answer": "ans"}]).encode("utf-8")
    with pytest.raises(TaskValidationError, match="'task' prompt must be a non-empty string"):
        load_and_validate_tasks(empty_prompt, attempts=1)

    # Duplicate IDs
    duplicate_ids = json.dumps([
        {"id": "p1", "task": "Task 1", "answer": "a1"},
        {"id": "p1", "task": "Task 2", "answer": "a2"},
    ]).encode("utf-8")
    with pytest.raises(TaskValidationError, match="Duplicate task ID: 'p1'"):
        load_and_validate_tasks(duplicate_ids, attempts=1)

    # Invalid attempts
    valid_content = json.dumps([{"id": "p1", "task": "Task 1", "answer": "a1"}]).encode("utf-8")
    with pytest.raises(TaskValidationError, match="attempts must be >= 1"):
        load_and_validate_tasks(valid_content, attempts=0)


# ---------------------------------------------------------------------------
# 2. Critical Failure Isolation Test (Plan Section 25)
# ---------------------------------------------------------------------------

def test_failure_isolation():
    """
    Rollout 1 -> intentional error
    Rollout 2 -> success (PASSED)
    Rollout 3 -> success (PASSED)

    Expected:
    - Job completes
    - Rollout 1 = ERROR
    - Rollout 2 = PASSED
    - Rollout 3 = PASSED
    """
    async def _run():
        test_store = JobStore()
        raw_tasks = [
            {"id": "task1", "task": "T1", "answer": "A1"},
            {"id": "task2", "task": "T2", "answer": "A2"},
            {"id": "task3", "task": "T3", "answer": "A3"},
        ]
        job, rollouts = create_job(raw_tasks, attempts=1)
        await test_store.create_job(job, rollouts)

        r1, r2, r3 = rollouts

        # Custom mock execution: r1 raises exception, r2 & r3 succeed
        async def mock_execute(rollout_id: str):
            if rollout_id == r1.id:
                raise RuntimeError("Simulated infrastructure crash for Rollout 1")
            await test_store.update_rollout(
                rollout_id,
                status=RolloutStatus.PASSED,
                reward=1.0,
                termination_reason="task_passed",
            )

        queue = asyncio.Queue()
        for r in rollouts:
            await queue.put(r.id)

        async def custom_worker(slot: int):
            while True:
                try:
                    rid = await queue.get()
                except asyncio.CancelledError:
                    break
                try:
                    r = await test_store.get_rollout(rid)
                    if r:
                        await mock_execute(rid)
                except Exception as exc:
                    await test_store.update_rollout(
                        rid,
                        status=RolloutStatus.ERROR,
                        error_type=ErrorType.ENVIRONMENT_START_ERROR,
                        error_message=str(exc),
                        error_stage="environment_start",
                        termination_reason="test_error",
                    )
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(custom_worker(i)) for i in range(2)]
        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        updated_job = await test_store.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status == JobStatus.COMPLETED
        assert updated_job.passed == 2
        assert updated_job.errors == 1
        assert updated_job.completed == 3

        res_r1 = await test_store.get_rollout(r1.id)
        res_r2 = await test_store.get_rollout(r2.id)
        res_r3 = await test_store.get_rollout(r3.id)

        assert res_r1.status == RolloutStatus.ERROR
        assert res_r1.error_type == ErrorType.ENVIRONMENT_START_ERROR
        assert res_r2.status == RolloutStatus.PASSED
        assert res_r3.status == RolloutStatus.PASSED

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3. Parallel Execution Test (Plan Section 26)
# ---------------------------------------------------------------------------

def test_parallel_execution_mock():
    """Verify mock worker pool processes all rollouts cleanly in parallel."""
    async def _run():
        test_store = JobStore()
        raw_tasks = [{"id": f"p{i}", "task": f"Task {i}", "answer": f"Ans {i}"} for i in range(5)]
        job, rollouts = create_job(raw_tasks, attempts=1)
        await test_store.create_job(job, rollouts)

        await run_job(job, rollouts, test_store, mock=True)

        updated_job = await test_store.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status == JobStatus.COMPLETED
        assert updated_job.completed == 5

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. FastAPI API Endpoint Tests
# ---------------------------------------------------------------------------

def test_fastapi_job_lifecycle():
    client = TestClient(app)

    # 1. Config check
    res = client.get("/api/config")
    assert res.status_code == 200
    assert "max_attempts" in res.json()

    # 2. Create job via file upload
    tasks_json = json.dumps([
        {"id": "api_t1", "task": "Compute total sales", "answer": "100"},
        {"id": "api_t2", "task": "Count active users", "answer": "50"},
    ])
    files = {"file": ("tasks.json", tasks_json.encode("utf-8"), "application/json")}
    res = client.post("/api/jobs", files=files, data={"attempts": "2"})
    assert res.status_code == 201
    data = res.json()
    assert "job_id" in data
    assert data["total_rollouts"] == 4
    job_id = data["job_id"]

    # 3. Get job details
    res = client.get(f"/api/jobs/{job_id}")
    assert res.status_code == 200
    job_data = res.json()
    assert job_data["job_id"] == job_id
    assert job_data["total"] == 4

    # 4. Get job rollouts summary
    res = client.get(f"/api/jobs/{job_id}/rollouts")
    assert res.status_code == 200
    rollouts_summary = res.json()
    assert len(rollouts_summary) == 4

    first_rollout_id = rollouts_summary[0]["rollout_id"]

    # 5. Get individual rollout detail
    res = client.get(f"/api/rollouts/{first_rollout_id}")
    assert res.status_code == 200
    r_detail = res.json()
    assert r_detail["rollout_id"] == first_rollout_id

    # 6. Get job per-task summary
    res = client.get(f"/api/jobs/{job_id}/tasks")
    assert res.status_code == 200
    tasks_summary = res.json()
    assert len(tasks_summary) == 2


def test_fastapi_job_json_payload():
    client = TestClient(app)

    payload = {
        "tasks": [
            {"id": "json_t1", "task": "Calculate average order value", "answer": "42.5"}
        ],
        "attempts": 1,
    }
    res = client.post("/api/jobs", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["total_rollouts"] == 1
    assert data["attempts_per_task"] == 1


# ---------------------------------------------------------------------------
# 5. JobStore Persistence & Rehydration Tests
# ---------------------------------------------------------------------------

def test_job_store_persistence(tmp_path):
    async def _run():
        state_file = tmp_path / "test_jobs_state.json"
        store = JobStore(state_file=state_file)

        raw_tasks = [
            {"id": "t1", "task": "Task 1", "answer": "Ans 1"},
            {"id": "t2", "task": "Task 2", "answer": "Ans 2"},
        ]
        job, rollouts = create_job(raw_tasks, attempts=1)
        await store.create_job(job, rollouts)

        # File should exist after create_job
        assert state_file.exists()
        with open(state_file, "r") as f:
            data = json.load(f)
        assert job.id in data["jobs"]
        assert len(data["rollouts"]) == 2

        # Update rollout status
        r1 = rollouts[0]
        await store.update_rollout(
            r1.id,
            status=RolloutStatus.PASSED,
            reward=1.0,
            duration_seconds=5.2,
            termination_reason="task_passed",
        )

        # Verify state file update
        with open(state_file, "r") as f:
            updated_data = json.load(f)
        assert updated_data["rollouts"][r1.id]["status"] == "PASSED"
        assert updated_data["jobs"][job.id]["passed"] == 1

        # Instantiate NEW store and rehydrate
        new_store = JobStore(state_file=state_file)
        await new_store.load_from_disk()

        rehydrated_job = await new_store.get_job(job.id)
        assert rehydrated_job is not None
        assert rehydrated_job.id == job.id
        assert len(rehydrated_job.tasks) == 2
        assert rehydrated_job.passed == 1

        rehydrated_r1 = await new_store.get_rollout(r1.id)
        assert rehydrated_r1 is not None
        assert rehydrated_r1.status == RolloutStatus.PASSED
        assert rehydrated_r1.reward == 1.0

    asyncio.run(_run())

