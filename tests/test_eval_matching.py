"""Tests for the grading rule itself.

These exist because the first version of this harness reported 3/8 when the
real number was 5/8. Two cases were marked wrong for returning the right rows
in a different order, on questions that never asked for an order. A grader that
can be wrong about the agent needs its own tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

from run_eval import matches  # noqa: E402


REF = [("gear", 58402.0), ("drinkware", 40527.0), ("home", 25769.0)]


def test_identical_results_match():
    assert matches(REF, list(REF), ordered=False)
    assert matches(REF, list(REF), ordered=True)


def test_row_order_is_ignored_when_the_question_did_not_ask_for_one():
    shuffled = [REF[2], REF[0], REF[1]]
    assert matches(REF, shuffled, ordered=False)


def test_row_order_is_enforced_when_the_question_asked_for_a_ranking():
    shuffled = [REF[2], REF[0], REF[1]]
    assert not matches(REF, shuffled, ordered=True)


def test_extra_columns_are_allowed():
    """Selecting id and subtotals alongside the answer is cosmetic."""
    wider = [
        (1, "gear", 60000.0, 1598.0, 58402.0),
        (2, "drinkware", 41000.0, 473.0, 40527.0),
        (3, "home", 26000.0, 231.0, 25769.0),
    ]
    assert matches(REF, wider, ordered=True)


def test_missing_values_do_not_match():
    wrong = [("gear", 58402.0), ("drinkware", 40527.0), ("home", 99999.0)]
    assert not matches(REF, wrong, ordered=False)


def test_a_different_number_of_rows_never_matches():
    assert not matches(REF, REF[:2], ordered=False)
    assert not matches(REF, REF + [("stationery", 1.0)], ordered=False)


def test_duplicated_rows_are_not_accepted_for_distinct_ones():
    """The real failure this caught: an agent that skipped GROUP BY and
    returned the same product once per order line."""
    dupes = [REF[0], REF[0], REF[0]]
    assert not matches(REF, dupes, ordered=False)


def test_unordered_matching_consumes_each_row_once():
    ref = [("a", 1), ("a", 1), ("b", 2)]
    assert matches(ref, [("b", 2), ("a", 1), ("a", 1)], ordered=False)
    assert not matches(ref, [("a", 1), ("a", 1), ("a", 1)], ordered=False)
