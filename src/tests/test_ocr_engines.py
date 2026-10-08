"""
OCR engines (src/readers/text_reader.py, paddle_ocr.py, image_zone.py).

The real PaddleOCR PP-OCRv5 models are not downloaded in tests: PaddleOCR is
exercised with tiny synthetic ONNX models built here with onnx.helper (a
constant CTC output and a darkness-based probability map), which run through
the same onnxruntime code path as the real models.
"""

import base64
import json

import cv2
import numpy as np
import pytest

from src.readers import base, image_zone, ocr, paddle_ocr, text_reader
from src.readers.base import read_zone
from src.template import Zone

DIGITS = "0123456789"


def text_image(text, width=300, height=60):
    image = np.full((height, width, 3), 255, np.uint8)
    cv2.putText(image, text, (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    return image


def page_with(crop, origin=(50, 20)):
    page = np.full((400, 400, 3), 255, np.uint8)
    x, y = origin
    page[y : y + crop.shape[0], x : x + crop.shape[1]] = crop
    return page


def make_zone(options, zone_type="ocr", origin=(50, 20), dims=(60, 300)):
    return Zone("roll", zone_type, list(origin), list(dims), dict(options))


# ------------------------------------------------------------------ direction
@pytest.mark.parametrize(
    "direction,turn",
    [
        ("horizontal", None),
        ("rot90cw", cv2.ROTATE_90_COUNTERCLOCKWISE),  # text reads bottom to top
        ("rot90ccw", cv2.ROTATE_90_CLOCKWISE),  # text reads top to bottom
        ("rot180", cv2.ROTATE_180),
    ],
)
def test_rotate_crop_makes_text_horizontal(direction, turn):
    upright = text_image("123")
    turned = upright if turn is None else cv2.rotate(upright, turn)
    assert np.array_equal(text_reader.rotate_crop(turned, direction), upright)


class FakeRead:
    """A reader that recognises only an upright crop (by comparing pixels)."""

    def __init__(self, upright, text):
        self.upright, self.text, self.seen = upright, text, []

    def __call__(self, crop):
        self.seen.append(crop.shape[:2])
        ok = crop.shape == self.upright.shape and np.array_equal(crop, self.upright)
        read = text_reader.TextRead(self.text if ok else "", 0.9 if ok else 0.0)
        read.engine = "fake"
        read.valid = ok
        return read, [read], False


def test_auto_direction_keeps_best_valid_read():
    upright = text_image("4821937")
    crop = cv2.rotate(upright, cv2.ROTATE_90_COUNTERCLOCKWISE)
    zone = make_zone({"direction": "auto", "pattern": "[0-9]{7}"})
    reader = FakeRead(upright, "4821937")
    result = text_reader.read_text_crop(zone, crop, {}, reader=reader)
    assert result.value == "4821937"
    assert result.details["direction"] == "rot90cw"
    assert len(reader.seen) == 4  # all four directions tried
    assert len(result.details["reads"]) == 4


def test_fixed_direction_reads_once():
    upright = text_image("42")
    zone = make_zone({"direction": "rot180"})
    reader = FakeRead(upright, "42")
    result = text_reader.read_text_crop(
        zone, cv2.rotate(upright, cv2.ROTATE_180), {}, reader=reader
    )
    assert result.value == "42" and len(reader.seen) == 1


@pytest.mark.skipif(not ocr.tesseract_available(), reason="Tesseract not installed")
def test_vertical_zone_read_with_tesseract():
    upright = text_image("4821937")
    page = page_with(cv2.rotate(upright, cv2.ROTATE_90_COUNTERCLOCKWISE))
    options = {"pattern": "[0-9]{7}", "whitelist": DIGITS}
    plain = read_zone(make_zone(options), page, {"ocr_params": {}})
    assert plain.value != "4821937"
    for direction in ("rot90cw", "auto"):
        zone = make_zone(dict(options, direction=direction))
        result = read_zone(zone, page, {"ocr_params": {}})
        assert result.value == "4821937", direction
        assert result.details["direction"] == "rot90cw"
        assert result.details["char_confidences"]


# ------------------------------------------------------------- user patterns
def test_regex_to_user_patterns():
    assert ocr.regex_to_user_patterns("^[0-9]{7}$") == ["\\d" * 7]
    assert ocr.regex_to_user_patterns("\\d{2,3}") == ["\\d\\d", "\\d\\d\\d"]
    assert ocr.regex_to_user_patterns("[A-Z]\\d+") == ["\\A\\d\\*"]
    assert ocr.regex_to_user_patterns("(a|b)") is None


# --------------------------------------------------- engine choice / fallback
def stub_engines(monkeypatch, reads, available=("tesseract", "paddle")):
    calls = []

    def fake_engine_read(name, crop, zone, settings, min_confidence):
        calls.append(name)
        text, confidence = reads[name]
        read = text_reader.TextRead(text, confidence, [confidence] * len(text), name)
        read.valid = text_reader.is_valid(text, zone)
        return read

    monkeypatch.setattr(text_reader, "engine_read", fake_engine_read)
    monkeypatch.setattr(
        text_reader, "engine_available", lambda name, s, lang=None: name in available
    )
    return calls


def test_no_fallback_by_default(monkeypatch):
    calls = stub_engines(monkeypatch, {"tesseract": ("", 0.0), "paddle": ("12", 0.9)})
    result = read_zone(make_zone({}), page_with(text_image("12")), {})
    assert calls == ["tesseract"]
    assert "empty" in result.flags


def test_fallback_on_empty_read(monkeypatch):
    calls = stub_engines(monkeypatch, {"tesseract": ("", 0.0), "paddle": ("12", 0.9)})
    params = {"ocr_params": {"fallback_engine": "paddle"}}
    result = read_zone(make_zone({}), page_with(text_image("12")), params)
    assert calls == ["tesseract", "paddle"]
    assert result.value == "12" and result.engine == "paddle"
    assert not result.needs_review


def test_fallback_on_invalid_read_and_per_zone_engine(monkeypatch):
    calls = stub_engines(
        monkeypatch, {"tesseract": ("12a", 0.9), "paddle": ("123", 0.8)}
    )
    zone = make_zone({"pattern": "[0-9]{3}", "fallbackEngine": "paddle"})
    result = read_zone(zone, page_with(text_image("123")), {"ocr_params": {}})
    assert calls == ["tesseract", "paddle"]
    assert result.value == "123"
    # Two different non-empty reads: review
    assert "engine_disagree" in result.flags and result.needs_review
    zone = make_zone({"engine": "paddle"})
    calls.clear()
    assert read_zone(zone, page_with(text_image("1")), {}).engine == "paddle"
    assert calls == ["paddle"]


def test_disagreement_review_can_be_switched_off(monkeypatch):
    stub_engines(monkeypatch, {"tesseract": ("12", 0.3), "paddle": ("13", 0.9)})
    params = {"ocr_params": {"fallback_engine": "paddle", "disagree_to_review": False}}
    result = read_zone(make_zone({}), page_with(text_image("13")), params)
    assert result.value == "13" and "engine_disagree" not in result.flags


def test_good_primary_read_skips_fallback(monkeypatch):
    calls = stub_engines(monkeypatch, {"tesseract": ("12", 0.95), "paddle": ("13", 0.9)})
    params = {"ocr_params": {"fallback_engine": "paddle"}}
    result = read_zone(make_zone({}), page_with(text_image("12")), params)
    assert calls == ["tesseract"] and result.value == "12" and not result.flags


def test_low_char_confidence_flag(monkeypatch):
    def fake_engine_read(name, crop, zone, settings, min_confidence):
        read = text_reader.TextRead("123", 0.9, [0.99, 0.4, 0.99], name)
        read.valid = True
        return read

    monkeypatch.setattr(text_reader, "engine_read", fake_engine_read)
    monkeypatch.setattr(text_reader, "engine_available", lambda *a, **k: True)
    zone = make_zone({"minCharConfidence": 0.5})
    result = read_zone(zone, page_with(text_image("123")), {})
    assert "low_char_confidence" in result.flags and result.needs_review
    assert result.details["char_confidences"] == [0.99, 0.4, 0.99]


def test_paddle_unavailable_falls_back_to_tesseract(monkeypatch):
    # onnxruntime failing to import (e.g. Windows 7) disables PaddleOCR cleanly
    paddle_ocr.reset_cache()
    monkeypatch.setattr(paddle_ocr, "_ORT", False)
    monkeypatch.setattr(paddle_ocr, "_ORT_ERROR", "DLL load failed")
    monkeypatch.setattr(ocr, "tesseract_available", lambda: True)
    settings = text_reader.ocr_settings({"default_engine": "paddle"})
    primary, fallback, notes = text_reader.choose_engines(make_zone({}), settings)
    assert primary == "tesseract" and fallback is None
    assert notes["engine_unavailable"] == "paddle"
    assert "onnxruntime" in notes["reason"]
    assert paddle_ocr.paddle_available() is False
    paddle_ocr.reset_cache()


def test_no_engine_at_all(monkeypatch):
    monkeypatch.setattr(text_reader, "engine_available", lambda *a, **k: False)
    result = read_zone(make_zone({}), page_with(text_image("1")), {})
    assert "engine_unavailable" in result.flags and result.needs_review


def test_build_defaults_overlay(tmp_path, monkeypatch):
    from src.readers import ocr_build

    path = tmp_path / "ocr_build.json"
    path.write_text(json.dumps({"ocr_params": {"default_engine": "paddle"}}))
    monkeypatch.setenv("OMR_OCR_BUILD", str(path))
    monkeypatch.setattr(ocr_build, "_CACHE", None)
    try:
        assert text_reader.ocr_settings()["default_engine"] == "paddle"
        # config.json still wins
        assert text_reader.ocr_settings({"default_engine": "tesseract"})[
            "default_engine"
        ] == "tesseract"
    finally:
        ocr_build._CACHE = None


# ------------------------------------------------------- PaddleOCR internals
def test_ctc_decode_merges_repeats_and_blanks():
    chars = ["<blank>"] + list(DIGITS) + [" "]
    seq = [1, 1, 0, 2, 2, 0, 0, 2, 11, 4]  # "0", "1", "1", " ", "3"
    probs = np.full((len(seq), len(chars)), 0.01, np.float32)
    for t, index in enumerate(seq):
        probs[t, index] = 0.9
    text, conf = paddle_ocr.ctc_decode(probs, chars)
    assert text == "011 3" and len(conf) == 5
    # Whitelist masks out other characters
    text, _ = paddle_ocr.ctc_decode(probs, chars, allowed=set("013"))
    assert "2" not in text


def test_db_postprocess_finds_text_box():
    prob = np.zeros((100, 200), np.float32)
    prob[40:60, 30:170] = 0.95
    boxes, scores = paddle_ocr.db_postprocess(prob, scale=(2.0, 2.0))
    assert len(boxes) == 1 and scores[0] > 0.9
    box = boxes[0]
    # In original pixels (scale 2), grown a little beyond the region
    assert box[0][0] < 60 and box[2][0] > 340 and box[0][1] < 80 and box[2][1] > 120


def synthetic_models(directory, text="123"):
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper

    chars = ["<blank>"] + list(DIGITS) + [" "]
    steps = []
    for ch in text:
        steps += [chars.index(ch), 0]
    probs = np.full((1, len(steps), len(chars)), 0.001, np.float32)
    for t, index in enumerate(steps):
        probs[0, t, index] = 0.97
    const = helper.make_tensor("C", TensorProto.FLOAT, probs.shape, probs.ravel())
    zero = helper.make_tensor("Z", TensorProto.FLOAT, [], [0.0])
    rec = helper.make_graph(
        [
            helper.make_node("ReduceMean", ["x"], ["m"], keepdims=0),
            helper.make_node("Mul", ["m", "Z"], ["z"]),
            helper.make_node("Add", ["C", "z"], ["y"]),
        ],
        "rec",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, 48, None])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, None)],
        [const, zero],
    )
    scale = helper.make_tensor("K", TensorProto.FLOAT, [], [-10.0])
    det = helper.make_graph(
        [
            helper.make_node("ReduceMean", ["x"], ["m"], axes=[1], keepdims=1),
            helper.make_node("Mul", ["m", "K"], ["s"]),
            helper.make_node("Sigmoid", ["s"], ["y"]),
        ],
        "det",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 3, None, None])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, None)],
        [scale],
    )
    for graph, name in (
        (rec, "en_PP-OCRv5_mobile_rec.onnx"),
        (det, "PP-OCRv5_mobile_det.onnx"),
    ):
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 8
        onnx.save(model, str(directory / name))
    (directory / "ppocrv5_en_dict.txt").write_text("\n".join(DIGITS) + "\n")
    return directory


@pytest.fixture
def paddle_models(tmp_path):
    pytest.importorskip("onnxruntime")
    synthetic_models(tmp_path)
    paddle_ocr.reset_cache()
    yield tmp_path
    paddle_ocr.reset_cache()


def test_paddle_engine_with_synthetic_models(paddle_models):
    engine = paddle_ocr.get_engine(model_dir=str(paddle_models))
    assert engine.available, engine.unavailable_reason
    assert paddle_ocr.paddle_available(model_dir=str(paddle_models))
    text, confidence, chars, details = engine.read(text_image("123"))
    assert text == "123" and confidence > 0.9 and len(chars) == 3
    # Multi-line: the detection model finds the dark "line" first
    line = np.full((120, 300, 3), 255, np.uint8)
    cv2.rectangle(line, (40, 50), (260, 75), (0, 0, 0), -1)
    text, _, _, details = engine.read(line, single_line=False)
    assert details["detected_boxes"] == 1 and text == "123"


def test_paddle_zone_and_review_on_disagreement(paddle_models, monkeypatch):
    params = {"ocr_params": {"paddle_model_dir": str(paddle_models)}}
    page = page_with(text_image("123"))
    zone = make_zone({"engine": "paddle"}, dims=(300, 60))
    result = read_zone(zone, page, params)
    assert result.value == "123" and result.engine == "paddle"
    # Tesseract first, reading something invalid: Paddle's valid read wins, review
    monkeypatch.setattr(ocr, "tesseract_available", lambda: True)
    monkeypatch.setattr(
        text_reader,
        "tesseract_read",
        lambda crop, zone, s, m, expected_lines=1: _read("128x", 0.5, zone),
    )
    zone = make_zone(
        {"fallbackEngine": "paddle", "pattern": "[0-9]{3}"}, dims=(300, 60)
    )
    result = read_zone(zone, page, params)
    assert result.value == "123" and result.engine == "paddle"
    assert "engine_disagree" in result.flags and result.needs_review


def _read(text, confidence, zone):
    read = text_reader.TextRead(text, confidence, [], "tesseract")
    read.valid = text_reader.is_valid(text, zone)
    return read


def test_icr_paddle_second_reader_flags_disagreement(paddle_models):
    from src.readers import icr

    class Classifier:
        labels = list(DIGITS)

        def predict_proba(self, boxes):
            probs = np.zeros((1, 10))
            probs[0, 7] = 0.99  # always "7"
            return probs

    width = 3 * 40
    crop = np.full((40, width, 3), 255, np.uint8)
    for i, ch in enumerate("123"):
        cv2.putText(crop, ch, (i * 40 + 8, 32), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 3)
    page = page_with(crop)
    zone = make_zone({"characterBoxes": 3}, "icr", dims=(width, 40))
    settings = {"paddle_model_dir": str(paddle_models)}
    result = icr.read_icr_zone(zone, page, Classifier(), settings)
    second = result.details["second_reader"]
    assert second["engine"] == "paddle"
    assert "engine_disagree" in result.flags
    off = icr.read_icr_zone(zone, page, Classifier(), dict(settings, icr_second_reader="none"))
    assert "second_reader" not in off.details


# --------------------------------------------------------------- image zones
def test_image_zone_saves_crop_and_optional_base64(tmp_path):
    page = page_with(text_image("PHOTO"))
    zone = make_zone({"maxSide": 100}, "image", dims=(300, 60))
    zone.name = "photo"
    result = read_zone(zone, page, {})
    assert not result.needs_review
    images = image_zone.attach_zone_images({"photo": result}, [zone], "dir/s01.jpg")
    assert result.value == "s01_photo.png" and list(images) == ["s01_photo.png"]
    assert images["s01_photo.png"].shape[:2] == (20, 100)
    assert "image_base64" not in result.details  # off by default
    written = image_zone.save_zone_images(images, tmp_path / "zones")
    assert written and written[0].is_file()
    assert "crop" not in result.to_dict()

    zone.options = {"embedBase64": True, "saveFilename": "{zone}-{file}.jpg"}
    result = read_zone(zone, page, {})
    image_zone.attach_zone_images({"photo": result}, [zone], "s01.png")
    assert result.value == "photo-s01.jpg"
    uri = result.details["image_base64"]
    assert uri.startswith("data:image/png;base64,")
    decoded = cv2.imdecode(
        np.frombuffer(base64.b64decode(uri.split(",", 1)[1]), np.uint8), 1
    )
    assert decoded.shape[:2] == (60, 300)
    json.dumps(result.to_dict())  # JSON-safe for the DB / API


def test_image_zone_filename_is_safe():
    assert image_zone.zone_filename("../../{file}_{zone}", "a b_p2.png", "z") == "a_b_p2_z.png"
    assert image_zone.zone_filename("{page}_{zone}.png", "x_p3.png", "z") == "3_z.png"
    assert image_zone.zone_filename("{bad}", "s.png", "z") == "s_z.png"


def test_new_review_flags_are_registered():
    assert {"engine_disagree", "low_char_confidence"} <= base.ZONE_REVIEW_FLAGS


def test_image_zone_through_engine_and_worker(tmp_path):
    import random

    from src.api.worker import SAVE_NONE, scan_and_store
    from src.pipeline import OMREngine
    from src.synth import default_spec, random_answers, render_sheet

    spec = default_spec(questions=10)
    template = spec.to_template()
    template["zones"] = {
        "photo": {
            "type": "image",
            "origin": [20, 20],
            "dimensions": [120, 80],
            "options": {"embedBase64": True},
        }
    }
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template))
    engine = OMREngine(path)
    rng = random.Random(3)
    image, _ = render_sheet(spec, random_answers(spec, rng), rng=rng)
    sheet = tmp_path / "s07.png"
    cv2.imwrite(str(sheet), image)

    stored = scan_and_store(engine, sheet, {}, tmp_path / "scans", SAVE_NONE, False)
    zone = stored[0]["zones"]["photo"]
    assert zone["value"] == "s07_photo.png" and not zone["needs_review"]
    assert zone["details"]["image_base64"].startswith("data:image/png;base64,")
    saved = list((tmp_path / "scans").rglob("zones/s07_photo.png"))
    assert len(saved) == 1
    assert cv2.imread(str(saved[0])).shape[:2] == (80, 120)


def test_ocr_api_routes(tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.storage import scan_dir_for, write_json_atomic

    app = create_app(tmp_path / "data", workers=1)
    client = TestClient(app)
    caps = client.get("/ocr/capabilities").json()
    assert set(caps["engines"]) == {"tesseract", "paddle"}
    assert "auto" in caps["directions"]
    assert caps["defaults"]["default_engine"] in ("tesseract", "paddle")
    assert "image" in client.get("/capabilities").json()["zone_types"]

    scan_id = "ab12cd"
    scan_dir = scan_dir_for(tmp_path / "data" / "scans", scan_id)
    (scan_dir / "zones").mkdir(parents=True)
    cv2.imwrite(str(scan_dir / "zones" / "s_photo.png"), np.zeros((5, 5), np.uint8))
    zone = {"type": "image", "value": "s_photo.png", "details": {"file": "s_photo.png"}}
    write_json_atomic(scan_dir / "result.json", {"zones": {"photo": zone}})
    response = client.get(f"/results/{scan_id}/zone-image/photo")
    assert response.status_code == 200 and response.content[:4] == b"\x89PNG"
    assert client.get(f"/results/{scan_id}/zone-image/other").status_code == 404
