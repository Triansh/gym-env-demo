import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from .checks import extract_json_from_text, compare_answers

logger = logging.getLogger(__name__)

class TaskGrader:
    """Evaluates task execution results against benchmark ground truth tasks.json."""

    def __init__(self, tasks_file: str = "tasks.json"):
        self.tasks_path = Path(tasks_file).resolve()
        self.tasks = self._load_tasks()

    def _load_tasks(self) -> Dict[str, Dict[str, Any]]:
        if not self.tasks_path.exists():
            raise FileNotFoundError(f"Tasks file not found: {self.tasks_path}")
        with open(self.tasks_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {item["id"]: item for item in data}

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.tasks.get(task_id)

    def grade(self, task_id: str, agent_claim: str, execution_error: Optional[str] = None) -> Dict[str, Any]:
        """Grade a task rollout."""
        task_data = self.get_task(task_id)
        if not task_data:
            return {
                "passed": False,
                "reward": 0.0,
                "reason": f"Task ID '{task_id}' not found in benchmark tasks."
            }

        if execution_error:
            return {
                "passed": False,
                "reward": 0.0,
                "reason": f"Execution error occurred: {execution_error}"
            }

        gt_raw = task_data.get("answer")
        try:
            ground_truth = json.loads(gt_raw) if isinstance(gt_raw, str) else gt_raw
        except Exception:
            ground_truth = gt_raw

        predicted = extract_json_from_text(agent_claim)
        passed, reason = compare_answers(predicted, ground_truth)

        return {
            "passed": passed,
            "reward": 1.0 if passed else 0.0,
            "reason": reason,
            "predicted": predicted,
            "ground_truth": ground_truth
        }
