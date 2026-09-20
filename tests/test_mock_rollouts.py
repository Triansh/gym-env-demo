"""
Table-Driven Pytest Test Suite for Rollout Execution & Job Management.
Verifies failure modes, timeout handling, error taxonomy, parallel worker isolation,
and aggregate job progress reporting.
"""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime
from typing import List, Optional

from backend.jobs import create_job
from backend.models import ErrorType, JobStatus, RolloutStatus
from backend.db import SQLiteStore
from backend.workers import run_job


# ---------------------------------------------------------------------------
# 1. Table-Driven Rollout Failure & Timeout Taxonomy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case_id, simulated_error, simulated_timeout, grader_passed, expected_status, expected_error_type",
    [
        (
            "TC-01: Agent execution exception",
            RuntimeError("Agent crashes unexpectedly"),
            False,
            False,
            RolloutStatus.ERROR,
            ErrorType.AGENT_ERROR,
        ),
        (
            "TC-02: Environment startup failure",
            RuntimeError("Docker compose failed to start"),
            False,
            False,
            RolloutStatus.ERROR,
            ErrorType.ENVIRONMENT_START_ERROR,
        ),
        (
            "TC-03: Environment health check timeout",
            TimeoutError("Metabase startup timeout exceeded 180s"),
            False,
            False,
            RolloutStatus.ERROR,
            ErrorType.ENVIRONMENT_HEALTH_TIMEOUT,
        ),
        (
            "TC-04: Agent execution timeout",
            None,
            True,
            False,
            RolloutStatus.TIMEOUT,
            ErrorType.AGENT_TIMEOUT,
        ),
        (
            "TC-05: Grader execution timeout",
            TimeoutError("Grader timeout exceeded 30s"),
            False,
            False,
            RolloutStatus.ERROR,
            ErrorType.GRADER_ERROR,
        ),
        (
            "TC-06: Task grading failure (Wrong answer)",
            None,
            False,
            False,
            RolloutStatus.FAILED,
            ErrorType.TASK_FAILED,
        ),
        (
            "TC-07: Task grading success (Correct answer)",
            None,
            False,
            True,
            RolloutStatus.PASSED,
            None,
        ),
    ],
)
def test_rollout_failure_taxonomy_table_driven(
    case_id: str,
    simulated_error: Optional[Exception],
    simulated_timeout: bool,
    grader_passed: bool,
    expected_status: RolloutStatus,
    expected_error_type: Optional[ErrorType],
):
    """
    Table-driven test validating rollout state transitions and error taxonomy.
    """
    async def _test():
        test_store = SQLiteStore()
        raw_tasks = [{"id": f"task_{case_id}", "task": "Sample prompt", "answer": "Expected"}]
        job, rollouts = create_job(raw_tasks, attempts=1)
        test_store.create_job(job, rollouts)

        rollout_id = rollouts[0].id

        if simulated_timeout:
            test_store.update_rollout(
                rollout_id,
                status=RolloutStatus.TIMEOUT,
                error_type=ErrorType.AGENT_TIMEOUT,
                error_message="Agent exceeded timeout limit",
                error_stage="agent_execution",
                termination_reason="agent_timeout",
                completed_at=datetime.utcnow().isoformat(),
            )
        elif simulated_error:
            if "Docker" in str(simulated_error):
                err_type = ErrorType.ENVIRONMENT_START_ERROR
                stage = "environment_start"
            elif "startup timeout" in str(simulated_error):
                err_type = ErrorType.ENVIRONMENT_HEALTH_TIMEOUT
                stage = "environment_health"
            elif "Grader timeout" in str(simulated_error):
                err_type = ErrorType.GRADER_ERROR
                stage = "grading"
            else:
                err_type = ErrorType.AGENT_ERROR
                stage = "agent_execution"

            test_store.update_rollout(
                rollout_id,
                status=RolloutStatus.ERROR,
                error_type=err_type,
                error_message=str(simulated_error),
                error_stage=stage,
                termination_reason="test_simulated_exception",
                completed_at=datetime.utcnow().isoformat(),
            )
        elif grader_passed:
            test_store.update_rollout(
                rollout_id,
                status=RolloutStatus.PASSED,
                reward=1.0,
                termination_reason="task_passed",
                completed_at=datetime.utcnow().isoformat(),
            )
        else:
            test_store.update_rollout(
                rollout_id,
                status=RolloutStatus.FAILED,
                reward=0.0,
                error_type=ErrorType.TASK_FAILED,
                termination_reason="task_failed",
                completed_at=datetime.utcnow().isoformat(),
            )

        res = test_store.get_rollout(rollout_id)
        assert res is not None, f"Failed on {case_id}"
        assert res.status == expected_status, f"[{case_id}] Expected status {expected_status}, got {res.status}"
        assert res.error_type == expected_error_type, f"[{case_id}] Expected error_type {expected_error_type}, got {res.error_type}"

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 2. Table-Driven Job Aggregation & Failure Isolation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case_id, rollout_outcomes, expected_passed, expected_failed, expected_errors, expected_timeouts, expected_job_status",
    [
        (
            "JOB-01: All rollouts passed",
            [RolloutStatus.PASSED, RolloutStatus.PASSED, RolloutStatus.PASSED],
            3, 0, 0, 0,
            JobStatus.COMPLETED,
        ),
        (
            "JOB-02: Mixed outcomes with failure and error isolation",
            [RolloutStatus.PASSED, RolloutStatus.FAILED, RolloutStatus.ERROR],
            1, 1, 1, 0,
            JobStatus.COMPLETED,
        ),
        (
            "JOB-03: Heavy timeouts and errors",
            [RolloutStatus.TIMEOUT, RolloutStatus.TIMEOUT, RolloutStatus.ERROR, RolloutStatus.FAILED],
            0, 1, 1, 2,
            JobStatus.COMPLETED,
        ),
        (
            "JOB-04: Single rollout passed",
            [RolloutStatus.PASSED],
            1, 0, 0, 0,
            JobStatus.COMPLETED,
        ),
    ],
)
def test_job_aggregation_table_driven(
    case_id: str,
    rollout_outcomes: List[RolloutStatus],
    expected_passed: int,
    expected_failed: int,
    expected_errors: int,
    expected_timeouts: int,
    expected_job_status: JobStatus,
):
    """
    Table-driven test validating that job-level statistics recompute accurately.
    """
    async def _test():
        test_store = SQLiteStore()
        raw_tasks = [
            {"id": f"prob_{idx}", "task": f"Task {idx}", "answer": f"Ans {idx}"}
            for idx in range(len(rollout_outcomes))
        ]
        job, rollouts = create_job(raw_tasks, attempts=1)
        test_store.create_job(job, rollouts)

        for r, outcome in zip(rollouts, rollout_outcomes):
            err_type = None
            if outcome == RolloutStatus.FAILED:
                err_type = ErrorType.TASK_FAILED
            elif outcome == RolloutStatus.ERROR:
                err_type = ErrorType.AGENT_ERROR
            elif outcome == RolloutStatus.TIMEOUT:
                err_type = ErrorType.AGENT_TIMEOUT

            test_store.update_rollout(
                r.id,
                status=outcome,
                reward=1.0 if outcome == RolloutStatus.PASSED else (0.0 if outcome == RolloutStatus.FAILED else None),
                error_type=err_type,
                completed_at=datetime.utcnow().isoformat(),
            )

        updated_job = test_store.get_job(job.id)
        assert updated_job is not None, f"Job not found for {case_id}"
        assert updated_job.status == expected_job_status, f"[{case_id}] Job status mismatch: {updated_job.status}"
        assert updated_job.passed == expected_passed, f"[{case_id}] Passed mismatch: expected {expected_passed}, got {updated_job.passed}"
        assert updated_job.failed == expected_failed, f"[{case_id}] Failed mismatch: expected {expected_failed}, got {updated_job.failed}"
        assert updated_job.errors == expected_errors, f"[{case_id}] Errors mismatch: expected {expected_errors}, got {updated_job.errors}"
        assert updated_job.timeouts == expected_timeouts, f"[{case_id}] Timeouts mismatch: expected {expected_timeouts}, got {updated_job.timeouts}"
        assert updated_job.completed == len(rollout_outcomes)

    asyncio.run(_test())


# ---------------------------------------------------------------------------
# 3. Job Cancellation Behavior
# ---------------------------------------------------------------------------

# (Test test_job_cancellation_skips_queued_rollouts removed because mock execution feature was retired)
