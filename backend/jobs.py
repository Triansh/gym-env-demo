"""
Milestone 2 Task Ingestion and Rollout Factory.
Validates tasks.json content and creates Job + Rollout objects.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple

from backend.models import Job, JobStatus, Rollout, RolloutStatus, Task


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TaskValidationError(ValueError):
    """Raised when tasks.json content is malformed."""
    pass


def load_and_validate_tasks(content: bytes, attempts: int) -> List[Dict[str, Any]]:
    """
    Parse and validate raw tasks.json bytes.

    Rules:
    - Must be a JSON array.
    - Each item must have 'id', 'task', and 'answer' fields.
    - 'id' must be a non-empty string.
    - 'task' must be a non-empty string.
    - No duplicate IDs.
    - attempts must be >= 1.

    Returns a list of raw task dicts.
    Raises TaskValidationError on any violation.
    """
    if attempts < 1:
        raise TaskValidationError("attempts must be >= 1")

    try:
        tasks = json.loads(content)
    except json.JSONDecodeError as e:
        raise TaskValidationError(f"Invalid JSON: {e}") from e

    if not isinstance(tasks, list):
        raise TaskValidationError("tasks.json must be a JSON array")

    if len(tasks) == 0:
        raise TaskValidationError("tasks.json must contain at least one task")

    seen_ids: set = set()
    for idx, t in enumerate(tasks):
        if not isinstance(t, dict):
            raise TaskValidationError(f"Task at index {idx} is not an object")

        for required in ("id", "task", "answer"):
            if required not in t:
                raise TaskValidationError(
                    f"Task at index {idx} is missing required field '{required}'"
                )

        task_id = t["id"]
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskValidationError(f"Task at index {idx}: 'id' must be a non-empty string")

        task_prompt = t["task"]
        if not isinstance(task_prompt, str) or not task_prompt.strip():
            raise TaskValidationError(
                f"Task '{task_id}': 'task' prompt must be a non-empty string"
            )

        if task_id in seen_ids:
            raise TaskValidationError(f"Duplicate task ID: '{task_id}'")
        seen_ids.add(task_id)

    return tasks


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_job(raw_tasks: List[Dict[str, Any]], attempts: int) -> Tuple[Job, List[Rollout]]:
    """
    Create a Job and all its Rollout objects from validated raw task dicts.

    Returns (job, rollouts) — nothing is persisted yet. Caller must add to store.
    """
    job_id = str(uuid.uuid4())

    tasks = [
        Task(
            id=t["id"],
            prompt=t["task"],
            expected_answer=t["answer"],
        )
        for t in raw_tasks
    ]

    rollouts: List[Rollout] = []
    rollout_ids: List[str] = []

    for task in tasks:
        for attempt in range(1, attempts + 1):
            rollout_id = str(uuid.uuid4())
            rollout_ids.append(rollout_id)
            rollouts.append(
                Rollout(
                    id=rollout_id,
                    job_id=job_id,
                    task_id=task.id,
                    attempt_number=attempt,
                    status=RolloutStatus.QUEUED,
                )
            )

    job = Job(
        id=job_id,
        tasks=tasks,
        rollout_ids=rollout_ids,
        attempts_per_task=attempts,
        status=JobStatus.QUEUED,
        created_at=datetime.utcnow().isoformat(),
        total=len(rollouts),
    )

    return job, rollouts
