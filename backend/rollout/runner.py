"""
Rollout Runner — canonical single-rollout executor.
Called directly by workers.py for real (non-mock) runs.

Lifecycle:
  STARTING → environment start
  RUNNING  → agent execution
  GRADING  → task grading
  PASSED / FAILED / ERROR / TIMEOUT

Each rollout gets:
  - A unique docker-compose project name  (no container name conflicts)
  - A unique Metabase host port           (no port conflicts)
  - Its own artifact directory            artifacts/<job_id>/<task_id>/<attempt>/
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(override=True)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.environment.manager import EnvironmentManager
from backend.agent.runner import AgentRunner
from backend.grader.grader import TaskGrader
from backend.configs import (
    AGENT_TIMEOUT,
    ARTIFACTS_ROOT,
    DEFAULT_MODEL_NAME,
    DEFAULT_TASKS_FILE,
    ENVIRONMENT_START_TIMEOUT,
    GRADER_TIMEOUT,
    ROLLOUT_PORT_BASE,
)
from backend.models import ErrorType, RolloutStatus

logger = logging.getLogger(__name__)


def _port_for_slot(slot: int) -> tuple[int, int]:
    """Return (metabase_port, postgres_port) for a worker slot index."""
    return ROLLOUT_PORT_BASE + slot, ROLLOUT_PORT_BASE + 100 + slot


def execute_rollout(rollout_id: str, store, worker_slot: int = 0) -> None:
    """
    Execute a single rollout end-to-end (blocking — run in asyncio thread executor).
    Updates the store at each lifecycle stage so the API reflects live progress.

    worker_slot: integer index of the worker (0-based). Used to assign a unique
    Metabase port so parallel rollouts never conflict.
    """
    import asyncio, concurrent.futures

    # ---- helpers to update store from a thread ---------------------------
    def _update(**kwargs):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                store.update_rollout(rollout_id, **kwargs), loop
            )
            future.result(timeout=10)
        else:
            loop.run_until_complete(store.update_rollout(rollout_id, **kwargs))

    def _get_rollout():
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(store.get_rollout(rollout_id), loop)
            return future.result(timeout=5)
        else:
            return loop.run_until_complete(store.get_rollout(rollout_id))

    # ---- get rollout metadata --------------------------------------------
    rollout = _get_rollout()
    if rollout is None:
        logger.error(f"execute_rollout: rollout {rollout_id} not found")
        return

    job_id = rollout.job_id
    task_id = rollout.task_id
    attempt = rollout.attempt_number

    # ---- artifact directory: <job_id>/<task_id>/<attempt>/ --------------
    artifact_dir = Path(ARTIFACTS_ROOT) / job_id / task_id / str(attempt)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # ---- per-rollout file logger ----------------------------------------
    log_path = artifact_dir / "agent.log"
    log_handler = logging.FileHandler(str(log_path))
    log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

    start_time = time.time()
    started_at = datetime.utcnow().isoformat()

    # ---- per-rollout isolation ------------------------------------------
    project_name = f"mb_{rollout_id[:8]}"
    metabase_port, postgres_port = _port_for_slot(worker_slot)
    metabase_url = f"http://localhost:{metabase_port}"

    # primary error tracking (cleanup failure must not overwrite these)
    primary_error_type: ErrorType | None = None
    primary_error_message: str | None = None
    primary_error_stage: str | None = None
    cleanup_error: str | None = None

    status = RolloutStatus.ERROR
    reward = None
    termination_reason = "unknown"
    agent_claim = ""
    grader_result: dict = {}
    transcript = []
    screenshots = []
    env_manager = None

    try:
        _update(
            status=RolloutStatus.STARTING,
            started_at=started_at,
            artifact_path=str(artifact_dir),
        )

        # ---- load task --------------------------------------------------
        grader = TaskGrader(tasks_file=str(DEFAULT_TASKS_FILE))
        task_data = grader.get_task(task_id)
        if not task_data:
            raise RuntimeError(f"Task '{task_id}' not found in tasks.json")

        # ---- environment start ------------------------------------------
        env_manager = EnvironmentManager(
            base_url=metabase_url,
            project_name=project_name,
            metabase_port=metabase_port,
            postgres_port=postgres_port,
        )
        try:
            env_manager.start(clean=True)
        except Exception as e:
            primary_error_type = ErrorType.ENVIRONMENT_START_ERROR
            primary_error_stage = "environment_start"
            primary_error_message = str(e)
            logger.error(f"[{project_name}] Env start failed: {e}\n{traceback.format_exc()}")
            raise

        if not env_manager.wait_until_ready(timeout=ENVIRONMENT_START_TIMEOUT):
            primary_error_type = ErrorType.ENVIRONMENT_HEALTH_TIMEOUT
            primary_error_stage = "environment_health"
            primary_error_message = f"Metabase didn't become healthy within {ENVIRONMENT_START_TIMEOUT}s"
            raise RuntimeError(primary_error_message)

        # ---- agent execution --------------------------------------------
        _update(status=RolloutStatus.RUNNING)
        agent_runner = AgentRunner(model_name=DEFAULT_MODEL_NAME)

        try:
            with ThreadPoolExecutor(max_workers=1) as tpe:
                fut = tpe.submit(agent_runner.run, task_data["task"], metabase_url)
                try:
                    agent_out = fut.result(timeout=AGENT_TIMEOUT)
                except FuturesTimeout:
                    fut.cancel()
                    primary_error_type = ErrorType.AGENT_TIMEOUT
                    primary_error_stage = "agent_execution"
                    primary_error_message = f"Agent exceeded {AGENT_TIMEOUT}s"
                    status = RolloutStatus.TIMEOUT
                    termination_reason = "agent_timeout"
                    raise TimeoutError(primary_error_message)
        except TimeoutError:
            raise
        except Exception as e:
            if primary_error_type is None:
                primary_error_type = ErrorType.AGENT_ERROR
                primary_error_stage = "agent_execution"
                primary_error_message = str(e)
            logger.error(f"[{project_name}] Agent error: {e}\n{traceback.format_exc()}")
            raise

        agent_claim = agent_out.get("agent_claim", "")
        history = agent_out.get("history", [])
        step_screenshots = agent_out.get("screenshots", [])

        with open(artifact_dir / "transcript.json", "w", encoding="utf-8") as f:
            json.dump(history, f, default=str, indent=2)
        transcript = history

        shot_dir = artifact_dir / "screenshots"
        shot_dir.mkdir(exist_ok=True)
        shot_names = []
        for idx, (action_name, img_data) in enumerate(step_screenshots, 1):
            if img_data:
                name = f"{idx:03d}_{action_name}.png"
                with open(shot_dir / name, "wb") as f:
                    f.write(img_data)
                shot_names.append(name)
        screenshots = shot_names

        # ---- grading ----------------------------------------------------
        _update(status=RolloutStatus.GRADING)
        try:
            with ThreadPoolExecutor(max_workers=1) as tpe:
                fut = tpe.submit(grader.grade, task_id, agent_claim)
                try:
                    grader_result = fut.result(timeout=GRADER_TIMEOUT)
                except FuturesTimeout:
                    primary_error_type = ErrorType.GRADER_ERROR
                    primary_error_stage = "grading"
                    primary_error_message = f"Grader exceeded {GRADER_TIMEOUT}s"
                    raise TimeoutError(primary_error_message)
        except TimeoutError:
            raise
        except Exception as e:
            primary_error_type = ErrorType.GRADER_ERROR
            primary_error_stage = "grading"
            primary_error_message = str(e)
            logger.error(f"[{project_name}] Grader error: {e}\n{traceback.format_exc()}")
            raise

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
        logger.error(f"[{project_name}] Rollout failed at '{primary_error_stage}': {exc}\n{traceback.format_exc()}")

    finally:
        duration = round(time.time() - start_time, 2)
        completed_at = datetime.utcnow().isoformat()

        if env_manager is not None:
            try:
                env_manager.destroy()
            except Exception as ce:
                cleanup_error = str(ce)
                logger.error(f"[{project_name}] Cleanup error: {ce}")

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
            logger.error(f"Could not write result.json: {we}")

        _update(
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

        try:
            root_logger.removeHandler(log_handler)
            log_handler.close()
        except Exception:
            pass

    logger.info(
        f"[{project_name}] {task_id} attempt {attempt} → {status} | reward={reward} | {duration}s"
    )
