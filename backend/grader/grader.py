import json
import logging
from typing import Dict, Any, Optional
from .checks import extract_json_from_text, compare_answers

logger = logging.getLogger(__name__)

class TaskGrader:
    """Evaluates task execution results against benchmark ground truth."""

    def grade(self, expected_answer: Any, agent_claim: str, execution_error: Optional[str] = None) -> Dict[str, Any]:
        """Grade a task rollout."""
        if execution_error:
            return {
                "passed": False,
                "reward": 0.0,
                "reason": f"Execution error occurred: {execution_error}"
            }

        try:
            ground_truth = json.loads(expected_answer) if isinstance(expected_answer, str) else expected_answer
        except Exception:
            ground_truth = expected_answer

        predicted = extract_json_from_text(agent_claim)
        passed, reason = compare_answers(predicted, ground_truth)

        return {
            "passed": passed,
            "reward": 1.0 if passed else 0.0,
            "reason": reason,
            "predicted": predicted,
            "ground_truth": ground_truth
        }
