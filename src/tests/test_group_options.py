"""Per-group placeholders (template "groupOptions", src/utils/parsing.py)."""

import copy
import json
from pathlib import Path

import pytest

from src.pipeline import OMREngine
from src.rules import reapply_rules
from src.utils.parsing import (
    column_state,
    describe_groups,
    get_concatenated_response,
    group_review_items,
    join_group,
)

TEMPLATE = {
    "pageDimensions": [800, 600],
    "bubbleDimensions": [20, 20],
    "preProcessors": [],
    "fieldBlocks": {
        "Roll": {
            "fieldType": "QTYPE_INT",
            "fieldLabels": ["roll1..6"],
            "origin": [50, 50],
            "bubblesGap": 30,
            "labelsGap": 30,
        },
    },
    "customLabels": {"Roll": ["roll1..6"]},
}


def make_engine(tmp_path, **extra):
    template = copy.deepcopy(TEMPLATE)
    template.update(extra)
    path = Path(tmp_path, "template.json")
    path.write_text(json.dumps(template))
    return OMREngine(path)


def col(value, flags=None, review=False, reviewed=False):
    flags = list(flags if flags is not None else ([] if value else ["empty"]))
    details = {"value": value, "flags": flags, "needs_review": review}
    if reviewed:
        details["reviewed"] = True
    return details


# 0, empty, multi-marked "12", low confidence 4, 5, 6
COLUMNS = [
    col("0"),
    col(""),
    col("12", ["multi_marked"], review=True),
    col("4", ["low_confidence"], review=True),
    col("5"),
    col("6"),
]


def read(columns):
    fields = {f"roll{i + 1}": c for i, c in enumerate(columns)}
    omr = {name: f["value"] for name, f in fields.items()}
    return omr, fields


def test_old_templates_keep_the_plain_join(tmp_path):
    engine = make_engine(tmp_path)
    omr, fields = read(COLUMNS)
    assert engine.template.group_options == {}
    # Exactly today's output, with or without per-column details
    assert get_concatenated_response(omr, engine.template)["Roll"] == "012456"
    assert get_concatenated_response(omr, engine.template, fields)["Roll"] == "012456"
    assert describe_groups(omr, engine.template, fields) == {}


def test_defaults_give_one_character_per_column(tmp_path):
    engine = make_engine(tmp_path, groupOptions={"Roll": {}})
    omr, fields = read(COLUMNS)
    value = get_concatenated_response(omr, engine.template, fields)["Roll"]
    assert value == "0 *-56"
    assert len(value) == 6
    groups = describe_groups(omr, engine.template, fields)
    assert groups["Roll"]["value"] == value
    assert groups["Roll"]["flagged"] is True
    assert [c["state"] for c in groups["Roll"]["columns"]] == [
        "ok",
        "empty",
        "multi",
        "issue",
        "ok",
        "ok",
    ]


def test_empty_placeholder_or_skip(tmp_path):
    omr, fields = read(COLUMNS)
    engine = make_engine(tmp_path, groupOptions={"Roll": {"empty": "_"}})
    assert get_concatenated_response(omr, engine.template, fields)["Roll"] == "0_*-56"
    engine = make_engine(tmp_path, groupOptions={"Roll": {"empty": None}})
    assert get_concatenated_response(omr, engine.template, fields)["Roll"] == "0*-56"


def test_custom_multi_and_issue_characters(tmp_path):
    omr, fields = read(COLUMNS)
    engine = make_engine(
        tmp_path, groupOptions={"Roll": {"empty": "0", "multi": "M", "issue": "?"}}
    )
    assert get_concatenated_response(omr, engine.template, fields)["Roll"] == "00M?56"


def test_without_details_only_blanks_are_replaced(tmp_path):
    engine = make_engine(tmp_path, groupOptions={"Roll": {}})
    omr, _ = read(COLUMNS)
    # No per-column flags known: a multi-marked value cannot be told apart
    assert get_concatenated_response(omr, engine.template)["Roll"] == "0 12456"


def test_column_states():
    assert column_state("3", col("3")) == "ok"
    assert column_state("", col("")) == "empty"
    assert column_state("12", col("12", ["multi_marked"], review=True)) == "multi"
    assert column_state("4", col("4", ["low_confidence"], review=True)) == "issue"
    # A flagged blank (possible missed mark) is an issue, not just empty
    flagged_blank = col("", ["empty", "possible_missed_mark"], review=True)
    assert column_state("", flagged_blank) == "issue"
    # A column a person reviewed counts as read
    assert column_state("12", col("12", ["multi_marked"], reviewed=True)) == "ok"
    assert column_state("", col("", ["empty"], reviewed=True)) == "empty"
    # The template's empty value counts as blank
    assert column_state("-", col("-", ["empty"]), empty_value="-") == "empty"


def test_join_group_returns_states():
    omr, fields = read(COLUMNS[:3])
    value, states = join_group(
        ["roll1", "roll2", "roll3"], omr, {"empty": " ", "multi": "*", "issue": "-"}, fields
    )
    assert value == "0 *"
    assert states == ["ok", "empty", "multi"]


def test_flagged_group_review_item():
    groups = {
        "Roll": {
            "value": "0 *",
            "flagged": True,
            "columns": [
                {"name": "roll1", "state": "ok"},
                {"name": "roll2", "state": "empty"},
                {"name": "roll3", "state": "multi"},
            ],
        }
    }
    items = group_review_items(groups, [])
    assert items == [
        {
            "kind": "custom_label",
            "name": "Roll",
            "flags": ["multi_marked"],
            "fields": ["roll1", "roll2", "roll3"],
        }
    ]
    # Not repeated when a column is already in the review list
    assert group_review_items(groups, [{"name": "roll3"}]) == []


def test_length_validation_catches_skipped_columns(tmp_path):
    engine = make_engine(
        tmp_path,
        groupOptions={"Roll": {"empty": None}},
        validate={"Roll": {"length": 6}},
    )
    columns = [col(v) for v in ["1", "2", "", "4", "5", "6"]]
    _omr, fields = read(columns)
    result = reapply_rules({"status": "ok", "fields": fields, "zones": {}}, engine.template)
    assert result["responses"]["Roll"] == "12456"
    assert not result["validation"]["Roll"]["ok"]
    assert result["status"] == "needs_review"


def test_reapply_records_groups_and_corrections_clear_them(tmp_path):
    engine = make_engine(tmp_path, groupOptions={"Roll": {}})
    _omr, fields = read(copy.deepcopy(COLUMNS))
    result = reapply_rules({"status": "ok", "fields": fields, "zones": {}}, engine.template)
    assert result["responses"]["Roll"] == "0 *-56"
    assert result["groups"]["Roll"]["flagged"]
    assert result["status"] == "needs_review"

    # A reviewer fixes the two bad columns
    for name, value in (("roll3", "2"), ("roll4", "3")):
        fields[name].update(value=value, needs_review=False, reviewed=True)
    result = reapply_rules(result, engine.template)
    assert result["responses"]["Roll"] == "0 2356"
    assert not result["groups"]["Roll"]["flagged"]
    assert result["status"] == "ok"


def test_group_options_must_name_known_keys(tmp_path):
    with pytest.raises(Exception):
        make_engine(tmp_path, groupOptions={"Roll": {"blank": " "}})
