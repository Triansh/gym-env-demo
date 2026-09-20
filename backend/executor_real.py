"""
Milestone 2 Real Rollout Executor.
Runs a single rollout end-to-end: environment → agent → grader → artifacts.
Designed to be called from a worker thread via asyncio run_in_executor.

Key properties:
- Every stage transitions rollout status in the store
- try/finally guarantees environment cleanup
- Cleanup failures are recorded but NEVER replace the original error_type
- Full tracebacks go to agent.log, not the frontend
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from backend.configs import (
    AGENT_TIMEOUT,
    ARTIFACTS_ROOT,
    DEFAULT_METABASE_URL,
    DEFAULT_MODEL_NAME,
    DEFAULT_TASKS_FILE,
    ENVIRONMENT_START_TIMEOUT,
    GRADER_TIMEOUT,
)
from backend.models import ErrorType, RolloutStatus
from backend.store import JobStore

logger = logging.getLogger(__name__)


def _update_rollout_sync(store: JobStore, rollout_id: str, **kwargs) -> None:
    """Synchronous bridge to update rollout in the asyncio store from a thread."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        # We're already in an event loop (shouldn't happen for blocking executor)
        import concurrent.futures
        future = asyncio.run_coroutine_threadsafe(
            store.update_rollout(rollout_id, **kwargs), loop
        )
        future.result(timeout=10)
    else:
        loop.run_until_complete(store.update_rollout(rollout_id, **kwargs))


def _get_rollout_sync(store: JobStore, rollout_id: str):
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        import concurrent.futures
        future = asyncio.run_coroutine_threadsafe(store.get_rollout(rollout_id), loop)
        return future.result(timeout=5)
    else:
        return loop.run_until_complete(store.get_rollout(rollout_id))


def execute_rollout(rollout_id: str, store: JobStore) -> None:
    """
    Execute a single rollout end-to-end (blocking — run in a thread executor).

    Status lifecycle:
      QUEUED → STARTING → RUNNING → GRADING → PASSED / FAILED / ERROR / TIMEOUT
    """
    rollout = _get_rollout_sync(store, rollout_id)
    if rollout is None:
        logger.error(f"execute_rollout: rollout {rollout_id} not found")
        return

    job_id = rollout.job_id
    task_id = rollout.task_id
    attempt = rollout.attempt_number

    artifact_dir = Path(ARTIFACTS_ROOT) / job_id / rollout_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    log_path = artifact_dir / "agent.log"
    log_handler = logging.FileHandler(str(log_path))
    log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

    start_time = time.time()
    started_at = datetime.utcnow().isoformat()

    # These track the "primary" error — cleanup failures must not overwrite them
    primary_error_type: ErrorType | None = None
    primary_error_message: str | None = None
    primary_error_stage: str | None = None
    cleanup_error: str | None = None

    status = RolloutStatus.ERROR
    reward: float | None = None
    termination_reason = "unknown"
    agent_claim = ""
    grader_result: Dict[str, Any] = {}
    transcript = []
    screenshots = []

    env_manager = None

    try:
        # ---- STAGE: Load task -------------------------------------------
        _update_rollout_sync(store, rollout_id,
                             status=RolloutStatus.STARTING,
                             started_at=started_at,
                             artifact_path=str(artifact_dir))

        from backend.grader.grader import TaskGrader
        grader = TaskGrader(tasks_file=str(DEFAULT_TASKS_FILE))
        task_data = grader.get_task(task_id)
        if not task_data:
            raise RuntimeError(f"Task '{task_id}' not found in tasks.json")

        # ---- STAGE: Environment start ------------------------------------
        from backend.environment.manager import EnvironmentManager
        env_manager = EnvironmentManager(base_url=DEFAULT_METABASE_URL)

        try:
            env_manager.start(clean=True)
        except Exception as e:
            primary_error_type = ErrorType.ENVIRONMENT_START_ERROR
            primary_error_stage = "environment_start"
            primary_error_message = str(e)
            logger.error(f"Environment start failed: {e}\n{traceback.format_exc()}")
            raise

        if not env_manager.wait_until_ready(timeout=ENVIRONMENT_START_TIMEOUT):
            primary_error_type = ErrorType.ENVIRONMENT_HEALTH_TIMEOUT
            primary_error_stage = "environment_health"
            primary_error_message = f"Metabase did not become healthy within {ENVIRONMENT_START_TIMEOUT}s"
            raise RuntimeError(primary_error_message)

        # ---- STAGE: Agent execution --------------------------------------
        _update_rollout_sync(store, rollout_id, status=RolloutStatus.RUNNING)

        from backend.agent.runner import AgentRunner
        agent_runner = AgentRunner(model_name=DEFAULT_MODEL_NAME)

        try:
            with ThreadPoolExecutor(max_workers=1) as tpe:
                future = tpe.submit(
                    agent_runner.run,
                    task_data["task"],
                    DEFAULT_METABASE_URL,
                )
                try:
                    agent_out = future.result(timeout=AGENT_TIMEOUT)
                except FuturesTimeout:
                    future.cancel()
                    primary_error_type = ErrorType.AGENT_TIMEOUT
                    primary_error_stage = "agent_execution"
                    primary_error_message = f"Agent exceeded {AGENT_TIMEOUT}s timeout"
                    status = RolloutStatus.TIMEOUT
                    termination_reason = "agent_timeout"
                    raise TimeoutError(primary_error_message)

            agent_claim = agent_out.get("agent_claim", "")
            history = agent_out.get("history", [])
            step_screenshots = agent_out.get("screenshots", [])

            # Save transcript
            transcript_path = artifact_dir / "transcript.json"
            with open(transcript_path, "w", encoding="utf-8") as f:
                json.dump(history, f, default=str, indent=2)
            transcript = history

            # Save screenshots
            shot_dir = artifact_dir / "screenshots"
            shot_dir.mkdir(exist_ok=True)
            shot_names = []
            for idx, (action_name, img_data) in enumerate(step_screenshots, 1):
                if img_data:
                    name = f"screenshot_{idx:03d}_{action_name}.png"
                    with open(shot_dir / name, "wb") as f:
                        f.write(img_data)
                    shot_names.append(name)
            screenshots = shot_names

        except TimeoutError:
            raise
        except Exception as e:
            if primary_error_type is None:
                primary_error_type = ErrorType.AGENT_ERROR
                primary_error_stage = "agent_execution"
                primary_error_message = str(e)
            logger.error(f"Agent error: {e}\n{traceback.format_exc()}")
            raise

        # ---- STAGE: Grading ---------------------------------------------
        _update_rollout_sync(store, rollout_id, status=RolloutStatus.GRADING)

        try:
            with ThreadPoolExecutor(max_workers=1) as tpe:
                future = tpe.submit(grader.grade, task_id, agent_claim)
                try:
                    grader_result = future.result(timeout=GRADER_TIMEOUT)
                except FuturesTimeout:
                    primary_error_type = ErrorType.GRADER_ERROR
                    primary_error_stage = "grading"
                    primary_error_message = f"Grader exceeded {GRADER_TIMEOUT}s timeout"
                    raise TimeoutError(primary_error_message)
        except TimeoutError:
            raise
        except Exception as e:
            primary_error_type = ErrorType.GRADER_ERROR
            primary_error_stage = "grading"
            primary_error_message = str(e)
            logger.error(f"Grader error: {e}\n{traceback.format_exc()}")
            raise

        # Save grader output
        with open(artifact_dir / "grader.json", "w", encoding="utf-8") as f:
            json.dump(grader_result, f, indent=2)

        passed = grader_result.get("passed", False)
        status = RolloutStatus.PASSED if passed else RolloutStatus.FAILED
        reward = grader_result.get("reward", 0.0)
        termination_reason = "task_passed" if passed else "task_failed"
        if not passed:
            primary_error_type = ErrorType.TASK_FAILED

    except Exception as exc:
        if primary_error_type is None:
            primary_error_type = ErrorType.AGENT_ERROR
            primary_error_stage = "unknown"
            primary_error_message = str(exc)
        logger.error(f"Rollout {rollout_id} failed at stage '{primary_error_stage}': {exc}\n{traceback.format_exc()}")

    finally:
        duration = round(time.time() - start_time, 2)
        completed_at = datetime.utcnow().isoformat()

        # Always try to destroy environment
        if env_manager is not None:
            try:
                env_manager.destroy()
            except Exception as ce:
                cleanup_error = str(ce)
                logger.error(f"Cleanup error for rollout {rollout_id}: {ce}")
                # cleanup_error is stored separately; primary error_type is NOT overwritten

        # Write result.json
        result_dict = {
            "rollout_id": rollout_id,
            "job_id": job_id,
            "task_id": task_id,
            "attempt_number": attempt,
            "status": str(status),
            "reward": reward,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": duration,
            "termination_reason": termination_reason,
            "error_type": str(primary_error_type) if primary_error_type else None,
            "error_message": primary_error_message,
            "error_stage": primary_error_stage,
            "cleanup_error": cleanup_error,
            "artifact_path": str(artifact_dir),
        }
        try:
            with open(artifact_dir / "result.json", "w", encoding="utf-8") as f:
                json.dump(result_dict, f, indent=2)
        except Exception as we:
            logger.error(f"Could not write result.json for rollout {rollout_id}: {we}")

        # Final store update
        _update_rollout_sync(
            store, rollout_id,
            status=status,
            reward=reward,
            completed_at=completed_at,
            duration_seconds=duration,
            termination_reason=termination_reason,
            error_type=primary_error_type,
            error_message=primary_error_message,
            error_stage=primary_error_stage,
            cleanup_error=cleanup_error,
            transcript=transcript,
            grader_result=grader_result,
            artifact_path=str(artifact_dir),
            screenshots=screenshots,
            agent_claim=agent_claim,
        )

        # Remove file handler
        try:
            root_logger.removeHandler(log_handler)
            log_handler.close()
        except Exception:
            pass

    logger.info(f"Rollout {rollout_id} ({task_id} attempt {attempt}) → {status} | reward={reward} | {duration}s")
