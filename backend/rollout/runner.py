import sys
import os
import json
import uuid
import time
import argparse
import logging
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
from backend.rollout.models import RolloutResult
from backend.configs import (
    DEFAULT_TASKS_FILE,
    DEFAULT_METABASE_URL,
    DEFAULT_MODEL_NAME,
    DEFAULT_ROLLOUT_TIMEOUT,
    DEFAULT_ENV_TIMEOUT,
    ARTIFACTS_ROOT
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

def run_rollout(
    task_id: str,
    tasks_file: str = str(DEFAULT_TASKS_FILE),
    base_url: str = DEFAULT_METABASE_URL,
    model_name: str = DEFAULT_MODEL_NAME,
    keep_env: bool = False,
    timeout: int = DEFAULT_ROLLOUT_TIMEOUT
) -> RolloutResult:
    """Execute a single end-to-end benchmark rollout."""
    rollout_id = str(uuid.uuid4())[:8]
    artifacts_dir = Path(ARTIFACTS_ROOT) / rollout_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    grader = TaskGrader(tasks_file=tasks_file)
    task_data = grader.get_task(task_id)
    if not task_data:
        raise ValueError(f"Task ID '{task_id}' not found in {tasks_file}.")

    start_time = time.time()
    started_at = datetime.utcnow().isoformat()

    env_manager = EnvironmentManager(base_url=base_url)
    agent_claim = ""
    grader_res = {"passed": False, "reward": 0.0, "reason": "Not run"}
    status = "ERROR"
    termination_reason = "UNKNOWN"

    try:
        logger.info(f"--- Launching Rollout {rollout_id} for Task '{task_id}' ---")
        env_manager.start(clean=True)
        if not env_manager.wait_until_ready(timeout=180):
            status = "ERROR"
            termination_reason = "ENVIRONMENT_START_FAILURE"
            raise RuntimeError("Environment readiness check timed out.")

        logger.info("Environment ready. Executing agent...")
        agent_runner = AgentRunner(model_name=model_name)
        agent_out = agent_runner.run(task_prompt=task_data["task"], initial_url=base_url)

        agent_claim = agent_out.get("agent_claim", "")
        history = agent_out.get("history", [])

        with open(artifacts_dir / "transcript.json", "w", encoding="utf-8") as f:
            json.dump(history, f, default=str, indent=2)

        # Extract step-by-step screenshots
        screenshots = agent_out.get("screenshots", [])
        step_count = 0
        for action_name, img_data in screenshots:
            if img_data:
                step_count += 1
                img_filename = f"screenshot_{step_count:03d}_{action_name}.png"
                with open(artifacts_dir / img_filename, "wb") as img_f:
                    img_f.write(img_data)

        logger.info(f"Saved {step_count} step-by-step screenshots to {artifacts_dir}")

        logger.info("Agent execution completed. Running task grader...")
        grader_res = grader.grade(task_id=task_id, agent_claim=agent_claim)
        
        status = "PASS" if grader_res.get("passed") else "FAIL"
        termination_reason = grader_res.get("reason", "COMPLETED")

    except Exception as exc:
        logger.error(f"Rollout failed with exception: {exc}")
        if status == "ERROR" and termination_reason == "UNKNOWN":
            termination_reason = f"EXCEPTION: {str(exc)}"
        grader_res = grader.grade(task_id=task_id, agent_claim="", execution_error=str(exc))
    finally:
        finished_at = datetime.utcnow().isoformat()
        duration = round(time.time() - start_time, 2)

        if not keep_env:
            logger.info("Cleaning up environment...")
            try:
                env_manager.destroy()
            except Exception as e:
                logger.error(f"Environment cleanup error: {e}")

    result = RolloutResult(
        rollout_id=rollout_id,
        task_id=task_id,
        status=status,
        reward=grader_res.get("reward", 0.0),
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        agent_claim=agent_claim,
        grader_result=grader_res,
        termination_reason=termination_reason,
        artifacts_dir=str(artifacts_dir),
        metadata={
            "model": model_name,
            "metabase_url": base_url,
            "task_prompt": task_data["task"]
        }
    )

    with open(artifacts_dir / "result.json", "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2)

    logger.info(f"--- Rollout {rollout_id} Summary ---")
    logger.info(f"Status: {status} | Reward: {result.reward} | Duration: {duration}s")
    logger.info(f"Artifacts saved to: {artifacts_dir}")

    return result

def main():
    parser = argparse.ArgumentParser(description="Run a benchmark task rollout.")
    parser.add_argument("--task-id", type=str, required=True, help="Task ID from tasks.json (e.g., problem1)")
    parser.add_argument("--tasks", type=str, default="tasks.json", help="Path to tasks.json")
    parser.add_argument("--url", type=str, default="http://localhost:3000", help="Metabase base URL")
    parser.add_argument("--model", type=str, default="gemini-3-flash-preview", help="Gemini model")
    parser.add_argument("--keep-environment", action="store_true", help="Keep environment active after rollout")
    args = parser.parse_args()

    result = run_rollout(
        task_id=args.task_id,
        tasks_file=args.tasks,
        base_url=args.url,
        model_name=args.model,
        keep_env=args.keep_environment
    )

    print(json.dumps(result.to_dict(), indent=2))

if __name__ == "__main__":
    main()
