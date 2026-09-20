"""
Milestone 2 In-Memory Data Models.
Job, Task, and Rollout dataclasses with full field sets per the M2 plan.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Status Enums
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RolloutStatus(str, Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    GRADING = "GRADING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class ErrorType(str, Enum):
    TASK_VALIDATION_ERROR = "TASK_VALIDATION_ERROR"
    ENVIRONMENT_START_ERROR = "ENVIRONMENT_START_ERROR"
    ENVIRONMENT_HEALTH_TIMEOUT = "ENVIRONMENT_HEALTH_TIMEOUT"
    ENVIRONMENT_INITIALIZATION_ERROR = "ENVIRONMENT_INITIALIZATION_ERROR"
    BROWSER_START_ERROR = "BROWSER_START_ERROR"
    AGENT_ERROR = "AGENT_ERROR"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    GRADER_ERROR = "GRADER_ERROR"
    CLEANUP_ERROR = "CLEANUP_ERROR"
    TASK_FAILED = "TASK_FAILED"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    prompt: str
    expected_answer: Any  # grader data; never sent to the agent


@dataclass
class Rollout:
    id: str
    job_id: str
    task_id: str
    attempt_number: int
    status: RolloutStatus = RolloutStatus.QUEUED
    reward: Optional[float] = None          # None = not evaluated (infra failure)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    termination_reason: Optional[str] = None
    error_type: Optional[ErrorType] = None
    error_message: Optional[str] = None
    error_stage: Optional[str] = None
    cleanup_error: Optional[str] = None
    transcript: List[Any] = field(default_factory=list)
    grader_result: Dict[str, Any] = field(default_factory=dict)
    artifact_path: Optional[str] = None
    screenshots: List[str] = field(default_factory=list)
    agent_claim: str = ""

    def to_summary(self) -> Dict[str, Any]:
        """Lightweight representation for list endpoints."""
        status_val = self.status.value if isinstance(self.status, Enum) else str(self.status)
        error_val = self.error_type.value if isinstance(self.error_type, Enum) else self.error_type
        return {
            "rollout_id": self.id,
            "job_id": self.job_id,
            "task_id": self.task_id,
            "attempt_number": self.attempt_number,
            "status": status_val,
            "reward": self.reward,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "termination_reason": self.termination_reason,
            "error_type": error_val,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Full representation for detail endpoints and persistence."""
        status_val = self.status.value if isinstance(self.status, Enum) else str(self.status)
        error_val = self.error_type.value if isinstance(self.error_type, Enum) else self.error_type
        return {
            "rollout_id": self.id,
            "job_id": self.job_id,
            "task_id": self.task_id,
            "attempt_number": self.attempt_number,
            "status": status_val,
            "reward": self.reward,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "termination_reason": self.termination_reason,
            "error_type": error_val,
            "error_message": self.error_message,
            "error_stage": self.error_stage,
            "cleanup_error": self.cleanup_error,
            "transcript": self.transcript,
            "grader_result": self.grader_result,
            "artifact_path": self.artifact_path,
            "screenshots": self.screenshots,
            "agent_claim": self.agent_claim,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Rollout:
        """Construct a Rollout instance from a dictionary representation."""
        raw_status = data.get("status", "QUEUED")
        status_mapping = {
            "PASS": RolloutStatus.PASSED,
            "PASSED": RolloutStatus.PASSED,
            "FAIL": RolloutStatus.FAILED,
            "FAILED": RolloutStatus.FAILED,
        }
        if raw_status in status_mapping:
            status = status_mapping[raw_status]
        else:
            try:
                status = RolloutStatus(raw_status)
            except ValueError:
                status = RolloutStatus.QUEUED

        raw_error = data.get("error_type")
        error_type = None
        if raw_error:
            try:
                error_type = ErrorType(raw_error)
            except ValueError:
                error_type = None

        return cls(
            id=data.get("rollout_id") or data.get("id") or str(uuid.uuid4()),
            job_id=data.get("job_id", ""),
            task_id=data.get("task_id", ""),
            attempt_number=data.get("attempt_number", 1),
            status=status,
            reward=data.get("reward"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at") or data.get("finished_at"),
            duration_seconds=data.get("duration_seconds"),
            termination_reason=data.get("termination_reason"),
            error_type=error_type,
            error_message=data.get("error_message") or data.get("error"),
            error_stage=data.get("error_stage"),
            cleanup_error=data.get("cleanup_error"),
            transcript=data.get("transcript") or [],
            grader_result=data.get("grader_result") or {},
            artifact_path=data.get("artifact_path") or data.get("artifacts_dir"),
            screenshots=data.get("screenshots") or [],
            agent_claim=data.get("agent_claim", ""),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            RolloutStatus.PASSED,
            RolloutStatus.FAILED,
            RolloutStatus.ERROR,
            RolloutStatus.TIMEOUT,
            RolloutStatus.CANCELLED,
        )


@dataclass
class Job:
    id: str
    tasks: List[Task]
    rollout_ids: List[str]
    attempts_per_task: int
    status: JobStatus = JobStatus.QUEUED
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total: int = 0
    completed: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    timeouts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        status_val = self.status.value if isinstance(self.status, Enum) else str(self.status)
        return {
            "job_id": self.id,
            "status": status_val,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "attempts_per_task": self.attempts_per_task,
            "total": self.total,
            "completed": self.completed,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "rollout_ids": self.rollout_ids,
            "tasks": [
                {
                    "task_id": t.id,
                    "task": t.prompt,
                    "expected_answer": t.expected_answer,
                }
                for t in self.tasks
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Job:
        """Construct a Job instance from a dictionary representation."""
        raw_status = data.get("status", "QUEUED")
        status_mapping = {
            "COMPLETE": JobStatus.COMPLETED,
            "COMPLETED": JobStatus.COMPLETED,
        }
        if raw_status in status_mapping:
            status = status_mapping[raw_status]
        else:
            try:
                status = JobStatus(raw_status)
            except ValueError:
                status = JobStatus.QUEUED

        raw_tasks = data.get("tasks", [])
        tasks = []
        for t in raw_tasks:
            t_id = t.get("task_id") or t.get("id") or "task"
            t_prompt = t.get("task") or t.get("prompt") or ""
            t_ans = t.get("expected_answer", t.get("answer", ""))
            tasks.append(Task(id=t_id, prompt=t_prompt, expected_answer=t_ans))

        return cls(
            id=data.get("job_id") or data.get("id") or str(uuid.uuid4()),
            tasks=tasks,
            rollout_ids=data.get("rollout_ids") or [],
            attempts_per_task=data.get("attempts_per_task", 1),
            status=status,
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at") or data.get("finished_at"),
            total=data.get("total", data.get("total_rollouts", 0)),
            completed=data.get("completed", 0),
            passed=data.get("passed", 0),
            failed=data.get("failed", 0),
            errors=data.get("errors", 0),
            timeouts=data.get("timeouts", 0),
        )

