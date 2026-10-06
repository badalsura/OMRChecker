"""Template "validate" (value shape checks) and "checks" (cross-field rules)."""

import copy
import csv
import json
import random
from pathlib import Path

import cv2
import pytest

from src.pipeline import STATUS_NEEDS_REVIEW, STATUS_OK, OMREngine
from src.readers import ocr
from src.rules import reapply_rules
from src.rules.validation import ValidationRule
from src.synth import default_spec, render_sheet
from src.utils.parsing import get_concatenated_response

BASE_TEMPLATE = {
    "pageDimensions": [800, 600],
    "bubbleDimensions": [20, 20],
    "preProcessors": [],
    "fieldBlocks": {
        "Roll": {
            "fieldType": "QTYPE_INT",
            "fieldLabels": ["roll1..4"],
            "origin": [50, 50],
            "bubblesGap": 30,
            "labelsGap": 30,
        },
        "MCQ": {
            "bubbleValues": ["A", "B", "C", "D"],
            "direction": "horizontal",
            "fieldLabels": ["q1..2"],
            "origin": [300, 50],
            "bubblesGap": 30,
            "labelsGap": 30,
        },
    },
    "customLabels": {"Roll": ["roll1..4"]},
    "zones": {
        "bc": {"type": "barcode", "origin": [300, 300], "dimensions": [300, 80]},
        "printed": {"type": "ocr", "origin": [300, 400], "dimensions": [300, 50]},
        "hand": {
            "type": "icr",
            "origin": [50, 450],
            "dimensions": [200, 50],
            "options": {"characterBoxes": 4},
        },
    },
}


def make_engine(tmp_path, validate=None, checks=None, zones=None, **extra):
    template = copy.deepcopy(BASE_TEMPLATE)
    if validate is not None:
        template["validate"] = validate
    if checks is not None:
        template["checks"] = checks
    for name, options in (zones or {}).items():
        template["zones"][name].setdefault("options", {}).update(options)
    template.update(extra)
    path = Path(tmp_path, "template.json")
    path.write_text(json.dumps(template))
    return OMREngine(path)


def field(value, review=False, flags=None):
    flags = list(flags or ([] if value else ["empty"]))
    return {"value": value, "flags": flags, "needs_review": review}


def zone(value, kind="barcode", flags=None, review=False, characters=None):
    details = {"characters": characters} if characters is not None else {}
    return {
        "name": "",
        "type": kind,
        "value": value,
        "confidence": 1.0 if value else 0.0,
        "flags": list(flags or []),
        "needs_review": review,
        "details": details,
    }


class Sheet:
    """A fake read of BASE_TEMPLATE, run through the template's rules."""

    def __init__(self, engine, roll="1234", q1="A", q2="B", zones=None, lazy=None):
        self.engine = engine
        roll = list(roll) if isinstance(roll, str) else roll
        self.fields = {f"roll{i + 1}": field(v) for i, v in enumerate(roll)}
        self.fields["q1"], self.fields["q2"] = field(q1), field(q2)
        self.zones = {
            "bc": zone(""),
            "printed": zone("", "ocr"),
            "hand": zone("", "icr", characters=["", "", "", ""]),
        }
        self.zones.update(zones or {})
        self.lazy_reads = []
        self.lazy = lazy or {}

    def run(self):
        omr = {name: f["value"] for name, f in self.fields.items()}
        omr.update({name: z["value"] for name, z in self.zones.items()})
        self.responses = get_concatenated_response(omr, self.engine.template)
        self.checks, self.validation, self.extra = self.engine.template.rules.apply(
            omr, self.responses, self.fields, self.zones, read_lazy=self.read_lazy
        )
        from src.rules import review_items

        self.review = review_items(self.fields, self.zones, self.extra)
        return self

    def read_lazy(self, name):
        self.lazy_reads.append(name)
        return self.lazy[name]


# ---------------------------------------------------------------- validation


def test_validation_rule_shapes():
    rule = ValidationRule("x", {"length": [2, 4], "pattern": r"\d+"})
    assert rule.failures("123") == []
    assert rule.failures("12345") == ["length 5 not in [2, 4]"]
    assert rule.failures("12a") == ["does not match '\\\\d+'"]
    assert ValidationRule("x", {"leadingZeros": "forbid"}).failures("0123") == [
        "leading zero"
    ]
    assert ValidationRule("x", {"leadingZeros": "forbid"}).failures("0") == []
    assert ValidationRule("x", {"allowed": ["A", 7]}).failures("7") == []
    assert ValidationRule("x", {"allowed": ["A"]}).failures("B") == [
        "not an allowed value"
    ]
    numeric = ValidationRule("x", {"range": [1, 100]})
    assert numeric.failures("0042") == []  # compared as a number, kept as text
    assert numeric.failures("101") == ["101 outside [1, 100]"]
    assert numeric.failures("4a") == ["not a number"]
    assert ValidationRule("x", {"range": [None, 5]}).failures("-3") == []
    assert ValidationRule("x", {"length": 3}).failures("") == []
    assert ValidationRule("x", {"required": True}).failures("") == ["empty"]
    with pytest.raises(Exception):
        ValidationRule("x", {"onFail": "explode"})
    with pytest.raises(Exception):
        ValidationRule("x", {"pattern": "("})


def test_gaps_use_columns_not_the_concatenated_string():
    rule = ValidationRule("x", {"allowGaps": False, "allowEmptyEnds": False})
    columns = ["1", "", "3", "4"]
    # Concatenated "134" looks fine; the columns show the blank middle digit
    assert rule.failures("134") == []
    assert rule.failures("134", columns) == ["gap at position 2"]
    assert rule.failures("34", ["", "", "3", "4"]) == ["2 empty leading position(s)"]
    assert rule.failures("12", ["1", "2", "", ""]) == ["2 empty trailing position(s)"]
    assert (
        ValidationRule("x", {"allowGaps": False}).failures("34", ["", "", "3", "4"])
        == []
    )
    # Without columns, whitespace marks an empty position
    assert rule.failures("1 3") == ["gap at position 2"]


def test_custom_label_gap_sends_sheet_to_review(tmp_path):
    engine = make_engine(tmp_path, validate={"Roll": {"length": 4, "allowGaps": False}})
    sheet = Sheet(engine, roll=["1", "", "3", "4"]).run()

    record = sheet.validation["Roll"]
    assert not record["ok"]
    assert record["kind"] == "custom_label"
    assert "gap at position 2" in record["reasons"]
    assert "length 3 not in 4" in record["reasons"]
    assert sheet.responses["Roll"] == "134"  # review keeps the value
    item = sheet.review[-1]
    assert item["kind"] == "custom_label" and item["name"] == "Roll"
    assert item["flags"] == ["validation_failed"]
    assert item["fields"] == ["roll1", "roll2", "roll3", "roll4"]

    ok = Sheet(engine, roll="0123").run()
    assert ok.validation["Roll"]["ok"] and ok.review == []
    assert ok.responses["Roll"] == "0123"  # leading zeros kept, never int()


@pytest.mark.parametrize(
    "on_fail,blanked,reviewed",
    [
        ("review", False, True),
        ("blank", True, False),
        ("both", True, True),
        ("flag", False, False),
    ],
)
def test_on_fail_actions(tmp_path, on_fail, blanked, reviewed):
    engine = make_engine(
        tmp_path,
        validate={"Roll": {"allowGaps": False, "onFail": on_fail}},
        emptyValue="-",
    )
    sheet = Sheet(engine, roll=["1", "-", "3", "4"]).run()
    assert sheet.responses["Roll"] == ("-" if blanked else "1-34")
    assert bool(sheet.review) == reviewed
    assert sheet.validation["Roll"]["action"] == on_fail


def test_field_and_zone_validation_flag_the_item(tmp_path):
    engine = make_engine(
        tmp_path,
        validate={
            "q1": {"allowed": ["A", "B"]},
            "bc": {"length": 12, "onFail": "both"},
            "roll2": {"allowed": ["1"], "onFail": "blank"},
        },
    )
    sheet = Sheet(engine, roll="1234", q1="C", zones={"bc": zone("123")}).run()
    assert sheet.fields["q1"]["needs_review"]
    assert "validation_failed" in sheet.fields["q1"]["flags"]
    assert sheet.fields["q1"]["validation_reasons"] == ["not an allowed value"]
    assert sheet.zones["bc"]["needs_review"]
    assert sheet.responses["bc"] == ""
    assert {(i["kind"], i["name"]) for i in sheet.review} == {
        ("field", "q1"),
        ("zone", "bc"),
    }
    # A blanked column updates the custom label it belongs to
    assert sheet.responses["Roll"] == "134"
    assert not sheet.fields["roll2"]["needs_review"]


def test_icr_character_boxes_are_columns(tmp_path):
    engine = make_engine(tmp_path, validate={"hand": {"allowGaps": False}})
    hand = zone("124", "icr", characters=["1", "2", "", "4"])
    sheet = Sheet(engine, zones={"hand": hand}).run()
    assert sheet.validation["hand"]["reasons"] == ["gap at position 3"]


def test_unknown_validate_name_is_rejected(tmp_path):
    with pytest.raises(Exception, match="unknown name"):
        make_engine(tmp_path, validate={"nope": {"length": 3}})


# ---------------------------------------------------------------- checks


def check(**spec):
    return {"name": "book", "sources": ["bc", "printed"], **spec}


def test_agreeing_sources(tmp_path):
    engine = make_engine(tmp_path, checks=[check(normalize="digits")])
    sheet = Sheet(
        engine, zones={"bc": zone("00123"), "printed": zone("SR 00123", "ocr")}
    ).run()
    result = sheet.checks["book"]
    assert result["value"] == "00123"
    assert result["chosen_source"] == "bc"
    assert result["sources"] == {"bc": "00123", "printed": "SR 00123"}
    assert result["flags"] == [] and not result["needs_review"]
    assert sheet.responses["book"] == "00123"
    assert "book" in engine.template.output_columns
    assert sheet.review == []


@pytest.mark.parametrize(
    "spec,value,reviewed",
    [
        ({}, "111", True),
        ({"reviewOnConflict": False}, "111", False),
        ({"onConflict": "review", "reviewOnConflict": False}, "111", True),
        ({"onConflict": "error"}, "", True),
        ({"priority": ["printed", "bc"], "reviewOnConflict": False}, "222", False),
    ],
)
def test_conflicts(tmp_path, spec, value, reviewed):
    engine = make_engine(tmp_path, checks=[check(**spec)])
    sheet = Sheet(
        engine, zones={"bc": zone("111"), "printed": zone("222", "ocr")}
    ).run()
    result = sheet.checks["book"]
    assert "cross_check_failed" in result["flags"]
    assert result["value"] == value
    assert result["needs_review"] == reviewed
    assert (
        {"kind": "check", "name": "book", "flags": result["flags"]} in sheet.review
    ) == reviewed


def test_fallback_and_absorbed_source_review(tmp_path):
    engine = make_engine(tmp_path, checks=[check()])
    missing = zone("", flags=["not_found"], review=True)
    sheet = Sheet(engine, zones={"bc": missing, "printed": zone("42", "ocr")}).run()
    result = sheet.checks["book"]
    assert result["value"] == "42" and result["chosen_source"] == "printed"
    assert result["flags"] == ["fallback_used"] and not result["needs_review"]
    # The barcode's own "not_found" review is settled by the fallback
    assert not sheet.zones["bc"]["needs_review"]
    assert sheet.zones["bc"]["review_resolved_by"] == "book"
    assert sheet.review == []

    engine = make_engine(tmp_path, checks=[check(reviewOnFallback=True)])
    missing = zone("", flags=["not_found"], review=True)
    sheet = Sheet(engine, zones={"bc": missing, "printed": zone("42", "ocr")}).run()
    assert sheet.checks["book"]["needs_review"]
    assert sheet.zones["bc"]["needs_review"]

    engine = make_engine(tmp_path, checks=[check(onMissing="review")])
    sheet = Sheet(engine, zones={"printed": zone("42", "ocr")}).run()
    assert sheet.checks["book"]["needs_review"]


def test_a_source_that_decided_alone_keeps_its_review(tmp_path):
    engine = make_engine(tmp_path, checks=[check()])
    flagged = zone("777", flags=["multiple_symbols"], review=True)
    sheet = Sheet(engine, zones={"bc": flagged}).run()
    assert sheet.checks["book"]["value"] == "777"
    assert sheet.zones["bc"]["needs_review"]
    agreeing = Sheet(
        engine,
        zones={
            "bc": zone("777", flags=["multiple_symbols"], review=True),
            "printed": zone("777", "ocr"),
        },
    ).run()
    assert not agreeing.zones["bc"]["needs_review"]


def test_all_sources_missing(tmp_path):
    engine = make_engine(tmp_path, checks=[check()], emptyValue="-")
    sheet = Sheet(engine).run()
    result = sheet.checks["book"]
    assert result["flags"] == ["all_sources_missing"]
    assert result["value"] == "-" and result["needs_review"]
    quiet = make_engine(tmp_path, checks=[check(reviewOnAllMissing=False)])
    assert not Sheet(quiet).run().checks["book"]["needs_review"]


@pytest.mark.parametrize(
    "normalize,bc,printed,value",
    [
        ("upper", "ab1", " AB1 ", "AB1"),
        ("strip", "x1", " x1", "x1"),
        ("alnum", "A-1", "A 1", "A1"),
        ({"regex": r"SR-(\d+)", "group": 1}, "SR-0042", "no 0042 SR-0042", "0042"),
    ],
)
def test_normalizers(tmp_path, normalize, bc, printed, value):
    engine = make_engine(tmp_path, checks=[check(normalize=normalize)])
    sheet = Sheet(engine, zones={"bc": zone(bc), "printed": zone(printed, "ocr")}).run()
    assert sheet.checks["book"]["value"] == value
    assert sheet.checks["book"]["flags"] == []


def test_bubble_column_against_handwriting(tmp_path):
    """Compare a written digit (ICR) with its bubbled column: just two sources."""
    engine = make_engine(
        tmp_path,
        checks=[
            {"name": "roll_check", "sources": ["Roll", "hand"], "output": "roll_final"}
        ],
    )
    same = Sheet(engine, roll="1234", zones={"hand": zone("1234", "icr")}).run()
    assert same.responses["roll_final"] == "1234"
    assert same.checks["roll_check"]["flags"] == []
    differ = Sheet(engine, roll="1234", zones={"hand": zone("1284", "icr")}).run()
    assert differ.checks["roll_check"]["needs_review"]
    assert "roll_final" in engine.template.output_columns


def test_rule_outputs_can_be_validated_and_chained(tmp_path):
    engine = make_engine(
        tmp_path,
        checks=[
            # Declared before the check it reads: order is resolved
            {"name": "final", "sources": ["book", "q1"], "reviewOnConflict": False},
            check(normalize="digits"),
        ],
        validate={"book": {"length": 5, "onFail": "blank"}},
    )
    good = Sheet(engine, zones={"bc": zone("12345")}).run()
    assert good.checks["final"]["value"] == "12345"
    bad = Sheet(engine, zones={"bc": zone("123")}).run()
    assert bad.checks["book"]["value"] == ""
    assert "validation_failed" in bad.checks["book"]["flags"]
    assert bad.responses["book"] == ""
    # The blanked output is missing for the next check, which falls back to q1
    assert bad.checks["final"]["value"] == "A"
    assert bad.checks["final"]["flags"] == ["fallback_used"]


def test_invalid_source_is_skipped(tmp_path):
    engine = make_engine(
        tmp_path,
        checks=[check(reviewOnConflict=False)],
        validate={"bc": {"length": 6, "onFail": "flag"}},
    )
    sheet = Sheet(
        engine, zones={"bc": zone("123"), "printed": zone("654321", "ocr")}
    ).run()
    result = sheet.checks["book"]
    assert result["value"] == "654321"
    assert result["skipped"] == {"bc": "invalid"}
    assert "fallback_used" in result["flags"]


@pytest.mark.parametrize(
    "checks,message",
    [
        ([check(), check()], "Duplicate check name"),
        ([check(sources=["bc", "nope"])], "unknown source"),
        ([check(output="q1")], "already exists"),
        (
            [
                {"name": "a", "sources": ["b"]},
                {"name": "b", "sources": ["a"]},
            ],
            "cycle",
        ),
        ([check(priority=["bc", "q2"])], "not sources"),
        ([check(normalize="weird")], "Invalid"),  # rejected by the schema
        ([check(onConflict="maybe")], "Invalid"),
    ],
)
def test_bad_checks_are_rejected(tmp_path, checks, message):
    with pytest.raises(Exception, match=message):
        make_engine(tmp_path, checks=checks)


def test_schema_rejects_unknown_keys(tmp_path):
    with pytest.raises(Exception):
        make_engine(tmp_path, validate={"Roll": {"lenght": 4}})
    with pytest.raises(Exception):
        make_engine(tmp_path, checks=[check(onMissing="maybe")])


def test_fallback_zone_reads_ocr_only_when_needed(tmp_path):
    engine = make_engine(tmp_path, zones={"bc": {"fallbackZone": "printed"}})
    printed = engine.template.zones[1]
    assert printed.name == "printed" and printed.lazy
    lazy_ocr = zone("", "ocr", flags=["not_read"])

    sheet = Sheet(
        engine,
        zones={"bc": zone("SR123"), "printed": dict(lazy_ocr)},
        lazy={"printed": zone("SR999", "ocr")},
    ).run()
    assert sheet.lazy_reads == []
    assert sheet.responses["bc"] == "SR123"
    assert sheet.checks["bc"]["skipped"] == {"printed": "not_read"}
    assert sheet.review == []

    missing = zone("", flags=["not_found"], review=True)
    sheet = Sheet(
        engine,
        zones={"bc": missing, "printed": dict(lazy_ocr)},
        lazy={"printed": zone("SR999", "ocr")},
    ).run()
    assert sheet.lazy_reads == ["printed"]
    assert sheet.responses["bc"] == "SR999"
    assert sheet.zones["printed"]["value"] == "SR999"
    assert sheet.checks["bc"]["flags"] == ["fallback_used"]
    assert sheet.checks["bc"]["needs_review"]  # reviewOnFallback defaults to true

    with pytest.raises(Exception, match="fallbackZone"):
        make_engine(tmp_path, zones={"bc": {"fallbackZone": "missing"}})


def test_reapply_is_idempotent(tmp_path):
    engine = make_engine(
        tmp_path,
        checks=[check()],
        validate={"Roll": {"allowGaps": False}},
    )
    sheet = Sheet(
        engine,
        roll=["1", "", "3", "4"],
        zones={
            "bc": zone("", flags=["not_found"], review=True),
            "printed": zone("9", "ocr"),
        },
    ).run()
    stored = {
        "status": STATUS_NEEDS_REVIEW,
        "fields": copy.deepcopy(sheet.fields),
        "zones": copy.deepcopy(sheet.zones),
    }
    first = copy.deepcopy(reapply_rules(stored, engine.template))
    second = reapply_rules(stored, engine.template)
    assert first["review"] == second["review"] == sheet.review
    assert first["checks"] == sheet.checks

    # A correction of the gap clears the custom label's review item
    stored["fields"]["roll2"]["value"] = "2"
    stored["fields"]["roll2"]["flags"] = []
    fixed = reapply_rules(stored, engine.template)
    assert fixed["responses"]["Roll"] == "1234"
    assert fixed["status"] == STATUS_OK


def test_no_rules_costs_nothing(tmp_path):
    engine = make_engine(tmp_path)
    assert not engine.template.rules
    sheet = Sheet(engine, roll=["1", "", "3", "4"]).run()
    assert sheet.checks == {} and sheet.validation == {} and sheet.review == []


# ---------------------------------------------------------------- end to end


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=20)


def spec_engine(tmp_path, spec, **template_extra):
    template = spec.to_template(pre_processors=[])
    template["customLabels"] = {"Roll": [f"roll1..{len(spec.blocks[0].field_labels)}"]}
    for key, value in template_extra.items():
        if key == "zone_options":
            for name, options in value.items():
                template["zones"][name].setdefault("options", {}).update(options)
        else:
            template[key] = value
    path = Path(tmp_path, "template.json")
    path.write_text(json.dumps(template))
    return OMREngine(path), template


def test_scan_reports_checks_and_validation(tmp_path, spec):
    engine, _ = spec_engine(
        tmp_path,
        spec,
        validate={"Roll": {"length": 6, "allowGaps": False}},
        checks=[
            {"name": "id", "sources": ["sheet_id", "qr"], "reviewOnConflict": False}
        ],
    )
    answers = {label: "1" for label in spec.blocks[0].field_labels}
    answers["roll3"] = ""
    image, truth = render_sheet(
        spec,
        answers,
        zone_values={"sheet_id": "SHEET-1", "qr": "SHEET-1"},
        rng=random.Random(1),
    )
    result = engine.scan(image, "sheet")
    data = result.to_dict()
    for key in (
        "file_id",
        "status",
        "responses",
        "fields",
        "zones",
        "review",
        "score",
        "error",
        "thresholds",
        "timings_ms",
    ):
        assert key in data
    assert data["checks"]["id"]["value"] == "SHEET-1"
    assert data["responses"]["id"] == "SHEET-1"
    assert data["validation"]["Roll"]["reasons"] == [
        "length 5 not in 6",
        "gap at position 3",
    ]
    assert {"kind": "custom_label", "name": "Roll"} == {
        k: v for k, v in data["review"][-1].items() if k in ("kind", "name")
    }
    assert result.status == STATUS_NEEDS_REVIEW
    assert "rules" in data["timings_ms"]


@pytest.mark.skipif(not ocr.tesseract_available(), reason="tesseract not installed")
def test_barcode_falls_back_to_printed_digits(tmp_path, spec):
    engine, template = spec_engine(
        tmp_path,
        spec,
        zone_options={
            "sheet_id": {"fallbackZone": "exam_code", "fallbackNormalize": "digits"},
            "exam_code": {"whitelist": "0123456789"},
        },
    )
    rng = random.Random(3)
    image, _ = render_sheet(
        spec, {}, zone_values={"sheet_id": "482913", "exam_code": "482913"}, rng=rng
    )
    readable = engine.scan(image, "ok")
    assert readable.responses["sheet_id"] == "482913"
    assert readable.zones["exam_code"]["flags"] == ["not_read"]  # never OCR'd
    assert readable.checks["sheet_id"]["chosen_source"] == "sheet_id"

    x, y = template["zones"]["sheet_id"]["origin"]
    w, h = template["zones"]["sheet_id"]["dimensions"]
    cv2.rectangle(image, (x, y), (x + w, y + h), 255, -1)  # barcode torn off
    damaged = engine.scan(image, "damaged")
    assert damaged.zones["sheet_id"]["flags"] == ["not_found"]
    assert damaged.responses["sheet_id"] == "482913"
    assert damaged.checks["sheet_id"]["chosen_source"] == "exam_code"
    assert damaged.checks["sheet_id"]["flags"] == ["fallback_used"]
    assert damaged.status == STATUS_NEEDS_REVIEW
    assert {
        "kind": "check",
        "name": "sheet_id",
        "flags": ["fallback_used"],
    } in damaged.review


def test_cli_writes_rule_outputs(tmp_path, spec, mocker):
    from src.tests.utils import run_entry_point, setup_mocker_patches

    setup_mocker_patches(mocker)
    input_dir = Path(tmp_path, "in")
    input_dir.mkdir()
    _, template = spec_engine(
        input_dir,
        spec,
        validate={"Roll": {"allowGaps": False}},
        checks=[{"name": "id", "sources": ["sheet_id", "qr"], "output": "sheet_key"}],
    )
    answers = {label: "2" for label in spec.blocks[0].field_labels}
    answers["roll2"] = ""
    image, _ = render_sheet(
        spec,
        answers,
        zone_values={"sheet_id": "S-1", "qr": "S-1"},
        rng=random.Random(2),
    )
    cv2.imwrite(str(Path(input_dir, "sheet.png")), image)
    run_entry_point(str(input_dir), str(Path(tmp_path, "out")))

    results = next(Path(tmp_path, "out").rglob("Results_*.csv"))
    rows = list(csv.DictReader(results.open()))
    assert rows[0]["sheet_key"] == "S-1"
    review = Path(tmp_path, "out", "Manual", "NeedsReview.csv").read_text()
    assert "custom_label" in review and "Roll" in review
