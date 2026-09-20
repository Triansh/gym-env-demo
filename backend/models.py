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
        return {
            "rollout_id": self.id,
            "job_id": self.job_id,
            "task_id": self.task_id,
            "attempt_number": self.attempt_number,
            "status": self.status,
            "reward": self.reward,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "termination_reason": self.termination_reason,
            "error_type": self.error_type,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Full representation for detail endpoints."""
        return {
            "rollout_id": self.id,
            "job_id": self.job_id,
            "task_id": self.task_id,
            "attempt_number": self.attempt_number,
            "status": self.status,
            "reward": self.reward,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "termination_reason": self.termination_reason,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "error_stage": self.error_stage,
            "cleanup_error": self.cleanup_error,
            "transcript": self.transcript,
            "grader_result": self.grader_result,
            "artifact_path": self.artifact_path,
            "screenshots": self.screenshots,
            "agent_claim": self.agent_claim,
        }

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
        return {
            "job_id": self.id,
            "status": self.status,
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
            "tasks": [{"task_id": t.id, "task": t.prompt} for t in self.tasks],
        }
