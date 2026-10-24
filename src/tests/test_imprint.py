"""Scanner imprint zones (src/readers/imprint.py): finding the dot-matrix line
in a search strip, CTC decoding and the zone result, with a tiny ONNX model
built here that always reads one string."""

import json
import random

import cv2
import numpy as np
import pytest

from src.ml.train_imprint import render_dot_line
from src.readers import imprint
from src.readers.base import read_zone
from src.template import Zone

CHARSET = "S0123456789"


def strip_with(text, seed=1, mark=True):
    rng = random.Random(seed)
    line = render_dot_line(text, rng)
    strip = np.full((line.shape[0] * 3, line.shape[1] + 200), 235, np.uint8)
    x, y = 120, line.shape[0]
    strip[y : y + line.shape[0], x : x + line.shape[1]] = line
    if mark:
        strip[10:40, 5:25] = 0  # a timing mark at the edge of the strip
    return strip, (x, y, line.shape[1], line.shape[0])


def logits_for(text, frames=24):
    """Logits that greedy-decode to text, with blanks between characters."""
    logits = np.full((frames, len(CHARSET) + 1), -5.0, np.float32)
    logits[:, 0] = 5.0
    for i, ch in enumerate(text):
        logits[2 * i + 1, 0] = -5.0
        logits[2 * i + 1, CHARSET.index(ch) + 1] = 6.0
    return logits


@pytest.fixture
def model(tmp_path):
    onnx = pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper

    value = logits_for("S100042")[None]
    graph = helper.make_graph(
        [
            helper.make_node(
                "Constant",
                [],
                ["logits"],
                value=helper.make_tensor(
                    "v", TensorProto.FLOAT, value.shape, value.flatten().tolist()
                ),
            )
        ],
        "imprint",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [None, 1, 32, None])],
        [helper.make_tensor_value_info("logits", TensorProto.FLOAT, None)],
    )
    proto = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    proto.ir_version = 8
    path = tmp_path / "teri_imprint.onnx"
    onnx.save(proto, str(path))
    path.with_suffix(".json").write_text(
        json.dumps({"charset": CHARSET, "blank": 0, "direction": "horizontal"})
    )
    imprint.reset_cache()
    yield path
    imprint.reset_cache()


def test_ctc_greedy_collapses_repeats_and_blanks():
    logits = logits_for("S100")
    logits[2, 0], logits[2, CHARSET.index("S") + 1] = (
        -5.0,
        7.0,
    )  # "S" over two frames: one S
    text, confidences = imprint.ctc_greedy(logits, CHARSET)
    assert text == "S100" and len(confidences) == 4
    assert all(0.9 < c <= 1.0 for c in confidences)
    # Characters outside the allowed set are never chosen
    text, _ = imprint.ctc_greedy(logits_for("S100"), CHARSET, allowed=set("0123456789"))
    assert "S" not in text


def test_locate_line_finds_the_print_and_skips_a_timing_mark():
    strip, (x, y, w, h) = strip_with("S100042")
    box = imprint.locate_line(strip)
    assert box is not None
    bx, by, bw, bh = box
    assert abs(bx - x) < 0.1 * w and abs((bx + bw) - (x + w)) < 0.1 * w
    assert by >= y - 0.2 * h and by + bh <= y + 1.2 * h
    assert imprint.locate_line(np.full((60, 300), 240, np.uint8)) is None


def test_imprint_zone_reads_with_its_model(model):
    strip, _ = strip_with("S100042")
    # Printed top to bottom on the page: the zone turns it back
    page = np.full((600, 400), 240, np.uint8)
    turned = cv2.rotate(strip, cv2.ROTATE_90_CLOCKWISE)
    page[10 : 10 + turned.shape[0], 20 : 20 + turned.shape[1]] = turned
    zone = Zone(
        "imprint_no",
        "imprint",
        [20, 10],
        [turned.shape[1], turned.shape[0]],
        {"model": str(model), "direction": "rot90ccw", "pattern": r"S\d{6}"},
    )
    result = read_zone(zone, page)
    assert result.value == "S100042" and result.engine == "imprint"
    assert not result.needs_review and result.confidence > 0.9
    assert (
        result.details["direction"] == "rot90ccw" and len(result.details["line"]) == 4
    )


def test_imprint_zone_without_a_model_goes_to_review(tmp_path, monkeypatch):
    monkeypatch.setattr(imprint, "model_dirs", lambda: [tmp_path])
    strip, _ = strip_with("S100042")
    zone = Zone("imprint_no", "imprint", [0, 0], [strip.shape[1], strip.shape[0]], {})
    result = read_zone(zone, strip)
    assert "no_imprint_model" in result.flags and result.needs_review
    assert imprint.installed_models() == []


def test_installed_models_lists_models_with_a_sidecar(model, monkeypatch):
    monkeypatch.setattr(imprint, "model_dirs", lambda: [model.parent])
    assert imprint.installed_models() == ["teri_imprint"]
    assert imprint.find_model("teri_imprint") == model


def test_a_line_split_by_a_faint_character_is_also_tried_whole():
    strip, (x, y, w, h) = strip_with("S100042", mark=False)
    # Blank out the middle character: two level pieces with a wide gap
    third = x + 3 * w // 7
    strip[:, third : third + w // 7 + 12] = 235
    boxes = imprint.line_candidates(strip, char_height=h * 0.6)
    assert any(bx <= x + 0.1 * w and bx + bw >= x + 0.9 * w for bx, by, bw, bh in boxes)


def test_file_name_check_flags_a_different_number():
    from src.readers.base import ZoneReadResult, check_file_names, finalize

    zone = Zone(
        "imprint_no",
        "imprint",
        [0, 0],
        [10, 10],
        {"matchFileName": True, "pattern": r"S\d{6}"},
    )

    def read(value, file_id):
        result = finalize(ZoneReadResult(zone.name, zone.type, value, 0.99, []), zone)
        return check_file_names({zone.name: result}, [zone], file_id)[zone.name]

    same = read("S100001", "batch/100001.jpg")
    assert "file_name_mismatch" not in same.flags and not same.needs_review
    assert same.details["file_name"] == {"expected": "100001", "read": "100001"}
    other = read("S100007", "100001.jpg")
    assert "file_name_mismatch" in other.flags and other.needs_review
    # An empty or malformed read cannot confirm the file name: flagged too
    assert "file_name_mismatch" in read("3100001", "100001.jpg").flags
    empty = read("", "100001.jpg")
    assert (
        "file_name_mismatch" in empty.flags and empty.details["file_name"]["read"] == ""
    )
    # PDF pages: the page suffix is not part of the name
    assert "file_name_mismatch" not in read("S100001", "100001_p1.png").flags
    zone.options["matchFileName"] = False
    assert "file_name_mismatch" not in read("S100007", "100001.jpg").flags


def test_directions_follow_the_model_and_the_strip_shape():
    class Model:
        direction = "rot90ccw"

    tall = np.zeros((1000, 60), np.uint8)
    wide = np.zeros((60, 1000), np.uint8)
    # Unset: the model's own
    assert imprint.imprint_directions({}, Model(), tall) == ["rot90ccw"]
    # A tall strip left at "horizontal" (an editor default) is tried turned first
    tried = imprint.imprint_directions({"direction": "horizontal"}, Model(), tall)
    assert tried[0] == "rot90ccw" and set(tried) == {
        "rot90ccw",
        "horizontal",
        "rot90cw",
    }
    assert imprint.imprint_directions({"direction": "horizontal"}, Model(), wide) == [
        "horizontal"
    ]
    assert len(imprint.imprint_directions({"direction": "auto"}, Model(), wide)) == 3


def test_a_tall_strip_set_to_horizontal_still_reads(model):
    meta = json.loads(model.with_suffix(".json").read_text())
    meta.update(direction="rot90ccw", pattern=r"S\d{6}", char_height=None)
    model.with_suffix(".json").write_text(json.dumps(meta))
    imprint.reset_cache()
    strip, _ = strip_with("S100042")
    page = np.full((600, 400), 240, np.uint8)
    turned = cv2.rotate(strip, cv2.ROTATE_90_CLOCKWISE)
    page[10 : 10 + turned.shape[0], 20 : 20 + turned.shape[1]] = turned
    zone = Zone(
        "imprint_no",
        "imprint",
        [20, 10],
        [turned.shape[1], turned.shape[0]],
        {"model": str(model), "direction": "horizontal"},
    )
    result = read_zone(zone, page)
    assert result.value == "S100042" and result.details["direction"] == "rot90ccw"
    assert "pattern_mismatch" not in result.flags
