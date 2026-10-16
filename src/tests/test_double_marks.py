"""Optional double-mark rules (review_params keep_darker_gap / strict_double_mark)."""

from src.core import ImageInstanceOps
from src.utils.parsing import Settings


def resolve(fills, marked, **params):
    details = [{"fill_ratio": f, "marked": bool(m)} for f, m in zip(fills, marked)]
    bubbles = list("ABCD")
    detected = [b for b, m in zip(bubbles, marked) if m]
    flags = ImageInstanceOps.resolve_double_marks(
        bubbles, details, detected, Settings(params)
    )
    return detected, flags


def test_off_by_default():
    assert resolve([0.9, 0.4, 0, 0], [1, 1, 0, 0]) == (["A", "B"], [])
    assert resolve([0.9, 0.4, 0, 0], [1, 0, 0, 0]) == (["A"], [])


def test_keep_darker_only_when_clearly_ahead():
    assert resolve([0.9, 0.55, 0, 0], [1, 1, 0, 0], keep_darker_gap=0.3) == (
        ["A"],
        ["kept_darker_mark"],
    )
    assert resolve([0.9, 0.7, 0, 0], [1, 1, 0, 0], keep_darker_gap=0.3) == (
        ["A", "B"],
        [],
    )


def test_strict_turns_faint_second_mark_into_multi():
    assert resolve([0.32, 0.9, 0, 0], [0, 1, 0, 0], strict_double_mark=0.3) == (
        ["A", "B"],
        ["faint_second_mark"],
    )
    # Nothing marked: strict does not invent an answer
    assert resolve([0.32, 0.1, 0, 0], [0, 0, 0, 0], strict_double_mark=0.3) == ([], [])


def test_keep_darker_settles_the_field_before_strict():
    assert resolve(
        [0.9, 0.55, 0.31, 0], [1, 1, 0, 0], keep_darker_gap=0.3, strict_double_mark=0.3
    ) == (["A"], ["kept_darker_mark"])
