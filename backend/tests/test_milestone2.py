"""
Milestone 2 Unit Tests.
Tests task validation, rollout factory, failure isolation, and job progress.
Run with: python -m pytest backend/tests/test_milestone2.py -v
"""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch

from backend.jobs import TaskValidationError, create_job, load_and_validate_tasks
from backend.models import ErrorType, JobStatus, Rollout, RolloutStatus
from backend.store import JobStore


# ---------------------------------------------------------------------------
# Helper fixtures
# ---------------------------------------------------------------------------

VALID_TASKS = [
    {"id": "problem1", "task": "Find total orders", "answer": "42"},
    {"id": "problem2", "task": "Find average revenue", "answer": "100.0"},
    {"id": "problem3", "task": "List top products", "answer": '["A","B"]'},
]


def valid_tasks_bytes(tasks=None) -> bytes:
    return json.dumps(tasks or VALID_TASKS).encode()


# ---------------------------------------------------------------------------
# Task Ingestion Validation
# ---------------------------------------------------------------------------

class TestTaskValidation:
    def test_valid_tasks(self):
        tasks = load_and_validate_tasks(valid_tasks_bytes(), attempts=2)
        assert len(tasks) == 3

    def test_invalid_json(self):
        with pytest.raises(TaskValidationError, match="Invalid JSON"):
            load_and_validate_tasks(b"not json", attempts=1)

    def test_not_a_list(self):
        with pytest.raises(TaskValidationError, match="must be a JSON array"):
            load_and_validate_tasks(json.dumps({"id": "x"}).encode(), attempts=1)

    def test_empty_list(self):
        with pytest.raises(TaskValidationError, match="at least one task"):
            load_and_validate_tasks(b"[]", attempts=1)

    def test_missing_id_field(self):
        bad = [{"task": "Do something", "answer": "42"}]
        with pytest.raises(TaskValidationError, match="missing required field 'id'"):
            load_and_validate_tasks(json.dumps(bad).encode(), attempts=1)

    def test_missing_task_field(self):
        bad = [{"id": "p1", "answer": "42"}]
        with pytest.raises(TaskValidationError, match="missing required field 'task'"):
            load_and_validate_tasks(json.dumps(bad).encode(), attempts=1)

    def test_missing_answer_field(self):
        bad = [{"id": "p1", "task": "Do something"}]
        with pytest.raises(TaskValidationError, match="missing required field 'answer'"):
            load_and_validate_tasks(json.dumps(bad).encode(), attempts=1)

    def test_empty_task_prompt(self):
        bad = [{"id": "p1", "task": "   ", "answer": "42"}]
        with pytest.raises(TaskValidationError, match="non-empty string"):
            load_and_validate_tasks(json.dumps(bad).encode(), attempts=1)

    def test_duplicate_ids(self):
        bad = [
            {"id": "p1", "task": "Task one", "answer": "1"},
            {"id": "p1", "task": "Task two", "answer": "2"},
        ]
        with pytest.raises(TaskValidationError, match="Duplicate task ID"):
            load_and_validate_tasks(json.dumps(bad).encode(), attempts=1)

    def test_attempts_zero(self):
        with pytest.raises(TaskValidationError, match="attempts must be >= 1"):
            load_and_validate_tasks(valid_tasks_bytes(), attempts=0)


# ---------------------------------------------------------------------------
# Rollout Factory
# ---------------------------------------------------------------------------

class TestRolloutFactory:
    def test_correct_rollout_count(self):
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=2)
        job, rollouts = create_job(raw, attempts=2)
        assert len(rollouts) == 6  # 3 tasks × 2 attempts

    def test_unique_rollout_ids(self):
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=3)
        _, rollouts = create_job(raw, attempts=3)
        ids = [r.id for r in rollouts]
        assert len(ids) == len(set(ids))

    def test_all_rollouts_queued(self):
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=2)
        _, rollouts = create_job(raw, attempts=2)
        assert all(r.status == RolloutStatus.QUEUED for r in rollouts)

    def test_attempt_numbers(self):
        raw = load_and_validate_tasks(valid_tasks_bytes()[:], attempts=3)
        raw_single = [VALID_TASKS[0]]
        raw_s = load_and_validate_tasks(json.dumps(raw_single).encode(), attempts=3)
        _, rollouts = create_job(raw_s, attempts=3)
        assert [r.attempt_number for r in rollouts] == [1, 2, 3]

    def test_job_total_field(self):
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=2)
        job, rollouts = create_job(raw, attempts=2)
        assert job.total == 6

    def test_job_status_queued(self):
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=1)
        job, _ = create_job(raw, attempts=1)
        assert job.status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# Job Store
# ---------------------------------------------------------------------------

class TestJobStore:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_create_and_get_job(self):
        store = JobStore()
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=1)
        job, rollouts = create_job(raw, attempts=1)
        self._run(store.create_job(job, rollouts))
        retrieved = self._run(store.get_job(job.id))
        assert retrieved is not None
        assert retrieved.id == job.id

    def test_get_rollout(self):
        store = JobStore()
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=1)
        job, rollouts = create_job(raw, attempts=1)
        self._run(store.create_job(job, rollouts))
        r = self._run(store.get_rollout(rollouts[0].id))
        assert r is not None
        assert r.status == RolloutStatus.QUEUED

    def test_update_rollout_changes_status(self):
        store = JobStore()
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=1)
        job, rollouts = create_job(raw, attempts=1)
        self._run(store.create_job(job, rollouts))
        rid = rollouts[0].id
        self._run(store.update_rollout(rid, status=RolloutStatus.PASSED, reward=1.0))
        r = self._run(store.get_rollout(rid))
        assert r.status == RolloutStatus.PASSED
        assert r.reward == 1.0

    def test_job_progress_recomputed(self):
        store = JobStore()
        raw = [VALID_TASKS[0]]
        raw_v = load_and_validate_tasks(json.dumps(raw).encode(), attempts=2)
        job, rollouts = create_job(raw_v, attempts=2)
        asyncio.run(store.create_job(job, rollouts))
        # Mark one PASSED, one FAILED
        asyncio.run(store.update_rollout(rollouts[0].id, status=RolloutStatus.PASSED, reward=1.0))
        asyncio.run(store.update_rollout(rollouts[1].id, status=RolloutStatus.FAILED, reward=0.0))
        j = asyncio.run(store.get_job(job.id))
        assert j.passed == 1
        assert j.failed == 1
        assert j.completed == 2
        assert j.status == JobStatus.COMPLETED

    def test_cancel_job(self):
        store = JobStore()
        raw = load_and_validate_tasks(valid_tasks_bytes(), attempts=1)
        job, rollouts = create_job(raw, attempts=1)
        asyncio.run(store.create_job(job, rollouts))
        cancelled = asyncio.run(store.cancel_job(job.id))
        assert cancelled is True
        j = asyncio.run(store.get_job(job.id))
        assert j.status == JobStatus.CANCELLED


# ---------------------------------------------------------------------------
# Failure Isolation Test
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    """
    One rollout raises an exception; the other rollouts must still complete as PASSED.
    Tests the worker pool exception boundary from workers.py.
    """

    def test_one_error_does_not_kill_others(self):
        """
        Mock execute_mock_rollout so rollout[0] raises, [1] and [2] complete normally.
        After running the worker pool, rollout[0] = ERROR, [1,2] = PASSED.
        """
        import asyncio
        from unittest.mock import patch, AsyncMock
        from backend.workers import run_job

        raw = [VALID_TASKS[0], VALID_TASKS[1], VALID_TASKS[2]]
        raw_v = load_and_validate_tasks(json.dumps(raw).encode(), attempts=1)
        job, rollouts = create_job(raw_v, attempts=1)

        store = JobStore()
        asyncio.run(store.create_job(job, rollouts))

        call_count = [0]

        async def mock_executor(rollout_id: str, s: JobStore):
            idx = call_count[0]
            call_count[0] += 1
            if idx == 0:
                raise RuntimeError("Intentional failure for rollout 0")
            from datetime import datetime
            await s.update_rollout(
                rollout_id,
                status=RolloutStatus.PASSED,
                reward=1.0,
                completed_at=datetime.utcnow().isoformat(),
                termination_reason="task_passed",
            )

        with patch("backend.executor_mock.execute_mock_rollout", side_effect=mock_executor):
            asyncio.run(run_job(job, rollouts, store, mock=True))

        r0 = asyncio.run(store.get_rollout(rollouts[0].id))
        r1 = asyncio.run(store.get_rollout(rollouts[1].id))
        r2 = asyncio.run(store.get_rollout(rollouts[2].id))

        assert r0.status == RolloutStatus.ERROR, f"Expected ERROR, got {r0.status}"
        assert r1.status == RolloutStatus.PASSED, f"Expected PASSED, got {r1.status}"
        assert r2.status == RolloutStatus.PASSED, f"Expected PASSED, got {r2.status}"


# ---------------------------------------------------------------------------
# Job Progress Counting
# ---------------------------------------------------------------------------

class TestJobProgressCounting:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_success_rate_excludes_infra_errors(self):
        """Infrastructure errors (ERROR/TIMEOUT) don't count as agent failures."""
        store = JobStore()
        tasks_raw = VALID_TASKS[:3]
        raw_v = load_and_validate_tasks(json.dumps(tasks_raw).encode(), attempts=1)
        job, rollouts = create_job(raw_v, attempts=1)
        self._run(store.create_job(job, rollouts))

        from datetime import datetime
        self._run(store.update_rollout(rollouts[0].id, status=RolloutStatus.PASSED, reward=1.0, completed_at=datetime.utcnow().isoformat()))
        self._run(store.update_rollout(rollouts[1].id, status=RolloutStatus.FAILED, reward=0.0, completed_at=datetime.utcnow().isoformat()))
        self._run(store.update_rollout(rollouts[2].id, status=RolloutStatus.ERROR, reward=None, completed_at=datetime.utcnow().isoformat()))

        j = self._run(store.get_job(job.id))
        assert j.passed == 1
        assert j.failed == 1
        assert j.errors == 1
        assert j.completed == 3
        assert j.status == JobStatus.COMPLETED

        # Agent success rate = passed / (passed + failed) = 1/2 = 50%
        evaluated = j.passed + j.failed
        success_rate = j.passed / evaluated if evaluated > 0 else 0.0
        assert success_rate == 0.5
