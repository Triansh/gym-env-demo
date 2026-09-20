import json
import re
import logging
from typing import Any, Tuple, Dict, List

logger = logging.getLogger(__name__)

def extract_json_from_text(text: str) -> Any:
    """Extract JSON object or array from agent claim text."""
    if not text or not isinstance(text, str):
        return None

    s = text.strip()

    # 1. Try direct parse
    try:
        return json.loads(s)
    except Exception:
        pass

    # 2. Find all ```json ... ``` or ``` ... ``` blocks (check in reverse)
    code_blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
    for block in reversed(code_blocks):
        try:
            return json.loads(block.strip())
        except Exception:
            pass

    # 3. Stream decoder finding JSON object ({...}) or array ([...])
    decoder = json.JSONDecoder()
    for i, char in enumerate(s):
        if char in ("{", "["):
            try:
                obj, _ = decoder.raw_decode(s[i:])
                return obj
            except Exception:
                pass

    # 4. Candidate regex match fallback
    candidate_matches = re.findall(
        r"(\{(?:[^{}]|(?:\{[^{}]*\}))*\})|(\[(?:[^\[\]]|(?:\[[^\[\]]*\]))*)",
        s,
        re.DOTALL
    )
    for m in reversed(candidate_matches):
        candidate_str = m[0] or m[1]
        if candidate_str:
            try:
                return json.loads(candidate_str.strip())
            except Exception:
                pass

    return None


def normalize_scalar(val: Any) -> Any:
    """Normalize scalar values (numbers, strings, booleans)."""
    if isinstance(val, bool):
        return val
    elif isinstance(val, (int, float)):
        f = float(val)
        return round(f, 4) if not f.is_integer() else int(f)
    elif isinstance(val, str):
        v = val.strip()
        try:
            f = float(v)
            return round(f, 4) if not f.is_integer() else int(f)
        except ValueError:
            return v.lower()
    return val


def is_parallel_dict(d: Dict[str, Any]) -> bool:
    """Check if dictionary contains parallel lists of equal length > 1."""
    if not isinstance(d, dict) or not d:
        return False
    values = list(d.values())
    if not all(isinstance(v, list) for v in values):
        return False
    lengths = [len(v) for v in values]
    return len(set(lengths)) == 1 and lengths[0] > 1


def transpose_parallel_dict(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Transpose dict of parallel lists into list of row dicts with normalized keys/values."""
    keys = list(d.keys())
    first_list = d[keys[0]]
    rows = []
    for idx in range(len(first_list)):
        row = {}
        for k in keys:
            norm_k = k.strip().lower()
            row[norm_k] = normalize_value(d[k][idx])
        rows.append(row)
    return rows


def normalize_value(val: Any) -> Any:
    """Recursively normalize data structure for value comparison."""
    if isinstance(val, dict):
        return {k.strip().lower(): normalize_value(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [normalize_value(x) for x in val]
    else:
        return normalize_scalar(val)


def compare_answers(predicted: Any, ground_truth: Any) -> Tuple[bool, str]:
    """Compare predicted structure against expected ground truth structure."""
    if predicted is None:
        return False, "Failed to extract valid JSON from agent response."

    # Check for parallel lists in dict structures (e.g. product_titles + ratings)
    if isinstance(ground_truth, dict) and isinstance(predicted, dict):
        # Case-insensitive key normalization check
        gt_keys = {k.strip().lower(): k for k in ground_truth.keys()}
        pred_keys = {k.strip().lower(): k for k in predicted.keys()}

        if gt_keys.keys() != pred_keys.keys():
            return (
                False,
                f"Dictionary keys mismatch. Predicted keys: {list(predicted.keys())}, "
                f"Expected keys: {list(ground_truth.keys())}"
            )

        if is_parallel_dict(ground_truth) and is_parallel_dict(predicted):
            gt_rows = transpose_parallel_dict(ground_truth)
            pred_rows = transpose_parallel_dict(predicted)

            # Check exact ordered row match first
            if pred_rows == gt_rows:
                return True, "Exact match with ground truth answer."

            # Check if ground truth list is strictly ordered (e.g. distinct numbers sorted descending/ascending like rankings)
            is_strictly_ordered = False
            for k, val_list in ground_truth.items():
                if isinstance(val_list, list) and len(val_list) > 1 and all(isinstance(x, (int, float)) for x in val_list):
                    # Strict monotonicity with distinct elements (e.g. top-K ranking)
                    if len(set(val_list)) == len(val_list) and (val_list == sorted(val_list) or val_list == sorted(val_list, reverse=True)):
                        is_strictly_ordered = True
                        break


            if not is_strictly_ordered and len(gt_rows) == len(pred_rows):
                unmatched_gt = list(gt_rows)
                for p_row in pred_rows:
                    if p_row in unmatched_gt:
                        unmatched_gt.remove(p_row)
                    else:
                        break
                if not unmatched_gt:
                    return True, "Match with ground truth answer (row set equivalence)."

            return (
                False,
                f"Parallel list row mismatch. Predicted rows: {pred_rows}, Expected rows: {gt_rows}"
            )

    # Standard normalization for scalar, list, and dict structures
    norm_pred = normalize_value(predicted)
    norm_gt = normalize_value(ground_truth)

    if norm_pred == norm_gt:
        return True, "Exact match with ground truth answer."

    # For simple single lists, check multiset (unordered set) equality if not strictly ordered
    if isinstance(norm_pred, list) and isinstance(norm_gt, list) and len(norm_pred) == len(norm_gt):
        unmatched = list(norm_gt)
        for item in norm_pred:
            if item in unmatched:
                unmatched.remove(item)
            else:
                break
        if not unmatched:
            return True, "Match with ground truth answer (unordered list equivalence)."

    return False, f"Value mismatch. Predicted: {predicted}, Expected: {ground_truth}"


