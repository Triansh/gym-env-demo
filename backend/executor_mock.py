"""
Mock rollout executor — simulates random outcomes for demo without Docker/Metabase.
The asyncio-native version replaces the old threading-based mock in server.py.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Dict, List

from backend.models import ErrorType, RolloutStatus
from backend.store import JobStore

logger = logging.getLogger(__name__)

_OUTCOMES = ["PASSED", "PASSED", "FAILED", "FAILED", "ERROR", "TIMEOUT"]
_WEIGHTS = [0.35, 0.25, 0.20, 0.12, 0.05, 0.03]


async def execute_mock_rollout(rollout_id: str, store: JobStore) -> None:
    """Simulate a rollout asynchronously with random outcome."""
    await store.update_rollout(
        rollout_id,
        status=RolloutStatus.STARTING,
        started_at=datetime.utcnow().isoformat(),
    )

    # Simulate environment start
    await asyncio.sleep(random.uniform(0.5, 1.5))

    await store.update_rollout(rollout_id, status=RolloutStatus.RUNNING)

    duration = random.uniform(5, 20)
    await asyncio.sleep(min(duration, 8))  # capped for demo speed

    steps = random.randint(4, 14)
    transcript: List[Dict[str, Any]] = []
    action_types = ["click", "type", "scroll", "navigate", "screenshot"]
    targets = ["Questions", "New", "Browse Data", "Orders", "Products", "Save", "Run Query"]
    for i in range(steps):
        etype = random.choice(action_types)
        if etype == "click":
            transcript.append({"type": "action", "action": "click", "target": random.choice(targets), "timestamp": f"T+{i*2}s"})
        elif etype == "type":
            transcript.append({"type": "action", "action": "type", "text": "SELECT * FROM products", "timestamp": f"T+{i*2}s"})
        elif etype == "navigate":
            transcript.append({"type": "action", "action": "navigate", "url": f"http://localhost:3000/question/{random.randint(1, 50)}", "timestamp": f"T+{i*2}s"})
        elif etype == "scroll":
            transcript.append({"type": "action", "action": "scroll", "direction": "down", "timestamp": f"T+{i*2}s"})
        else:
            transcript.append({"type": "screenshot", "timestamp": f"T+{i*2}s"})

    await store.update_rollout(rollout_id, status=RolloutStatus.GRADING)
    await asyncio.sleep(0.3)

    outcome_str = random.choices(_OUTCOMES, weights=_WEIGHTS, k=1)[0]
    outcome = RolloutStatus(outcome_str)

    grader_result: Dict[str, Any] = {
        "passed": outcome == RolloutStatus.PASSED,
        "reward": 1.0 if outcome == RolloutStatus.PASSED else 0.0,
        "reason": (
            "All fields matched." if outcome == RolloutStatus.PASSED
            else "Field mismatch: expected different values." if outcome == RolloutStatus.FAILED
            else "Evaluation infrastructure error." if outcome == RolloutStatus.ERROR
            else "Rollout exceeded time limit."
        ),
        "predicted": {"mock": True},
        "ground_truth": {"mock": True},
    }

    termination_map = {
        RolloutStatus.PASSED: "agent_completed_task_passed",
        RolloutStatus.FAILED: "agent_completed_task_failed",
        RolloutStatus.ERROR: "grader_exception",
        RolloutStatus.TIMEOUT: "max_duration_exceeded",
    }

    reward = grader_result["reward"] if outcome in (RolloutStatus.PASSED, RolloutStatus.FAILED) else None
    error_type = ErrorType.GRADER_ERROR if outcome == RolloutStatus.ERROR else (
        ErrorType.AGENT_TIMEOUT if outcome == RolloutStatus.TIMEOUT else None
    )

    rollout = await store.get_rollout(rollout_id)
    agent_claim = f"Mock claim for {rollout.task_id}: answer was computed." if rollout else ""

    await store.update_rollout(
        rollout_id,
        status=outcome,
        reward=reward,
        completed_at=datetime.utcnow().isoformat(),
        duration_seconds=round(duration, 2),
        agent_claim=agent_claim,
        termination_reason=termination_map.get(outcome, "unknown"),
        grader_result=grader_result,
        transcript=transcript,
        error_type=error_type,
    )
    logger.info(f"Mock rollout {rollout_id} completed → {outcome_str}")
