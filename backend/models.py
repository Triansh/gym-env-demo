"""
Milestone 2 In-Memory Data Models.
Job, Task, and Rollout dataclasses with full field sets per the M2 plan.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

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

class Task(BaseModel):
    id: str
    prompt: str
    expected_answer: Any  # grader data; never sent to the agent


class Rollout(BaseModel):
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
    transcript: List[Any] = Field(default_factory=list)
    grader_result: Dict[str, Any] = Field(default_factory=dict)
    artifact_path: Optional[str] = None
    screenshots: List[str] = Field(default_factory=list)
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
        data = self.model_dump(mode="json")
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Rollout:
        """Construct a Rollout instance from a dictionary representation."""
        # Handle status logic for backward compatibility
        raw_status = data.get("status", "QUEUED")
        status_mapping = {
            "PASS": RolloutStatus.PASSED,
            "PASSED": RolloutStatus.PASSED,
            "FAIL": RolloutStatus.FAILED,
            "FAILED": RolloutStatus.FAILED,
        }
        if raw_status in status_mapping:
            data["status"] = status_mapping[raw_status]

        if "rollout_id" in data and "id" not in data:
            data["id"] = data["rollout_id"]
        
        if "finished_at" in data and "completed_at" not in data:
            data["completed_at"] = data["finished_at"]
            
        if "error" in data and "error_message" not in data:
            data["error_message"] = data["error"]
            
        if "artifacts_dir" in data and "artifact_path" not in data:
            data["artifact_path"] = data["artifacts_dir"]
        
        return cls.model_validate(data)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            RolloutStatus.PASSED,
            RolloutStatus.FAILED,
            RolloutStatus.ERROR,
            RolloutStatus.TIMEOUT,
            RolloutStatus.CANCELLED,
        )


class Job(BaseModel):
    id: str
    tasks: List[Task]
    rollout_ids: List[str]
    attempts_per_task: int
    status: JobStatus = JobStatus.QUEUED
    task_file: str = "tasks.json"
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total: int = 0
    completed: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    timeouts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Full representation for detail endpoints and persistence."""
        data = self.model_dump(mode="json")
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Job:
        """Construct a Job instance from a dictionary representation."""
        if "job_id" in data and "id" not in data:
            data["id"] = data["job_id"]
        return cls.model_validate(data)

class JobSummary(BaseModel):
    id: str
    status: JobStatus
    task_file: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempts_per_task: int
    total: int
    completed: int
    passed: int
    failed: int
    errors: int
    timeouts: int

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")
