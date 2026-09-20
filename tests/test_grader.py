"""
Unit & Table-Driven Integration Test Suite for Task Grader & Evaluation Engine.
Verifies JSON extraction, parallel list row pairing, case-insensitive key matching,
scalar float/int equivalence, and evaluation against all 10 benchmark problems in tasks.json.
"""
import pytest
from backend.grader.checks import extract_json_from_text, compare_answers
from backend.grader.grader import TaskGrader


# ---------------------------------------------------------------------------
# 1. JSON Extraction Unit Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "input_text, expected_extracted",
    [
        (
            '{"ratings": [4.6, 5], "product_titles": ["A", "B"]}',
            {"ratings": [4.6, 5], "product_titles": ["A", "B"]},
        ),
        (
            'Here is the requested output:\n```json\n{\n  "avg_rating": 3.15\n}\n```\nHope this helps!',
            {"avg_rating": 3.15},
        ),
        (
            'Reasoning: I found the item.\nResult: {"person_name": "Keith Bradtke", "order_count": 46}',
            {"person_name": "Keith Bradtke", "order_count": 46},
        ),
        (
            '{"nested": {"key": [1, 2, 3]}}',
            {"nested": {"key": [1, 2, 3]}},
        ),
        (
            'No JSON contained in this text at all.',
            None,
        ),
    ],
)
def test_extract_json_from_text(input_text, expected_extracted):
    extracted = extract_json_from_text(input_text)
    assert extracted == expected_extracted


# ---------------------------------------------------------------------------
# 2. Table-Driven Answer Comparison Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case_id, predicted, ground_truth, expected_passed",
    [
        (
            "TC-01: Problem 1 exact match with inverted dict key order",
            {
                "product_titles": ["Rustic Paper Wallet", "Ergonomic Wool Bag", "Lightweight Linen Hat"],
                "ratings": [4.6, 5, 5]
            },
            {
                "ratings": [4.6, 5, 5],
                "product_titles": ["Rustic Paper Wallet", "Ergonomic Wool Bag", "Lightweight Linen Hat"]
            },
            True,
        ),
        (
            "TC-02: Problem 1 parallel list rating mismatch (Rustic Paper Wallet given 5 instead of 4.6)",
            {
                "product_titles": ["Rustic Paper Wallet", "Ergonomic Wool Bag", "Lightweight Linen Hat"],
                "ratings": [5, 5, 4.6]
            },
            {
                "ratings": [4.6, 5, 5],
                "product_titles": ["Rustic Paper Wallet", "Ergonomic Wool Bag", "Lightweight Linen Hat"]
            },
            False,
        ),
        (
            "TC-03: Problem 1 row-reordered parallel list with preserved row pairings",
            {
                "product_titles": ["Ergonomic Wool Bag", "Lightweight Linen Hat", "Rustic Paper Wallet"],
                "ratings": [5, 5, 4.6]
            },
            {
                "ratings": [4.6, 5, 5],
                "product_titles": ["Rustic Paper Wallet", "Ergonomic Wool Bag", "Lightweight Linen Hat"]
            },
            True,
        ),
        (
            "TC-04: Case-insensitive dictionary keys",
            {
                "PRODUCT_TITLES": ["Rustic Paper Wallet"],
                "RATINGS": [4.6]
            },
            {
                "product_titles": ["Rustic Paper Wallet"],
                "ratings": [4.6]
            },
            True,
        ),
        (
            "TC-05: Numeric string vs float/int equivalence",
            {"price": "19.87", "category": "Doohickey"},
            {"price": 19.87, "category": "Doohickey"},
            True,
        ),
        (
            "TC-06: Integer float equivalence (4 vs 4.0)",
            {"rating": "4", "full_name": "Chris Satterfield"},
            {"rating": 4.0, "full_name": "Chris Satterfield"},
            True,
        ),
        (
            "TC-07: Problem 2 ordered ranking mismatch",
            {
                "order_counts": [664, 700, 996],
                "product_titles": ["Incredible Bronze Wallet", "Awesome Plastic Watch", "Fantastic Rubber Knife"]
            },
            {
                "order_counts": [996, 700, 664],
                "product_titles": ["Fantastic Rubber Knife", "Awesome Plastic Watch", "Incredible Bronze Wallet"]
            },
            False,
        ),
        (
            "TC-08: Problem 9 single list set equivalence",
            {
                "names": ["Florida Hackett", "Alvis Emmerich", "Rupert Walsh", "Lilian Roberts"]
            },
            {
                "names": ["Alvis Emmerich", "Florida Hackett", "Lilian Roberts", "Rupert Walsh"]
            },
            True,
        ),
    ],
)
def test_compare_answers_table_driven(case_id, predicted, ground_truth, expected_passed):
    passed, reason = compare_answers(predicted, ground_truth)
    assert passed == expected_passed, f"[{case_id}] Expected passed={expected_passed}, got {passed}. Reason: {reason}"


# ---------------------------------------------------------------------------
# 3. Integration Tests with TaskGrader for all tasks in tasks.json
# ---------------------------------------------------------------------------

def test_task_grader_all_benchmark_problems():
    grader = TaskGrader("tasks.json")
    assert len(grader.tasks) == 10

    for task_id, task_data in grader.tasks.items():
        gt_answer = task_data["answer"]
        res = grader.grade(task_id, agent_claim=gt_answer)
        assert res["passed"] is True, f"Failed self-grading for task '{task_id}': {res['reason']}"
