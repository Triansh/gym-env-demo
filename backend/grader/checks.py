import json
import re
import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)

def extract_json_from_text(text: str) -> Any:
    """Extract JSON object or array from agent claim text."""
    if not text:
        return None
    
    # Try direct parse
    try:
        return json.loads(text.strip())
    except Exception:
        pass

    # Find all ```json ... ``` or ``` ... ``` blocks (search in reverse for the final output block)
    code_blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    for block in reversed(code_blocks):
        try:
            return json.loads(block.strip())
        except Exception:
            pass

    # Find candidate JSON objects or arrays
    candidate_matches = re.findall(r"(\{(?:[^{}]|(?:\{[^{}]*\}))*\})|(\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*\])", text, re.DOTALL)
    for m in reversed(candidate_matches):
        candidate_str = m[0] or m[1]
        if candidate_str:
            try:
                return json.loads(candidate_str.strip())
            except Exception:
                pass

    return None

def normalize_value(val: Any) -> Any:
    """Recursively normalize values (convert numeric strings to float/int, sort list items if un-ordered)."""
    if isinstance(val, dict):
        return {k: normalize_value(v) for k, v in val.items()}
    elif isinstance(val, list):
        norm_list = [normalize_value(x) for x in val]
        # Attempt sorting lists of primitives for order-agnostic evaluation
        try:
            return sorted(norm_list)
        except Exception:
            return norm_list
    elif isinstance(val, (int, float)):
        return round(float(val), 4)
    elif isinstance(val, str):
        try:
            f = float(val)
            return round(f, 4) if not f.is_integer() else int(f)
        except ValueError:
            return val.strip().lower()
    return val

def compare_answers(predicted: Any, ground_truth: Any) -> Tuple[bool, str]:
    """Compare predicted structure against expected ground truth structure."""
    if predicted is None:
        return False, "Failed to extract valid JSON from agent response."

    norm_pred = normalize_value(predicted)
    norm_gt = normalize_value(ground_truth)

    if norm_pred == norm_gt:
        return True, "Exact match with ground truth answer."
    else:
        return False, f"Value mismatch. Predicted: {predicted}, Expected: {ground_truth}"
