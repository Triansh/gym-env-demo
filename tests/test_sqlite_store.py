"""
Unit tests for SQLite database persistence, SQLiteStore operations, and auto-migration.
"""
import asyncio
import json
import pytest
from pathlib import Path
from backend.db import SQLiteStore
from backend.models import Job, JobStatus, Rollout, RolloutStatus, Task
from backend.db import SQLiteStore


def test_sqlite_store_crud(tmp_path):
    db_file = tmp_path / "test_deeptune.db"
    store = SQLiteStore(db_path=db_file)

    # 1. Test Task CRUD
    task = Task(id="t1", prompt="Test Metabase query", expected_answer={"ans": 42})
    store.save_task(task)
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].id == "t1"
    assert tasks[0].prompt == "Test Metabase query"
    assert tasks[0].expected_answer == {"ans": 42}

    # 2. Test Job & Rollout CRUD
    job = Job(
        id="job_100",
        tasks=[task],
        rollout_ids=["rollout_100_1"],
        attempts_per_task=1,
        status=JobStatus.QUEUED,
        total=1,
    )
    rollout = Rollout(
        id="rollout_100_1",
        job_id="job_100",
        task_id="t1",
        attempt_number=1,
        status=RolloutStatus.QUEUED,
    )

    store.save_job(job)
    store.save_rollout(rollout)

    retrieved_job = store.get_job("job_100")
    assert retrieved_job is not None
    assert retrieved_job.id == "job_100"
    assert retrieved_job.attempts_per_task == 1

    retrieved_rollouts = store.get_rollouts_for_job("job_100")
    assert len(retrieved_rollouts) == 1
    assert retrieved_rollouts[0].id == "rollout_100_1"


def test_job_store_async_with_sqlite(tmp_path):
    async def _run():
        db_file = tmp_path / "test_jobstore.db"
        job_store = SQLiteStore(db_path=db_file)

        task = Task(id="t_metabase", prompt="Build card", expected_answer="OK")
        job = Job(
            id="job_200",
            tasks=[task],
            rollout_ids=["r_200_1"],
            attempts_per_task=1,
            status=JobStatus.QUEUED,
            total=1,
        )
        rollout = Rollout(
            id="r_200_1",
            job_id="job_200",
            task_id="t_metabase",
            attempt_number=1,
            status=RolloutStatus.QUEUED,
        )

        job_store.create_job(job, [rollout])

        # Retrieve job and rollout
        fetched_job = job_store.get_job("job_200")
        assert fetched_job is not None
        assert fetched_job.status == JobStatus.QUEUED

        # Update rollout status to PASSED and check job recomputation
        job_store.update_rollout("r_200_1", status=RolloutStatus.PASSED, reward=1.0)

        updated_job = job_store.get_job("job_200")
        assert updated_job.status == JobStatus.COMPLETED
        assert updated_job.passed == 1
        assert updated_job.completed == 1

        updated_rollout = job_store.get_rollout("r_200_1")
        assert updated_rollout.status == RolloutStatus.PASSED
        assert updated_rollout.reward == 1.0

    asyncio.run(_run())
