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
