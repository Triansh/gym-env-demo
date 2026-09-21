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

def _run_agent_in_process(q, task_prompt: str, metabase_url: str, model_name: str, screen_size: tuple):
    """Executes the agent in a dedicated process and returns strictly JSON-picklable parsed data."""
    try:
        from backend.agent.runner import AgentRunner
        import traceback
        agent_runner = AgentRunner(model_name=model_name)
        agent_out = agent_runner.run(task_prompt, metabase_url, screen_size)
        
        history = agent_out.get("history", [])
        step_screenshots = agent_out.get("screenshots", [])
        agent_claim = agent_out.get("agent_claim", "")
        
        # Serialize history BEFORE putting it in the inter-process queue
        # format_history_steps strips out complex objects (e.g. from Google GenAI)
        steps = format_history_steps(history, step_screenshots)
        
        q.put({
            "success": True,
            "agent_claim": agent_claim,
            "steps": steps,
            "step_screenshots": step_screenshots
        })
    except Exception as e:
        import traceback
        q.put({
            "success": False, 
            "error": str(e),
            "traceback": traceback.format_exc()
        })


def format_history_steps(history, step_screenshots):
    """
    Parses raw agent execution history into a list of structured step objects.
    Each step matches 1-to-1 with a step screenshot.
    """
    steps = []
    current_step = None
    step_counter = 0

    for item in history:
        role = getattr(item, 'role', '')
        parts = getattr(item, 'parts', [])
        if isinstance(item, dict):
            role = item.get('role', '')
            parts = item.get('parts', [])

        if role == 'model':
            thought = ""
            function_calls = []

            for part in parts:
                p_text = getattr(part, 'text', '') if not isinstance(part, dict) else part.get('text', '')
                p_fc = getattr(part, 'function_call', None) if not isinstance(part, dict) else part.get('function_call', None)

                if p_text:
                    thought += str(p_text) + "\n"
                if p_fc:
                    if isinstance(p_fc, dict):
                        fc_name = p_fc.get('name', '')
                        fc_args = p_fc.get('args', {})
                    else:
                        fc_name = getattr(p_fc, 'name', '')
                        fc_args = getattr(p_fc, 'args', {})
                        if hasattr(fc_args, 'to_dict'):
                            fc_args = fc_args.to_dict()
                        elif not isinstance(fc_args, dict):
                            fc_args = dict(fc_args) if fc_args else {}
                    function_calls.append({'name': fc_name, 'args': fc_args})

            for fc in function_calls:
                step_counter += 1
                shot_name = f"{step_counter:03d}_{fc['name']}.png" if step_counter <= len(step_screenshots) else None
                current_step = {
                    "step_number": step_counter,
                    "action": fc['name'],
                    "args": fc['args'],
                    "thought": thought.strip(),
                    "url": None,
                    "screenshot": shot_name
                }
                steps.append(current_step)

        elif role == 'user' and current_step is not None:
            for part in parts:
                p_fr = getattr(part, 'function_response', None) if not isinstance(part, dict) else part.get('function_response', None)
                if p_fr:
                    resp = getattr(p_fr, 'response', {}) if not isinstance(p_fr, dict) else p_fr.get('response', {})
                    if isinstance(resp, dict) and 'url' in resp:
                        current_step['url'] = resp['url']

    return steps


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
        store.update_rollout(rollout_id, **kwargs)

    def _get_rollout():
        return store.get_rollout(rollout_id)

    # ---- get rollout metadata --------------------------------------------
    rollout = _get_rollout()
    if rollout is None:
        logger.error(f"execute_rollout: rollout {rollout_id} not found")
        return

    job_id = rollout.job_id
    task_id = rollout.task_id
    attempt = rollout.attempt_number

    # ---- artifact directory: <job_id>/<rollout_id>/ --------------
    artifact_dir = Path(ARTIFACTS_ROOT) / job_id / rollout_id
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
        job = store.get_job(job_id)
        if not job:
            raise RuntimeError(f"Job '{job_id}' not found in database")
        
        task_data = None
        for t in job.tasks:
            if t.id == task_id:
                task_data = {"task": t.prompt, "answer": t.expected_answer}
                break
        
        if not task_data:
            raise RuntimeError(f"Task '{task_id}' not found in Job '{job_id}'")

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

        import multiprocessing
        import queue
        from backend.configs import DEFAULT_MODEL_NAME, DEFAULT_SCREEN_SIZE
        
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        
        p = ctx.Process(
            target=_run_agent_in_process, 
            args=(q, task_data["task"], metabase_url, DEFAULT_MODEL_NAME, DEFAULT_SCREEN_SIZE)
        )
        p.start()

        try:
            agent_out_data = q.get(timeout=AGENT_TIMEOUT)
            if not agent_out_data.get("success"):
                primary_error_type = ErrorType.AGENT_ERROR
                primary_error_stage = "agent_execution"
                primary_error_message = agent_out_data.get("error", "Unknown error")
                logger.error(f"[{project_name}] Agent error from process: {primary_error_message}\n{agent_out_data.get('traceback', '')}")
                raise RuntimeError(primary_error_message)
        except queue.Empty:
            # Cancel the process and guarantee all Chrome handles die
            p.terminate()
            p.join(timeout=2)
            if p.is_alive():
                p.kill()
            
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
            logger.error(f"[{project_name}] Agent queue get error: {e}\n{traceback.format_exc()}")
            raise

        agent_claim = agent_out_data.get("agent_claim", "")
        step_screenshots = agent_out_data.get("step_screenshots", [])
        formatted_steps = agent_out_data.get("steps", [])

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

        transcript_data = formatted_steps

        with open(artifact_dir / "transcript.json", "w", encoding="utf-8") as f:
            json.dump(transcript_data, f, default=str, indent=2)
        transcript = transcript_data

        # ---- grading ----------------------------------------------------
        _update(status=RolloutStatus.GRADING)
        try:
            grader = TaskGrader()
            with ThreadPoolExecutor(max_workers=1) as tpe:
                fut = tpe.submit(grader.grade, task_data["answer"], agent_claim)
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
        if status not in (RolloutStatus.TIMEOUT, RolloutStatus.ERROR):
            status = RolloutStatus.ERROR
        if primary_error_type is None:
            primary_error_type = ErrorType.AGENT_ERROR
        if not primary_error_stage or primary_error_stage == "unknown":
            primary_error_stage = "agent_execution"
        if termination_reason == "unknown":
            if primary_error_type in (ErrorType.ENVIRONMENT_START_ERROR, ErrorType.ENVIRONMENT_HEALTH_TIMEOUT):
                termination_reason = "environment_failure"
            elif primary_error_type in (ErrorType.AGENT_TIMEOUT, ErrorType.ENVIRONMENT_HEALTH_TIMEOUT) or status == RolloutStatus.TIMEOUT:
                termination_reason = "agent_timeout"
            elif primary_error_type == ErrorType.AGENT_ERROR:
                termination_reason = "agent_execution_failed"
            elif primary_error_type == ErrorType.GRADER_ERROR:
                termination_reason = "grader_failure"
            else:
                termination_reason = "execution_error"
        if primary_error_message is None:
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

        status_str = status.value if hasattr(status, "value") else str(status)
        error_type_str = primary_error_type.value if hasattr(primary_error_type, "value") else (str(primary_error_type) if primary_error_type else None)

        result_dict = {
            "rollout_id": rollout_id,
            "job_id": job_id,
            "task_id": task_id,
            "attempt_number": attempt,
            "status": status_str,
            "reward": reward,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": duration,
            "termination_reason": termination_reason,
            "error_type": error_type_str,
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

        # ----- Close per-rollout file handler FIRST, before _update() -------
        # This guarantees the FileHandler FD is released even if _update() raises
        # (e.g. when the process is already at the OS open-file limit).  A leaked
        # handler attached to the root logger would otherwise accumulate across
        # rollouts and eventually exhaust the file descriptor table.
        try:
            root_logger.removeHandler(log_handler)
            log_handler.close()
        except Exception:
            pass

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

    logger.info(
        f"[{project_name}] {task_id} attempt {attempt} → {status} | reward={reward} | {duration}s"
    )
