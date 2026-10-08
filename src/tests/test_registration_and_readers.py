import json
import random

import cv2
import numpy as np
import pytest

from src.pipeline import STATUS_ERROR, OMREngine
from src.readers import barcode, ocr
from src.synth import augment, default_spec, random_answers, render_sheet
from src.template import Zone


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=40)


def make_engine(tmp_path, template):
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template))
    return OMREngine(path)


def field_errors(result, answers):
    return {
        label: (expected, result.fields[label]["value"])
        for label, expected in answers.items()
        if result.fields[label]["value"] != expected
    }


@pytest.mark.parametrize("early_stop", [False, True])
@pytest.mark.parametrize(
    "seed,flip,rotation,perspective",
    [(1, False, 3, 0.03), (2, True, 3, 0.03), (3, False, 8, 0.06)],
)
def test_timing_marks_register_skewed_and_flipped_sheets(
    tmp_path, spec, seed, flip, rotation, perspective, early_stop
):
    template = spec.to_template()
    for step in template["preProcessors"]:
        if step["name"] == "TimingMarkAlignment":
            step["options"]["earlyStop"] = early_stop
    engine = make_engine(tmp_path, template)
    rng = random.Random(seed)
    answers = random_answers(spec, rng)
    image, _ = render_sheet(spec, answers, rng=rng, mark_style="mixed", erasures=2)
    captured, _ = augment(
        image, rng, rotation=rotation, perspective=perspective, flip_180=flip
    )

    result = engine.scan(captured, "sheet")

    registration = engine.template.pre_processors[0].last_registration
    assert result.status != STATUS_ERROR
    assert registration["orientation"] == (180 if flip else 0)
    assert registration["residual_px"] < 1.0
    assert field_errors(result, answers) == {}


@pytest.mark.parametrize("angle,shift_pitches", [(3.0, 1.0), (-2.5, -0.8), (0.8, 1.0)])
def test_timing_marks_do_not_slip_a_mark_on_flush_tilted_scans(
    tmp_path, spec, angle, shift_pitches
):
    # A page filling the whole scan gives no outline, so the coarse guess
    # misses the tilt and offset; a track could lock onto its neighbour mark
    template = spec.to_template()
    engine = make_engine(tmp_path, template)
    rng = random.Random(7)
    answers = random_answers(spec, rng)
    image, _ = render_sheet(spec, answers, rng=rng)
    h, w = image.shape[:2]
    marks = np.array(template["preProcessors"][0]["options"]["tracks"]["left"]["marks"])
    pitch = float(np.linalg.norm(marks[1] - marks[0]))
    move = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    move[1, 2] += shift_pitches * pitch
    captured = cv2.warpAffine(image, move, (w, h), borderValue=255)

    result = engine.scan(captured, "sheet")

    registration = engine.template.pre_processors[0].last_registration
    assert result.status != STATUS_ERROR
    assert registration["matched_marks"] >= registration["expected_marks"] - 2
    assert field_errors(result, answers) == {}


def test_timing_marks_non_rigid_refinement(tmp_path, spec):
    template = spec.to_template()
    template["preProcessors"][0]["options"]["nonRigid"] = True
    engine = make_engine(tmp_path, template)
    rng = random.Random(4)
    answers = random_answers(spec, rng)
    image, _ = render_sheet(spec, answers, rng=rng)
    captured, _ = augment(image, rng, rotation=2, perspective=0.02)

    assert field_errors(engine.scan(captured, "sheet"), answers) == {}


def test_registration_failure_is_reported_not_guessed(tmp_path, spec):
    engine = make_engine(tmp_path, spec.to_template())
    blank = np.full((1800, 1300), 255, np.uint8)

    result = engine.scan(blank, "blank")

    assert result.status == STATUS_ERROR
    assert "registration" in result.error.lower()


def test_ecc_alignment_removes_small_offsets(tmp_path, spec):
    rng = random.Random(5)
    blank, _ = render_sheet(spec, {}, rng=rng)
    cv2.imwrite(str(tmp_path / "reference.png"), blank)
    template = spec.to_template(
        pre_processors=[
            {
                "name": "EccAlignment",
                "options": {"reference": "reference.png", "motion": "affine"},
            }
        ]
    )
    engine = make_engine(tmp_path, template)
    answers = random_answers(spec, rng)
    image, _ = render_sheet(spec, answers, rng=rng)
    matrix = cv2.getRotationMatrix2D((620, 877), 0.6, 1.0)
    matrix[:, 2] += (9, -7)
    shifted = cv2.warpAffine(image, matrix, image.shape[::-1], borderValue=255)

    result = engine.scan(shifted, "shifted")

    assert engine.template.pre_processors[0].last_correlation > 0.8
    assert field_errors(result, answers) == {}


def test_blank_question_is_not_read_as_all_marked(tmp_path, spec):
    engine = make_engine(tmp_path, spec.to_template(pre_processors=[]))
    rng = random.Random(6)
    answers = random_answers(spec, rng)
    answers["q4"] = ""
    image, _ = render_sheet(spec, answers, rng=rng, mark_style="pencil")

    result = engine.scan(image, "sheet")

    assert result.fields["q4"]["value"] == ""
    assert "empty" in result.fields["q4"]["flags"]


def test_multi_marked_field_is_sent_to_review(tmp_path, spec):
    engine = make_engine(tmp_path, spec.to_template(pre_processors=[]))
    rng = random.Random(7)
    answers = random_answers(spec, rng)
    answers["q2"] = "AC"
    image, _ = render_sheet(spec, answers, rng=rng)

    result = engine.scan(image, "sheet")

    assert result.fields["q2"]["value"] == "AC"
    assert result.fields["q2"]["needs_review"]
    assert {"kind": "field", "name": "q2", "flags": ["multi_marked"]} in result.review


@pytest.mark.parametrize(
    "fmt,value",
    [
        ("Code128", "ABC-12345-xyz"),
        ("Code39", "CODE39TEST"),
        ("EAN13", "5901234123457"),
        ("UPCA", "036000291452"),
        ("ITF", "12345678901231"),
        ("Codabar", "A123456A"),
        ("QRCode", "https://example.com/sheet/42"),
        ("DataMatrix", "DM-0042"),
        ("PDF417", "PDF417 sample"),
        ("Aztec", "AZTEC-42"),
        ("MicroQRCode", "12345"),
    ],
)
def test_barcode_zone_reads_symbology(fmt, value):
    from src.readers.zxing_compat import apply as apply_zxing_compat

    if fmt in ("Codabar", "MicroQRCode") and apply_zxing_compat.patched:
        # zxing-cpp 2.2 (the Python 3.8 / Windows 7 build) can't write Micro QR
        # and drops Codabar start/stop characters
        pytest.skip("needs zxing-cpp >= 2.3")
    zxingcpp = pytest.importorskip("zxingcpp")
    symbol = zxingcpp.create_barcode(value, zxingcpp.barcode_format_from_str(fmt))
    code = np.array(zxingcpp.write_barcode_to_image(symbol, scale=4))
    page = np.full((code.shape[0] + 80, code.shape[1] + 80), 255, np.uint8)
    page[40 : 40 + code.shape[0], 40 : 40 + code.shape[1]] = code
    zone = Zone("id", "barcode", [20, 20], [code.shape[1] + 40, code.shape[0] + 40], {})

    result = barcode.read_barcode_zone(zone, page)

    # UPC-A is a subset of EAN-13 and may be reported with its leading zero
    assert result.value == value or result.value == "0" + value
    assert result.confidence == 1.0
    assert result.flags == []


def test_barcode_zone_format_restriction_rejects_other_symbologies():
    zxingcpp = pytest.importorskip("zxingcpp")
    symbol = zxingcpp.create_barcode("NOT-128", zxingcpp.BarcodeFormat.Code39)
    code = np.array(zxingcpp.write_barcode_to_image(symbol, scale=4))
    page = np.full((code.shape[0] + 40, code.shape[1] + 40), 255, np.uint8)
    page[20 : 20 + code.shape[0], 20 : 20 + code.shape[1]] = code
    zone = Zone(
        "id",
        "barcode",
        [0, 0],
        [page.shape[1], page.shape[0]],
        {"formats": ["Code128"]},
    )

    assert barcode.read_barcode_zone(zone, page).flags == ["not_found"]


@pytest.mark.skipif(not ocr.tesseract_available(), reason="tesseract not installed")
def test_ocr_zone_reads_printed_text(tmp_path, spec):
    engine = make_engine(tmp_path, spec.to_template(pre_processors=[]))
    rng = random.Random(8)
    image, truth = render_sheet(
        spec, {}, zone_values={"exam_code": "EXAM 4821"}, rng=rng
    )

    result = engine.scan(image, "sheet")

    assert result.zones["exam_code"]["value"] == "EXAM 4821"
    assert result.zones["sheet_id"]["value"] == truth["zones"]["sheet_id"]
    assert result.zones["qr"]["value"] == truth["zones"]["qr"]
    # Without a trained ICR model handwriting is always routed to review
    assert result.zones["candidate_no"]["needs_review"]
    assert "no_icr_model" in result.zones["candidate_no"]["flags"]


def test_marker_quadrilateral_sanity_check():
    from src.processors.CropOnMarkers import CropOnMarkers

    shape = (1000, 800)
    good = np.array([[50, 50], [750, 60], [740, 950], [60, 940]])
    # One false match near the centre collapses the page
    bad = np.array([[50, 50], [750, 60], [400, 500], [60, 940]])

    assert CropOnMarkers.is_plausible_quadrilateral(good, shape)
    assert not CropOnMarkers.is_plausible_quadrilateral(bad, shape)


@pytest.mark.parametrize("snap_radius,expect_correct", [(0, False), (20, True)])
def test_block_snap_recovers_locally_offset_blocks(
    tmp_path, spec, snap_radius, expect_correct
):
    template = spec.to_template(pre_processors=[])
    # The printed form drifted relative to the template for one block
    block = template["fieldBlocks"]["MCQ_1"]
    block["origin"] = [block["origin"][0] + 16, block["origin"][1] - 14]
    (tmp_path / "config.json").write_text(
        json.dumps({"alignment_params": {"block_snap_radius": snap_radius}})
    )
    engine = make_engine(tmp_path, template)
    rng = random.Random(9)
    answers = random_answers(spec, rng)
    image, _ = render_sheet(spec, answers, rng=rng, mark_style="mixed")

    result = engine.scan(image, "sheet")

    mcq_1 = spec.blocks[1].field_labels
    errors = {k: v for k, v in field_errors(result, answers).items() if k in mcq_1}
    assert (errors == {}) == expect_correct


def test_crop_page_finds_white_sheet_on_light_background(tmp_path, spec):
    rng = random.Random(10)
    answers = random_answers(spec, rng)
    image, _ = render_sheet(spec, answers, rng=rng)
    small = cv2.resize(image, (620, 877))
    # A white sheet on a light desk: the fixed truncation at 200 erases the edge
    canvas = np.full((1100, 860), 215, np.uint8)
    canvas[100:977, 120:740] = small
    template = spec.to_template(
        pre_processors=[{"name": "CropPage", "options": {"morphKernel": [10, 10]}}]
    )
    engine = make_engine(tmp_path, template)

    result = engine.scan(canvas, "light-desk")

    assert result.status != STATUS_ERROR
    assert field_errors(result, answers) == {}
