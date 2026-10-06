"""Dataset export, training and model integration for the crop classifiers."""

import csv
import json
import random

import cv2
import numpy as np
import pytest

from src.ml import dataset as ds
from src.ml.train import (
    classification_metrics,
    expected_calibration_error,
    fit_temperature,
    softmax_np,
    split_indices,
)
from src.synth import default_spec, random_answers, render_sheet
from src.synth.render import ZoneSpec


def icr_spec(questions=8):
    """Small sheet: roll grid, a few MCQs and one ICR zone (fast to read)."""
    spec = default_spec(questions=questions, roll_digits=3, with_zones=False)
    spec.zones = [
        ZoneSpec(
            "candidate_no",
            "icr",
            [140, 230],
            [360, 60],
            {"characterBoxes": 6, "whitelist": "0123456789"},
        )
    ]
    return spec


def write_template(spec, path):
    path.write_text(json.dumps(spec.to_template(pre_processors=[])))
    return path


def render_set(spec, n, seed=0, style="pen"):
    rng = random.Random(seed)
    sheets = []
    for i in range(n):
        answers = random_answers(spec, rng, blank_rate=0.1, multi_rate=0.1)
        image, truth = render_sheet(spec, answers, rng=rng, mark_style=style)
        sheets.append((f"sheet_{i}.png", image, truth))
    return sheets


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "value,values,expected",
    [
        ("", ["A", "B", "C"], set()),
        ("B", ["A", "B", "C"], {1}),
        ("AC", ["A", "B", "C"], {0, 2}),
        ("CA", ["A", "B", "C"], None),  # engine concatenates in template order
        ("X", ["A", "B"], None),
        ("110", ["1", "10", "11"], {0, 1}),
        ("1011", ["1", "10", "11"], {1, 2}),
    ],
)
def test_split_field_value(value, values, expected):
    result = ds.split_field_value(value, values)
    assert (None if result is None else set(result)) == expected


def test_label_dirnames_round_trip():
    for label in [
        "marked",
        "empty",
        "7",
        "a",
        "A",
        ds.BLANK_LABEL,
        "?",
        "/",
        "crossed_out",
    ]:
        name = ds.label_to_dirname(label)
        assert "/" not in name and name
        assert ds.dirname_to_label(name) == label
    assert ds.label_to_dirname("a") != ds.label_to_dirname("A")


def test_load_truth_formats(tmp_path):
    csv_path = tmp_path / "truth.csv"
    csv_path.write_text("file_name,q1,q2,roll\nimgs/a.jpg,A,,123\nb.jpg,BC,D,456\n")
    truth = ds.load_truth(csv_path)
    assert truth["imgs/a.jpg"] == {"q1": "A", "q2": "", "roll": "123"}
    assert ds.lookup_truth(truth, "a.jpg")["roll"] == "123"
    assert ds.lookup_truth(truth, "/scans/b.png")["q1"] == "BC"
    assert ds.lookup_truth(truth, "c.jpg") is None

    json_path = tmp_path / "truth.json"
    json_path.write_text(
        json.dumps({"a.png": {"answers": {"q1": "A"}, "zones": {"id": "X1"}}})
    )
    assert ds.load_truth(json_path)["a.png"] == {"q1": "A", "id": "X1"}
    json_path.write_text(json.dumps([{"file_id": "z.png", "q1": None}]))
    assert ds.load_truth(json_path)["z.png"] == {"q1": ""}


def test_metrics_and_calibration_helpers():
    metrics = classification_metrics(
        [0, 0, 1, 1, 1], [0, 1, 1, 1, 0], ["empty", "marked"]
    )
    assert metrics["accuracy"] == 0.6
    assert metrics["confusion_matrix"]["rows_true_cols_pred"] == [[1, 1], [1, 2]]
    assert metrics["per_class"]["marked"]["precision"] == pytest.approx(2 / 3, abs=1e-4)
    assert metrics["per_class"]["marked"]["recall"] == pytest.approx(2 / 3, abs=1e-4)

    # Overconfident logits with some errors: the fitted temperature softens them
    rng = np.random.default_rng(0)
    targets = rng.integers(0, 2, 2000)
    noisy = np.where(rng.random(2000) < 0.1, 1 - targets, targets)
    logits = np.stack([(noisy == 0) * 8.0, (noisy == 1) * 8.0], axis=1)
    temperature = fit_temperature(logits, targets)
    assert temperature > 1.5
    assert expected_calibration_error(
        softmax_np(logits, temperature), targets
    ) < expected_calibration_error(softmax_np(logits), targets)

    # No validation errors: confidence capped at the (n + 1) / (n + 2) estimate
    perfect = np.stack([(targets == 0) * 30.0, (targets == 1) * 30.0], axis=1)
    temperature = fit_temperature(perfect, targets)
    confidence = softmax_np(perfect, temperature).max(1).mean()
    assert confidence == pytest.approx(2001 / 2002, abs=1e-3)


def test_split_by_source_keeps_sheets_together():
    labels = ["marked", "empty"] * 50
    groups = [f"sheet{i // 10}" for i in range(100)]
    train, val = split_indices(labels, groups, val_fraction=0.2, seed=1)
    assert sorted(train + val) == list(range(100))
    assert not {groups[i] for i in train} & {groups[i] for i in val}
    train, val = split_indices(labels, None, val_fraction=0.2)
    assert len(val) == 20 and sorted(train + val) == list(range(100))


# --------------------------------------------------------------------------- export


def test_export_from_scans_bubbles_and_icr(tmp_path):
    spec = icr_spec()
    template = write_template(spec, tmp_path / "template.json")
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    rows = []
    sheets = render_set(spec, 3)
    for name, image, truth in sheets:
        cv2.imwrite(str(image_dir / name), image)
        rows.append({"file_name": name, **truth["answers"], **truth["zones"]})
    truth_path = tmp_path / "truth.csv"
    with open(truth_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    out = tmp_path / "dataset"
    summary = ds.export_from_scans(template, image_dir, truth_path, out)
    assert summary["sheets"] == 3 and not summary["skipped"]

    bubbles_per_sheet = sum(
        len(b.field_labels) * len(b.bubble_values) for b in spec.blocks
    )
    marks = sum(len(truth["marks"]) for _, _, truth in sheets)
    assert summary["labels"][ds.MARKED] == marks
    assert summary["labels"][ds.EMPTY] == 3 * bubbles_per_sheet - marks
    # Six handwritten digits per sheet
    digit_labels = {k: v for k, v in summary["labels"].items() if k.isdigit()}
    assert sum(digit_labels.values()) == 18

    with open(out / ds.MANIFEST_NAME) as handle:
        manifest = list(csv.DictReader(handle))
    assert len(manifest) == summary["samples"]
    first = manifest[0]
    assert set(ds.MANIFEST_COLUMNS) <= set(first)
    crop = cv2.imread(str(out / first["path"]), cv2.IMREAD_GRAYSCALE)
    assert crop.shape == tuple(spec.bubble[::-1])
    # The threshold reader is right on clean pen sheets, so predictions match labels
    bubble_rows = [r for r in manifest if r["kind"] == ds.KIND_BUBBLE]
    assert all(r["predicted"] == r["label"] for r in bubble_rows)

    images, labels, rows = ds.load_samples(out, (32, 32), kind=ds.KIND_BUBBLE)
    assert images.shape == (len(bubble_rows), 32, 32) and images.dtype == np.uint8
    images, labels, _ = ds.load_samples(out, (32, 32), kind=ds.KIND_ICR)
    assert sorted(set(labels)) == sorted(digit_labels)


def test_export_from_review_uses_only_confirmed_values(tmp_path):
    from src.pipeline import OMREngine

    spec = icr_spec()
    template = write_template(spec, tmp_path / "template.json")
    engine = OMREngine(template)
    _, image, truth = render_set(spec, 1, seed=3)[0]
    result = engine.scan(image, "reviewed.png")
    data = result.to_dict()

    # The operator "corrects" q1 to a different answer and confirms the ICR zone
    wrong_value = "A" if truth["answers"]["q1"] != "A" else "B"
    corrections = {"q1": wrong_value, "candidate_no": truth["zones"]["candidate_no"]}
    samples = ds.export_from_review(
        data,
        corrections,
        result.aligned_image,
        out_dir=tmp_path / "review",
        template_zones=engine.template.zones,
    )
    bubble_samples = [s for s in samples if s.kind == ds.KIND_BUBBLE]
    assert {s.field for s in bubble_samples} == {"q1"}
    assert [s.value for s in bubble_samples if s.label == ds.MARKED] == [wrong_value]
    icr_samples = [s for s in samples if s.kind == ds.KIND_ICR]
    assert "".join(s.label for s in icr_samples) == truth["zones"]["candidate_no"]
    with open(tmp_path / "review" / ds.MANIFEST_NAME) as handle:
        assert len(list(csv.DictReader(handle))) == len(samples)


def test_synthetic_samples_cover_both_classes(tmp_path):
    counts = ds.generate_synthetic(
        tmp_path / "synth", 4, seed=2, max_empty_per_sheet=30, kinds=("bubble", "icr")
    )
    assert counts[ds.MARKED] > 50 and counts[ds.EMPTY] == 4 * 30
    assert any(label.isdigit() for label in counts)
    images, labels, rows = ds.load_samples(tmp_path / "synth", (24, 24), kind="bubble")
    assert images.shape[1:] == (24, 24)
    assert {row["source"] for row in rows} and set(labels) == {ds.MARKED, ds.EMPTY}


# --------------------------------------------------------------------------- runtime


def _write_linear_onnx(path, weights, bias, labels, temperature=None):
    """A tiny ONNX model (flatten + matmul) without needing torch."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    h, w = 4, 4
    graph = helper.make_graph(
        [
            helper.make_node("Flatten", ["input"], ["flat"], axis=1),
            helper.make_node("MatMul", ["flat", "W"], ["scores"]),
            helper.make_node("Add", ["scores", "B"], ["logits"]),
        ],
        "linear",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, ["batch", 1, h, w])],
        [
            helper.make_tensor_value_info(
                "logits", TensorProto.FLOAT, ["batch", len(labels)]
            )
        ],
        [
            numpy_helper.from_array(weights.astype(np.float32), "W"),
            numpy_helper.from_array(bias.astype(np.float32), "B"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(path))
    sidecar = {"labels": labels, "input_size": [w, h]}
    if temperature is not None:
        sidecar["temperature"] = temperature
    path.with_suffix(".json").write_text(json.dumps(sidecar))


def test_onnx_classifier_applies_temperature(tmp_path):
    from src.ml.classifiers import OnnxCropClassifier, load_crop_classifier, softmax

    # marked logit = 8 - sum(pixels): dark crops are "marked", white ones "empty"
    weights = np.zeros((16, 2))
    weights[:, 1] = -1.0
    bias = np.array([0.0, 8.0])
    crops = [np.full((10, 10), 0, np.uint8), np.full((10, 10), 255, np.uint8)]

    _write_linear_onnx(tmp_path / "plain.onnx", weights, bias, ["empty", "marked"])
    plain = load_crop_classifier(tmp_path / "plain.onnx")
    assert plain.temperature == 1.0
    p_plain = plain.predict_proba(crops)
    assert p_plain.shape == (2, 2) and np.allclose(p_plain.sum(1), 1)
    assert p_plain[0].argmax() == 1 and p_plain[1].argmax() == 0

    _write_linear_onnx(
        tmp_path / "hot.onnx", weights, bias, ["empty", "marked"], temperature=4.0
    )
    hot = OnnxCropClassifier(tmp_path / "hot.onnx", threads=1)
    p_hot = hot.predict_proba(crops)
    # Same decisions, softer confidences
    assert (p_hot.argmax(1) == p_plain.argmax(1)).all()
    assert (p_hot.max(1) <= p_plain.max(1) + 1e-6).all()
    logits = np.array([[2.0, 0.0]])
    assert np.allclose(softmax(logits, 2.0), softmax(logits / 2.0))
    assert np.allclose(softmax(logits), softmax(logits, 1.0))


def test_train_export_and_read_sheet_with_model(tmp_path):
    pytest.importorskip("torch")
    pytest.importorskip("onnx")
    from src.ml.classifiers import OnnxCropClassifier
    from src.ml.train import train_from_arrays
    from src.pipeline import OMREngine

    samples = list(ds.synthetic_samples(10, seed=5, max_empty_per_sheet=40))
    images = np.stack([ds.resize_crop(s.image, (32, 32)) for s in samples])
    labels = [s.label for s in samples]
    groups = [s.source for s in samples]
    model_path = tmp_path / "models" / "bubble_model.onnx"
    metadata = train_from_arrays(
        images,
        labels,
        model_path,
        groups=groups,
        epochs=6,
        batch_size=64,
        patience=3,
        verbose=False,
    )
    assert model_path.exists()
    sidecar = json.loads(model_path.with_suffix(".json").read_text())
    assert sidecar["labels"] == ["empty", "marked"]
    assert sidecar["input_size"] == [32, 32]
    assert sidecar["temperature"] > 0
    assert sidecar["dataset"]["split"] == "by source"
    validation = metadata["metrics"]["validation"]
    assert validation["accuracy"] > 0.9
    assert set(validation["per_class"]) == {"empty", "marked"}

    classifier = OnnxCropClassifier(model_path)
    marked = [s.image for s in samples if s.label == ds.MARKED][:20]
    assert classifier.predict_proba(marked)[:, 1].mean() > 0.8

    spec = default_spec(questions=20, with_zones=False)
    template = write_template(spec, tmp_path / "template.json")
    engine = OMREngine(template, bubble_model_path=model_path)
    _, image, truth = render_set(spec, 1, seed=11)[0]
    result = engine.scan(image, "model.png")
    some_bubble = result.fields["q1"]["bubbles"][0]
    assert "model_marked_prob" in some_bubble
    correct = sum(result.responses[k] == v for k, v in truth["answers"].items())
    assert correct / len(truth["answers"]) >= 0.95
