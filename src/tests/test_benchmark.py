"""Benchmark metrics on a hand-built case and the end-to-end runners."""

import csv
import json
import random

import cv2
import pytest

from src.benchmark import compute_metrics, format_summary, run_files, run_synthetic
from src.benchmark.__main__ import main as benchmark_main
from src.synth import default_spec, random_answers, render_sheet


def bubble(value, marked, x=0):
    return {"value": value, "x": x, "y": 0, "w": 10, "h": 10, "marked": marked}


def field(label, value, marked_values, flags=(), needs_review=False, confidence=0.9):
    return {
        "label": label,
        "value": value,
        "confidence": confidence,
        "flags": list(flags),
        "needs_review": needs_review,
        "bubbles": [bubble(v, v in marked_values) for v in "ABCD"],
    }


def sheet(file_id, fields, zones=None, status=None, timings=None):
    needs_review = any(f["needs_review"] for f in fields.values()) or any(
        z["needs_review"] for z in (zones or {}).values()
    )
    return {
        "file_id": file_id,
        "status": status or ("needs_review" if needs_review else "ok"),
        "responses": {
            **{k: f["value"] for k, f in fields.items()},
            **{k: z["value"] for k, z in (zones or {}).items()},
        },
        "fields": fields,
        "zones": zones or {},
        "timings_ms": timings or {"bubbles": 10.0, "total": 12.0},
    }


def zone(value, kind="barcode", needs_review=False, confidence=1.0):
    return {
        "type": kind,
        "value": value,
        "confidence": confidence,
        "flags": ["not_found"] if needs_review else [],
        "needs_review": needs_review,
    }


def hand_built_records():
    return [
        # Sheet 1: all correct, nothing flagged
        {
            "file_id": "s1.png",
            "truth": {"q1": "A", "q2": "", "id": "X1"},
            "result": sheet(
                "s1.png",
                {"q1": field("q1", "A", "A"), "q2": field("q2", "", "", ["empty"])},
                {"id": zone("X1")},
                timings={"bubbles": 10.0, "total": 10.0},
            ),
        },
        # Sheet 2: q1 silently wrong (B read as C), q2 flagged and wrong, zone flagged wrong
        {
            "file_id": "s2.png",
            "truth": {"q1": "B", "q2": "AD", "id": "X2"},
            "result": sheet(
                "s2.png",
                {
                    "q1": field("q1", "C", "C", confidence=0.95),
                    "q2": field("q2", "A", "A", ["low_confidence"], True, 0.2),
                },
                {"id": zone("", needs_review=True, confidence=0.0)},
                timings={"bubbles": 30.0, "total": 30.0},
            ),
        },
        # Sheet 3: multi mark correctly read but flagged; zone right
        {
            "file_id": "s3.png",
            "truth": {"q1": "AB", "q2": "D", "id": "X3"},
            "result": sheet(
                "s3.png",
                {
                    "q1": field("q1", "AB", "AB", ["multi_marked"], True),
                    "q2": field("q2", "D", "D"),
                },
                {"id": zone("X3")},
                timings={"bubbles": 20.0, "total": 20.0},
            ),
        },
        # Sheet 4: registration failure
        {
            "file_id": "s4.png",
            "truth": {"q1": "A"},
            "result": {
                "file_id": "s4.png",
                "status": "error",
                "error": "Sheet registration failed (page, markers or timing marks not found)",
                "timings_ms": {"registration": 5.0},
            },
        },
    ]


def test_metrics_on_hand_built_case():
    metrics = compute_metrics(hand_built_records(), wall_seconds=2.0, workers=1)

    sheets = metrics["sheets"]
    assert sheets["total"] == 4 and sheets["scanned"] == 3
    assert sheets["error"] == 1 and sheets["registration_failures"] == 1
    assert sheets["registration_failure_rate"] == 0.25
    assert sheets["ok"] == 1 and sheets["needs_review"] == 2
    assert sheets["exact_match"] == 2  # s1 and s3
    assert sheets["exact_match_rate"] == pytest.approx(2 / 3, abs=1e-5)
    assert sheets["auto_accepted_accuracy"] == 1.0  # the only ok sheet is right
    assert sheets["straight_through_rate"] == 0.25

    fields = metrics["fields"]
    assert fields["total"] == 6 and fields["correct"] == 4
    assert fields["flagged"] == 2  # s2.q2 (wrong) and s3.q1 (right)
    assert fields["auto_accepted"] == 4 and fields["silent_errors"] == 1
    assert fields["auto_accepted_accuracy"] == 0.75
    assert fields["flagged_error_rate"] == 0.5
    assert fields["errors_caught_rate"] == 0.5

    barcode = metrics["by_kind"]["zone:barcode"]
    assert barcode["total"] == 3 and barcode["correct"] == 2
    assert barcode["flagged"] == 1 and barcode["silent_errors"] == 0
    assert barcode["auto_accepted_accuracy"] == 1.0
    values = metrics["values"]
    assert (
        values["total"] == 9 and values["correct"] == 6 and values["silent_errors"] == 1
    )

    # Bubbles: s1 q1 A tp; s2 q1 B fn, C fp; s2 q2 A tp, D fn; s3 q1 A,B tp; s3 q2 D tp
    b = metrics["bubbles"]
    assert (b["true_positive"], b["false_positive"], b["false_negative"]) == (5, 1, 2)
    assert b["total"] == 24
    assert b["precision"] == pytest.approx(5 / 6, abs=1e-5)
    assert b["recall"] == pytest.approx(5 / 7, abs=1e-5)

    # The silent error is listed first
    worst = metrics["worst_errors"]
    assert metrics["n_errors"] == 3
    assert worst[0]["file"] == "s2.png" and worst[0]["column"] == "q1"
    assert worst[0]["flagged"] is False and worst[0]["expected"] == "B"
    assert worst[0]["got"] == "C"
    assert all(e["flagged"] for e in worst[1:])

    assert metrics["review_reasons"]["low_confidence"] == {"count": 1, "wrong": 1}
    assert metrics["review_reasons"]["multi_marked"] == {"count": 1, "wrong": 0}

    throughput = metrics["throughput"]
    assert throughput["sheets_per_sec"] == 2.0
    assert throughput["latency_ms"]["bubbles"]["p50"] == 20.0
    assert throughput["latency_ms"]["registration"]["max"] == 5.0

    text = format_summary(metrics)
    assert "SILENT  s2.png  q1" in text and "registration" in text


def test_custom_label_columns_and_missing_columns():
    record = {
        "file_id": "c.png",
        "truth": {"roll": "12", "nope": "x"},
        "result": sheet(
            "c.png",
            {
                "roll1": field(
                    "roll1", "1", "", needs_review=True, flags=["weak_mark"]
                ),
                "roll2": field("roll2", "2", ""),
            },
        ),
    }
    record["result"]["responses"] = {"roll": "12"}
    metrics = compute_metrics([record], column_fields={"roll": ["roll1", "roll2"]})
    custom = metrics["by_kind"]["custom"]
    assert custom["total"] == 1 and custom["correct"] == 1 and custom["flagged"] == 1
    assert metrics["missing_columns"] == ["nope"]


def test_synthetic_benchmark_runs_clean_sheets(tmp_path):
    metrics = run_synthetic(5, preset="clean", seed=3, mark_style="pen")
    assert metrics["sheets"]["total"] == 5 and metrics["sheets"]["error"] == 0
    assert metrics["values"]["total"] == 5 * 46
    assert metrics["values"]["accuracy"] >= 0.99
    assert metrics["fields"]["auto_accepted_accuracy"] == 1.0
    assert metrics["bubbles"]["f1"] >= 0.99
    assert metrics["throughput"]["sheets_per_sec"] > 0
    assert metrics["throughput"]["latency_ms"]["total"]["p50"] > 0
    assert metrics["config"]["preset"] == "clean"
    json.dumps(metrics)  # the report must be serialisable


def test_file_benchmark_and_cli(tmp_path):
    spec = default_spec(questions=12, roll_digits=3, with_zones=False)
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps(spec.to_template(pre_processors=[])))
    images = tmp_path / "images"
    images.mkdir()
    rng = random.Random(4)
    rows = []
    for i in range(3):
        answers = random_answers(spec, rng, blank_rate=0.1)
        image, truth = render_sheet(spec, answers, rng=rng, mark_style="pen")
        cv2.imwrite(str(images / f"sheet{i}.png"), image)
        rows.append({"file_name": f"sheet{i}.png", **truth["answers"]})
    # Plant one wrong truth value to check the error report
    rows[1]["q1"] = "D" if rows[1]["q1"] != "D" else "A"
    truth_path = tmp_path / "truth.csv"
    with open(truth_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    metrics = run_files(template_path, images, truth_path, workers=1)
    assert metrics["sheets"]["scanned"] == 3
    assert metrics["fields"]["total"] == 3 * 15
    assert metrics["fields"]["correct"] == 3 * 15 - 1
    assert metrics["sheets"]["exact_match"] == 2
    assert metrics["worst_errors"][0]["file"] == "sheet1.png"
    assert metrics["worst_errors"][0]["column"] == "q1"

    report = tmp_path / "out" / "report.json"
    benchmark_main(
        [
            "--template",
            str(template_path),
            "--images",
            str(images),
            "--truth",
            str(truth_path),
            "--report",
            str(report),
            "--quiet",
        ]
    )
    saved = json.loads(report.read_text())
    assert saved["fields"]["correct"] == 3 * 15 - 1
    assert saved["config"]["mode"] == "files"


def test_multi_mark_truth_and_review_list_count_as_flagged():
    from src.benchmark.metrics import evaluate_record

    result = {
        "fields": {
            "q1": {"value": "AC", "needs_review": True, "flags": ["multi_marked"]},
            "q2": {"value": "C", "needs_review": False},
            "roll1": {"value": "1", "needs_review": False},
            "roll2": {"value": "", "needs_review": False},
        },
        "zones": {},
        "responses": {"q1": "AC", "q2": "C", "roll": "1"},
        # A validation rule, not a field flag, sends the roll number to review
        "review": [
            {"kind": "custom_label", "name": "roll", "fields": ["roll1", "roll2"]}
        ],
    }
    items = {
        item["column"]: item
        for item in evaluate_record(
            result, {"q1": "*", "q2": "*", "roll": "12"}, {"roll": ["roll1", "roll2"]}
        )
    }
    assert items["q1"]["correct"] and items["q1"]["flagged"]
    assert not items["q2"]["correct"] and not items["q2"]["flagged"]
    assert items["roll"]["flagged"] and not items["roll"]["correct"]
