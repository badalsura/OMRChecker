"""Correcting or accepting a custom label settles every column it covers, so
a flagged column whose digit stays does not keep its group's issue mark."""

from src.api.review import apply_review
from src.utils.parsing import column_state


def roll_result():
    def column(value, flagged):
        return {
            "value": value,
            "flags": ["low_confidence"] if flagged else [],
            "needs_review": flagged,
            "bubbles": [{"value": str(d)} for d in range(10)],
        }

    fields = {f"roll{i}": column(v, i <= 2) for i, v in enumerate("9931", 1)}
    return {
        "scan_id": "s",
        "fields": fields,
        "responses": {"roll": "  31"},
        "review": [{"name": "roll1"}, {"name": "roll2"}],
    }


def test_correcting_a_label_to_its_own_digits_clears_the_issue():
    result = roll_result()
    labels = {"roll": [f"roll{i}" for i in range(1, 5)]}
    resolved, events = apply_review(result, {"roll": "9931"}, [], "asha", {}, labels)
    fields = result["fields"]
    assert all(
        fields[c]["reviewed"] and not fields[c]["needs_review"] for c in labels["roll"]
    )
    assert [column_state(fields[c]["value"], fields[c]) for c in labels["roll"]] == [
        "ok"
    ] * 4
    assert {"roll", "roll1", "roll2"} <= set(resolved) and result["review"] == []
    # The label itself is the correction; unchanged columns are accepted
    actions = {e[1]: e[4] for e in events}
    assert actions["roll"] == "corrected" and actions["roll1"] == "accepted"


def test_a_label_correction_still_changes_the_columns_that_differ():
    result = roll_result()
    labels = {"roll": [f"roll{i}" for i in range(1, 5)]}
    apply_review(result, {"roll": "1931"}, [], "asha", {}, labels)
    assert result["fields"]["roll1"]["value"] == "1"
    assert result["fields"]["roll1"]["original_value"] == "9"
