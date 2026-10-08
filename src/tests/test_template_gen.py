import csv
import itertools
import json
import random

import cv2
import numpy as np
import pytest

from src.synth.render import augment, default_spec, random_answers, render_sheet
from src.template_gen import (
    apply_corrections,
    bubble_boxes,
    generate_template,
    validate_template,
)
from src.template_gen.__main__ import main as cli_main
from src.template_gen.assignment import linear_sum_assignment
from src.template_gen.labels import compress_labels, prepare_labels

N_SHEETS = 20


def _render(spec, count, seed, multi_rate=0.0):
    rng = random.Random(seed)
    images, labels = [], []
    for _ in range(count):
        answers = random_answers(spec, rng, multi_rate=multi_rate)
        image, _ = render_sheet(spec, answers, rng=rng, mark_style="mixed")
        captured, _ = augment(image, rng, rotation=1.5, perspective=0.01)
        images.append(captured)
        labels.append(answers)
    return images, labels


@pytest.fixture(scope="module")
def spec():
    return default_spec()


@pytest.fixture(scope="module")
def sheets(spec):
    return _render(spec, N_SHEETS, seed=7, multi_rate=0.03)


@pytest.fixture(scope="module")
def generated(sheets):
    images, labels = sheets
    return generate_template(images, labels)


def _scale(result, spec):
    width, height = result.template["pageDimensions"]
    return width / spec.page[0], height / spec.page[1]


def test_template_is_schema_valid(generated):
    assert validate_template(generated.template) == []
    assert generated.report["schema_errors"] == []
    json.dumps(generated.to_dict())


def test_blocks_and_bubble_positions(generated, spec):
    template = generated.template
    assert len(template["fieldBlocks"]) == len(spec.blocks)
    sx, sy = _scale(generated, spec)
    truth = {
        (label, value): (x + spec.bubble[0] / 2, y + spec.bubble[1] / 2)
        for _, label, value, x, y in spec.bubble_positions()
    }
    found = {
        (label, value): (x + w / 2, y + h / 2)
        for _, label, value, x, y, w, h in bubble_boxes(template)
    }
    assert set(found) == set(truth)
    errors = [
        np.hypot(found[k][0] - truth[k][0] * sx, found[k][1] - truth[k][1] * sy)
        for k in truth
    ]
    assert max(errors) < 3.0, max(errors)
    directions = {
        block["fieldLabels"][0]: block["direction"]
        for block in template["fieldBlocks"].values()
    }
    assert directions == {
        "roll1..6": "vertical",
        "q1..20": "horizontal",
        "q21..40": "horizontal",
    }


def test_label_agreement(generated):
    report = generated.report
    assert report["label_agreement"] >= 0.99
    assert all(block["label_agreement"] >= 0.99 for block in report["blocks"])
    assert report["self_check"]["registered"]["overall_agreement"] >= 0.99
    end_to_end = report["self_check"].get("end_to_end", {})
    if "overall_agreement" in end_to_end:
        assert end_to_end["overall_agreement"] >= 0.99


def test_custom_labels_group_roll_digits(generated):
    assert generated.template["customLabels"] == {"Roll": ["roll1..6"]}


def test_timing_tracks(generated, spec):
    processors = {p["name"]: p for p in generated.template["preProcessors"]}
    assert "TimingMarkAlignment" in processors
    options = processors["TimingMarkAlignment"]["options"]
    sx, sy = _scale(generated, spec)
    for name, expected in spec.timing_tracks.items():
        marks = np.array(options["tracks"][name]["marks"])
        assert len(marks) == len(expected)
        assert np.abs(marks - np.array(expected) * [sx, sy]).max() < 2.5
    assert np.allclose(options["markDimensions"], spec.timing_mark, atol=2)
    assert len(generated.report["corner_markers"]) == 4


def test_barcode_qr_and_icr_zones(generated, spec):
    zones = generated.template["zones"]
    by_type = {}
    for zone in zones.values():
        by_type.setdefault(zone["type"], []).append(zone)
    assert by_type["barcode"][0]["options"]["formats"] == ["Code128"]
    assert by_type["qrcode"][0]["options"]["formats"] == ["QRCode"]
    sx, sy = _scale(generated, spec)
    truth = {z.type: z for z in spec.zones}
    for kind in ("barcode", "qrcode", "icr"):
        x, y = by_type[kind][0]["origin"]
        w, h = by_type[kind][0]["dimensions"]
        tz = truth[kind]
        cx, cy = (tz.origin[0] + tz.dimensions[0] / 2) * sx, (
            tz.origin[1] + tz.dimensions[1] / 2
        ) * sy
        assert x <= cx <= x + w and y <= cy <= y + h, kind
    assert by_type["icr"][0]["options"]["characterBoxes"] == 6
    flagged = {item["name"] for item in generated.report["needs_verification"]}
    assert set(zones) <= flagged


def test_registered_images_and_reference(generated):
    width, height = generated.template["pageDimensions"]
    assert generated.reference_image.shape == (height, width)
    assert generated.reference_image.dtype == np.uint8
    assert all(s["used"] for s in generated.report["sheets"])


def test_without_labels(sheets):
    images, _ = sheets
    result = generate_template(images[:4], None, {"end_to_end_check": False})
    template = result.template
    assert validate_template(template) == []
    assert len(template["fieldBlocks"]) == 3
    labels = sorted(
        label
        for block in template["fieldBlocks"].values()
        for label in block["fieldLabels"]
    )
    assert labels == ["q1..20", "q21..40", "roll1..6"]
    blocks = [i for i in result.report["needs_verification"] if i["kind"] == "block"]
    assert len(blocks) == 3
    assert result.report["self_check"]["registered"]["failed_sheets"] == []


def test_composite_label_columns(sheets):
    images, labels = sheets
    composite = []
    for answers in labels[:12]:
        answers = dict(answers)
        digits = [answers.pop(f"roll{i}") for i in range(1, 7)]
        # A concatenated column is ambiguous when a digit is blank or
        # multi-marked; such values are left unknown here
        answers["Roll"] = "".join(digits) if all(len(d) == 1 for d in digits) else None
        composite.append(answers)
    result = generate_template(images[:12], composite, {"self_check": False})
    assert result.template["customLabels"] == {"Roll": ["roll1..6"]}
    assert result.report["label_agreement"] >= 0.98


def test_hungarian_matches_brute_force():
    rng = np.random.default_rng(0)
    for _ in range(50):
        n, m = (int(v) for v in rng.integers(1, 6, 2))
        cost = rng.random((n, m))
        rows, cols = linear_sum_assignment(cost)
        k = min(n, m)
        if n <= m:
            best = min(
                sum(cost[i, p[i]] for i in range(n))
                for p in itertools.permutations(range(m), n)
            )
        else:
            best = min(
                sum(cost[p[j], j] for j in range(m))
                for p in itertools.permutations(range(n), m)
            )
        assert len(rows) == k
        assert abs(cost[rows, cols].sum() - best) < 1e-9


def test_label_helpers():
    assert compress_labels(["q1", "q2", "q3", "x", "q5_1", "q5_2"]) == [
        "q1..3",
        "x",
        "q5_1",
        "q5_2",
    ]
    names, table, composites = prepare_labels(
        [{"Roll": "123", "q1": "AB"}, {"Roll": "456", "q1": ""}, None], 3
    )
    assert composites == {"Roll": ["roll1", "roll2", "roll3"]}
    assert names == ["roll1", "roll2", "roll3", "q1"]
    assert table[0]["q1"] == {"A", "B"} and table[1]["q1"] == set()
    assert table[1]["roll2"] == {"5"} and table[2]["roll1"] is None


def test_apply_corrections(generated):
    template = generated.template
    name = next(
        n for n, b in template["fieldBlocks"].items() if b["fieldLabels"] == ["q1..20"]
    )
    origin = template["fieldBlocks"][name]["origin"]
    corrected = apply_corrections(
        template,
        [
            {"op": "move_block", "name": name, "dx": 4, "dy": -2},
            {"op": "rename_block", "name": name, "to": "MCQ_1"},
            {"op": "rename_label", "from": "q1", "to": "first"},
            {
                "op": "update_block",
                "name": "MCQ_1",
                "set": {"bubbleValues": list("abcd")},
            },
        ],
    )
    block = corrected["fieldBlocks"]["MCQ_1"]
    assert block["origin"] == [origin[0] + 4, origin[1] - 2]
    assert block["fieldLabels"][0] == "first" and block["bubbleValues"] == list("abcd")
    assert name in template["fieldBlocks"]  # the original is untouched
    with pytest.raises(ValueError):
        apply_corrections(template, [{"op": "delete_block", "name": "missing"}])


def test_cli(tmp_path, sheets, spec):
    images, labels = sheets
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    columns = spec.field_labels()
    with open(tmp_path / "labels.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["file_name"] + columns)
        for k, (image, answers) in enumerate(zip(images[:8], labels[:8])):
            cv2.imwrite(str(image_dir / f"sheet_{k}.png"), image)
            writer.writerow([f"sheet_{k}.png"] + [answers[c] for c in columns])
    out = tmp_path / "out"
    code = cli_main(
        [
            "--images",
            str(image_dir),
            "--labels",
            str(tmp_path / "labels.csv"),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    for name in (
        "template.json",
        "reference.png",
        "overlay.png",
        "generation_report.json",
    ):
        assert (out / name).exists()
    template = json.loads((out / "template.json").read_text())
    assert validate_template(template) == []
    report = json.loads((out / "generation_report.json").read_text())
    assert report["label_agreement"] >= 0.98


# --------------------------------------------------------------------------
# Plan items 10 / 16: label files, exact naming, alignment report, boxes
# --------------------------------------------------------------------------
from src.template_gen import boxes as gen_boxes  # noqa: E402
from src.template_gen import marks as gen_marks  # noqa: E402
from src.template_gen import naming  # noqa: E402
from src.template_gen.bubbles import Grid  # noqa: E402
from src.utils.label_files import LabelFileError, parse_label_file  # noqa: E402


def _xlsx_bytes(rows):
    openpyxl = pytest.importorskip("openpyxl")
    import io

    book = openpyxl.Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_label_file_xlsx_file_name_and_answer_string():
    content = _xlsx_bytes(
        [
            ["File Name", "pcode", "ANS"],
            ["000001.jpg", 1051, "CB A*"],
            ["000002.jpg", "01052", "AB"],
        ]
    )
    labels, info = parse_label_file(
        content, ["scans/000002.jpg", "000001.JPG"], "labels.xlsx"
    )
    assert info["format"] == "xlsx" and info["file_column"] == "File Name"
    assert info["answer_columns"] == {"ANS": {"prefix": "q", "questions": 5}}
    first, second = labels
    # Matched by name (any case/folder), Excel's dropped zero restored
    assert second["pcode"] == "01051" and first["pcode"] == "01052"
    assert [second[f"q{i}"] for i in range(1, 6)] == ["C", "B", "", "A", "*"]
    assert [first[f"q{i}"] for i in range(1, 6)] == ["A", "B", "", "", ""]


@pytest.mark.parametrize("header", ["FILE-NAME", "file_name", "Image Name", " filename "])
def test_label_file_name_column_any_spelling(header):
    text = f"{header};q1\nb.png;A\na.png;C\n".encode()
    labels, info = parse_label_file(text, ["a.png", "b.png"], "x.csv")
    assert info["file_column"].strip() == header.strip()
    assert labels == [{"q1": "C"}, {"q1": "A"}]


def test_label_file_errors_and_student_name_column():
    with pytest.raises(LabelFileError):
        parse_label_file(b"q1\nA\n", ["a.png", "b.png"], "x.csv")
    with pytest.raises(LabelFileError):
        parse_label_file(b"\xd0\xcf\x11", ["a.png"], "old.xls")
    # A "Name" column of student names is data, rows go by order
    labels, info = parse_label_file(b"Name,q1\nAsha,A\nRavi,B\n", ["a.png", "b.png"])
    assert info["file_column"] is None and labels[1] == {"Name": "Ravi", "q1": "B"}


def _digit_fills(values, order, sheets, constant=False):
    """Fills (S, 10, cols) of a vertical digit grid marking `values`."""
    cols = len(values[0])
    fills = np.zeros((sheets, 10, cols))
    for s in range(sheets):
        text = values[0 if constant else s]
        for c, ch in enumerate(text):
            if ch != " ":
                fills[s, order.index(ch), c] = 120.0
    return fills


def test_exact_naming_constant_column_and_digit_order():
    order = list("1234567890")
    grid = Grid(x0=100, y0=100, dx=30, dy=30, cols=4, rows=10, bubble=[20, 20])
    fills = [_digit_fills(["3962"], order, 6, constant=True)]
    labels = [{"subject": "3962", "name": f"s{i}"} for i in range(6)]
    matches = naming.match_grids([grid], fills, 60.0, labels)
    assert len(matches) == 1
    match = matches[0]
    assert match["column"] == "subject" and match["order"] == "1..9,0"
    assignments, composites, truth = naming.build_assignments(matches, [grid], set())
    assert assignments[0]["field_labels"] == ["subject1", "subject2", "subject3", "subject4"]
    assert composites == {"subject": ["subject1", "subject2", "subject3", "subject4"]}
    table = naming.truth_tokens(truth, labels, 6)
    assert table[0]["subject"] == "3962"


def test_exact_naming_ragged_hundreds_column():
    digits = list("0123456789")
    main = Grid(x0=130, y0=100, dx=30, dy=30, cols=2, rows=10, bubble=[20, 20])
    ragged = Grid(x0=100, y0=100, dx=0, dy=30, cols=1, rows=2, bubble=[20, 20])
    values = ["028", "126", "048", "010", "139"]
    fills_main = np.zeros((5, 10, 2))
    fills_ragged = np.zeros((5, 2, 1))
    for s, v in enumerate(values):
        fills_ragged[s, digits.index(v[0]), 0] = 100
        for c in range(2):
            fills_main[s, digits.index(v[c + 1]), c] = 100
    labels = [{"marks1": v} for v in values]
    matches = naming.match_grids([main, ragged], [fills_main, fills_ragged], 50.0, labels)
    assert matches and matches[0]["grids"] == [1, 0]
    assignments, composites, _ = naming.build_assignments(matches, [main, ragged], set())
    assert composites == {"marks1": ["marks1_1", "marks1_2", "marks1_3"]}
    assert assignments[1]["values"] == ["0", "1"]


def test_symmetric_tracks_and_asymmetric_index_points():
    page = (400, 600)
    left = [[20, 50 + 50 * i] for i in range(10)]
    right = [[380, 50 + 50 * i] for i in range(10)]
    tracks = {
        "left": {"marks": left, "pitch": 50.0},
        "right": {"marks": right, "pitch": 50.0},
    }
    assert gen_marks.tracks_symmetric(tracks, page)
    lopsided = {"left": {"marks": left[:7], "pitch": 50.0}, "right": tracks["right"]}
    assert not gen_marks.tracks_symmetric(lopsided, page)
    dot = {"center": [60, 300], "size": [12, 12], "shape": "circle", "area": 110}
    twin_a = {"center": [100, 100], "size": [12, 12], "shape": "circle", "area": 110}
    twin_b = {"center": [300, 500], "size": [12, 12], "shape": "circle", "area": 110}
    chosen = gen_marks.asymmetric_points([twin_a, twin_b, dot], page)
    assert [c["center"] for c in chosen] == [[60, 300]]
    point = gen_marks.index_point(chosen[0], "P1")
    assert point == {
        "name": "P1", "center": [60.0, 300.0], "size": [12, 12],
        "shape": "circle", "required": True,
    }


def _marks_page():
    page = np.full((600, 400), 255, np.uint8)
    for i in range(10):
        cv2.rectangle(page, (10, 45 + 50 * i), (30, 55 + 50 * i), 0, -1)
    cv2.circle(page, (200, 300), 7, 0, -1)
    return page


def test_find_marks_in_box_and_mark_near_small_dot():
    page = _marks_page()
    track = gen_marks.find_marks_in_box(page, [0, 20, 45, 560], (400, 600))
    assert track["orientation"] == "vertical" and len(track["marks"]) == 10
    assert abs(track["pitch"] - 50) < 1
    dot = gen_marks.find_mark_near(page, [204, 296], (400, 600))
    assert dot["shape"] == "circle" and abs(dot["center"][0] - 200) < 1.5
    counts = gen_marks.match_counts(
        page, {"left": {"marks": track["marks"], "pitch": 50.0}},
        [gen_marks.index_point(dot, "P1")],
    )
    assert counts["tracks"]["left"] == [10, 10] and counts["index_points"]["P1"]


def test_printed_boxes_border_gap_and_adopt():
    page = np.full((800, 600), 255, np.uint8)
    cv2.rectangle(page, (90, 90), (290, 310), 0, 2)  # box around a 4x5 grid
    for r in range(5):
        for c in range(4):
            cv2.circle(page, (120 + 45 * c, 120 + 42 * r), 12, 0, 2)
    cv2.rectangle(page, (350, 400), (560, 700), 0, 2)  # an empty box
    found = gen_boxes.detect_printed_boxes(page, (600, 800))
    assert any(abs(b[0] - 90) <= 3 and abs(b[1] - 90) <= 3 for b in found)
    grid = Grid(x0=120, y0=120, dx=45, dy=42, cols=4, rows=5, bubble=[25, 25])
    fit = gen_boxes.border_for_grid(grid, found, [grid])
    assert fit is not None and all(10 <= g <= 25 for g in fit[1])
    block, info = gen_boxes.block_from_box(page, fit[0], (600, 800))
    assert info["rows"] * info["cols"] == 20 and block["rectifyOnBorder"]
    report = gen_boxes.boxes_report(found, [grid], {0: "B1"})
    assert any(r["blocks"] == ["B1"] for r in report)
    assert any(not r["blocks"] and not r["adoptable"] for r in report)


def test_generator_report_alignment_and_info(generated):
    report = generated.report
    alignment = report["alignment"]
    assert alignment["method"] == "tracks"
    assert alignment["summary"] and "marks, pitch" in alignment["summary"][0]
    assert len(alignment["sheets"]) == len(report["sheets"])
    for entry in alignment["sheets"]:
        assert entry["expected"] > 0 and entry["found"] <= entry["expected"]
    assert isinstance(report["info"], list)
    assert not any("page edges" in w for w in report["warnings"])
