"""Colour dropout, the palette tool and colour-aware loading through every entry point."""

import json
import random
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.color import (
    GREY,
    analyse_sheet,
    apply_dropout,
    color_match_weight,
    dropout_to_json,
    extract_palette,
    label_guess,
    normalize_dropout,
    parse_hex,
    suggest_dropout,
    to_hex,
)
from src.core import ImageInstanceOps
from src.pipeline import OMREngine
from src.synth import augment, default_spec, random_answers, render_sheet

PINK = "#E8618C"
BLUE_PEN = "#1F3C9A"
SCAN = dict(
    rotation=1.0,
    perspective=0.005,
    blur=1.0,
    noise=4.0,
    shadow=0.15,
    jpeg_quality=(80, 95),
)


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=20, roll_digits=4, with_zones=False)


def write_template(directory, spec, register=False, **extra):
    template = spec.to_template(pre_processors=None if register else [])
    template.update(extra)
    path = Path(directory, "template.json")
    path.write_text(json.dumps(template))
    return path


def colour_sheet(spec, seed, print_color=PINK, ink_color=BLUE_PEN, scan=False):
    rng = random.Random(seed)
    answers = random_answers(spec, rng, blank_rate=0.0)
    image, truth = render_sheet(
        spec,
        answers,
        rng=rng,
        mark_style="pen",
        print_color=print_color,
        ink_color=ink_color,
    )
    if scan:
        image, _ = augment(image, rng, **SCAN)
    return image, truth


def assert_reads(result, truth):
    assert result.status != "error", result.error
    for label, answer in truth["answers"].items():
        assert result.responses[label] == answer, label


# --------------------------------------------------------------------------- specs


def test_normalize_dropout_variants():
    assert normalize_dropout(None) is GREY
    assert normalize_dropout("grey") is GREY
    assert normalize_dropout("gray") is GREY
    assert normalize_dropout({"mode": "red", "strength": 0}) is GREY
    assert normalize_dropout({"mode": "color", "color": PINK, "tolerance": 0}) is GREY
    red = normalize_dropout({"mode": "red"})
    assert red.mode == "red" and red.strength == 1.0
    assert normalize_dropout("red") == red  # hashable and comparable
    colour = normalize_dropout({"mode": "color", "color": "#e86", "tolerance": 30})
    assert colour.color == parse_hex("#EE8866") and colour.tolerance == 30
    assert dropout_to_json(colour) == {
        "mode": "color",
        "color": "#EE8866",
        "tolerance": 30.0,
        "strength": 1.0,
    }
    with pytest.raises(ValueError):
        normalize_dropout({"mode": "purple"})
    with pytest.raises(ValueError):
        normalize_dropout({"mode": "color"})
    assert to_hex(parse_hex(PINK)) == PINK


def swatch(*hexes):
    return np.uint8([[parse_hex(h) for h in hexes]])


def test_channel_and_max_modes():
    strip = swatch(PINK, BLUE_PEN, "#202020", "#FFFFFF")
    grey = apply_dropout(strip, "grey")[0]
    red = apply_dropout(strip, "red")[0]
    maximum = apply_dropout(strip, "max")[0]
    assert grey[0] < 160  # pink is mid-grey without dropout
    assert red[0] > 220 and red[1] < 60 and red[2] < 40 and red[3] == 255
    # max: every saturated colour goes light, black stays dark
    assert maximum[0] > 220 and maximum[1] > 140 and maximum[2] < 40
    # A grey image has no colour to drop and is used as-is
    plain = np.full((4, 4), 77, np.uint8)
    assert apply_dropout(plain, "red") is plain
    # BGRA input is accepted
    bgra = cv2.cvtColor(strip, cv2.COLOR_BGR2BGRA)
    assert (apply_dropout(bgra, "red") == apply_dropout(strip, "red")).all()


def test_strength_blends_with_grey():
    strip = swatch(PINK)
    grey = int(apply_dropout(strip, "grey")[0, 0])
    full = int(apply_dropout(strip, {"mode": "red"})[0, 0])
    half = int(apply_dropout(strip, {"mode": "red", "strength": 0.5})[0, 0])
    assert abs(half - (grey + full) / 2) <= 1


def test_color_mode_soft_falloff_and_reference():
    spec = {"mode": "color", "color": PINK, "tolerance": 40}
    near = (140, 97, 232)  # pink, slightly bluer
    strip = np.uint8([[parse_hex(PINK), near, parse_hex(BLUE_PEN), (30, 30, 30)]])
    out = apply_dropout(strip, spec)[0]
    assert out[0] == 255 and out[1] >= 250
    assert out[2] < 70 and out[3] < 40  # pens untouched

    # The 32-level colour table matches a direct per-pixel Lab computation
    rng = np.random.default_rng(0)
    pixels = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    weights = color_match_weight(pixels, parse_hex(PINK), 40).astype(np.float32)
    lab = cv2.cvtColor(pixels.astype(np.float32) / 255, cv2.COLOR_BGR2Lab)
    target = cv2.cvtColor(np.float32([[parse_hex(PINK)]]) / 255, cv2.COLOR_BGR2Lab)
    delta = lab - target[0, 0]
    delta[..., 0] *= 0.5
    distance = np.sqrt((delta**2).sum(axis=2))
    expected = np.clip((60 - distance) / 20, 0, 1) * 255
    assert np.abs(weights - expected).mean() < 6
    # Soft falloff: partial weights exist between tolerance and 1.5x tolerance
    ramp = np.uint8([[[140 - i * 3, 97 - i, 232 - i * 4] for i in range(40)]])
    ramp_weights = color_match_weight(ramp, parse_hex(PINK), 20)[0]
    assert ((ramp_weights > 0) & (ramp_weights < 255)).any()


# --------------------------------------------------------------------------- palette


def test_palette_and_suggestion():
    image = np.full((400, 600, 3), 250, np.uint8)
    for y in range(20, 380, 20):
        cv2.line(image, (20, y), (580, y), parse_hex(PINK), 3)
    cv2.rectangle(image, (100, 100), (200, 200), (25, 25, 25), -1)
    cv2.rectangle(image, (300, 100), (400, 200), parse_hex(BLUE_PEN), -1)
    palette = extract_palette(image, k=6)
    assert 0.6 < palette["paper_share"] < 0.95
    labels = {c["label_guess"] for c in palette["colors"]}
    assert {"pink", "black ink", "blue"} <= labels
    for entry in palette["colors"]:
        assert entry["hex"].startswith("#") and 0 < entry["share"] < 1
    with_paper = extract_palette(image, ignore_paper=False)
    assert any(c["label_guess"] == "paper" for c in with_paper["colors"])

    analysed = analyse_sheet(image)
    pink = next(c for c in analysed["colors"] if c["label_guess"] == "pink")
    assert pink["suggestion"]["settings"]["mode"] == "red"
    assert pink["suggestion"]["good"]
    black = next(c for c in analysed["colors"] if c["label_guess"] == "black ink")
    assert "suggestion" not in black

    # Grey input works too
    assert extract_palette(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))["colors"]


def test_suggest_dropout_choices():
    pink = suggest_dropout(PINK)
    assert pink["settings"] == {"mode": "red", "strength": 1.0}
    assert pink["good"] and pink["target_after"] > 200
    assert max(pink["keep_after"].values()) < 120
    assert [c["settings"]["mode"] for c in pink["candidates"]][0] == "red"
    # Green print can't be removed by a channel while keeping blue pens dark
    green = suggest_dropout("#40B060")
    assert green["settings"]["mode"] == "color" and green["good"]
    assert green["settings"]["tolerance"] > 0
    # Grey print looks like pencil: no setting separates them
    assert not suggest_dropout("#909090")["good"]
    # Custom keep colours
    custom = suggest_dropout(BLUE_PEN, keep=["#202020"])
    assert custom["good"] and set(custom["keep_after"]) == {"#202020"}


def test_label_guess():
    assert label_guess(parse_hex("#FAFAFA")) == "paper"
    assert label_guess(parse_hex("#151515")) == "black ink"
    assert label_guess(parse_hex("#707070")) == "pencil / grey"
    assert label_guess(parse_hex(PINK)) == "pink"
    assert label_guess(parse_hex("#C01818")) == "red"
    assert label_guess(parse_hex(BLUE_PEN)) == "blue"
    assert label_guess(parse_hex("#30A050")) == "green"
    assert label_guess(parse_hex("#F0C020")) == "yellow"


# --------------------------------------------------------------------------- engine


def test_grey_templates_unchanged(tmp_path, spec):
    path = write_template(tmp_path, spec)
    engine = OMREngine(path)
    assert not engine.needs_color
    image, truth = colour_sheet(spec, 1)
    from_colour = engine.scan(image, "colour").to_dict()
    from_grey = engine.scan(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), "grey").to_dict()
    for key in ("responses", "fields", "review", "thresholds"):
        assert from_colour[key] == from_grey[key]
    assert "dropout" in from_colour["timings_ms"]
    assert "dropout" not in from_grey["timings_ms"]


def test_red_dropout_reads_colour_sheet(tmp_path, spec):
    path = write_template(tmp_path, spec, register=True, colorDropout={"mode": "red"})
    engine = OMREngine(path)
    assert engine.needs_color
    for seed in range(2):
        image, truth = colour_sheet(spec, seed, scan=True)
        result = engine.scan(image, f"s{seed}")
        assert_reads(result, truth)
    # The aligned image no longer shows the pink print: empty bubbles are white
    empty = [b for f in result.fields.values() for b in f["bubbles"] if not b["marked"]]
    assert np.median([b["mean_intensity"] for b in empty]) > 225


def test_pen_matching_print_and_too_few_marks(tmp_path, spec):
    image, truth = colour_sheet(spec, 4, ink_color=PINK)
    path = write_template(tmp_path, spec, colorDropout={"mode": "red"})
    # The red channel removes the pink pen along with the print
    result = OMREngine(path).scan(image)
    assert not any(item["kind"] == "sheet" for item in result.review)
    flagged = OMREngine(
        path, config_overrides={"review_params": {"min_marked_bubbles": 5}}
    ).scan(image)
    sheet_items = [item for item in flagged.review if item["kind"] == "sheet"]
    assert sheet_items and sheet_items[0]["flags"] == ["too_few_marks"]
    assert sheet_items[0]["marked_bubbles"] < 5
    assert flagged.status == "needs_review"

    # Regrade with colour removal off, without rewriting the template
    regraded = OMREngine(
        path,
        template_overrides={"colorDropout": "grey"},
        config_overrides={"review_params": {"min_marked_bubbles": 5}},
    )
    assert not regraded.needs_color
    result = regraded.scan(image)
    assert_reads(result, truth)
    assert not any(item["kind"] == "sheet" for item in result.review)
    # None removes the key; set_color_dropout changes a live engine
    assert not OMREngine(path, template_overrides={"colorDropout": None}).needs_color
    live = OMREngine(path)
    live.set_color_dropout(None)
    assert_reads(live.scan(image), truth)
    with pytest.raises(Exception):
        OMREngine(path, template_overrides={"colorDropout": {"mode": "purple"}})


def tint_region(image, box, color):
    """Reprint the dark parts of box in `color` (multiply blend)."""
    x, y, w, h = box
    region = image[y : y + h, x : x + w].astype(np.float32)
    darkness = 1.0 - region.mean(axis=2, keepdims=True) / 255.0
    tint = np.float32(parse_hex(color)) / 255.0
    image[y : y + h, x : x + w] = np.clip(
        255.0 * (1.0 - darkness * (1.0 - tint)), 0, 255
    ).astype(np.uint8)


def test_zone_override_reads_its_own_variant(tmp_path):
    pytest.importorskip("zxingcpp")
    spec = default_spec(questions=20, roll_digits=4, with_zones=True)
    spec.zones = [z for z in spec.zones if z.type == "barcode"]
    rng = random.Random(7)
    answers = random_answers(spec, rng, blank_rate=0.0)
    image, truth = render_sheet(
        spec, answers, rng=rng, print_color=PINK, ink_color=BLUE_PEN
    )
    # The barcode is printed in the same pink as the form
    zone = spec.zones[0]
    tint_region(image, (*zone.origin, *zone.dimensions), PINK)
    image, _ = augment(image, rng, **SCAN)
    barcode = truth["zones"][zone.name]

    remove_pink = {"mode": "color", "color": PINK, "tolerance": 50}
    plain = write_template(tmp_path, spec, register=True, colorDropout=remove_pink)
    result = OMREngine(plain).scan(image)
    assert_reads(result, truth)
    assert result.zones[zone.name]["value"] != barcode  # dropped with the print

    template = json.loads(plain.read_text())
    template["zones"][zone.name]["options"]["colorDropout"] = "grey"
    plain.write_text(json.dumps(template))
    engine = OMREngine(plain)
    assert engine.needs_color
    calls = []
    original = engine.template.prepare_image

    def counting(image):
        grey, variants = original(image)
        calls.append(len(variants))
        return grey, variants

    engine.template.prepare_image = counting
    result = engine.scan(image)
    assert_reads(result, truth)
    assert result.zones[zone.name]["value"] == barcode
    assert calls == [1]

    # A grey input has no variants: zones read the page image
    grey_result = engine.scan(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    assert calls == [1] and grey_result.status != "error"
    assert engine.template.prepare_image(np.zeros((4, 4), np.uint8))[1] == {}


def test_zone_variant_needs_color_rules(tmp_path, spec):
    path = write_template(tmp_path, spec)
    template = json.loads(path.read_text())
    template["zones"] = {
        "code": {
            "type": "barcode",
            "origin": [700, 110],
            "dimensions": [400, 100],
            "options": {"colorDropout": "grey"},
        }
    }
    path.write_text(json.dumps(template))
    # grey zone on a grey page needs nothing extra
    assert not OMREngine(path).needs_color
    template["zones"]["code"]["options"]["colorDropout"] = {"mode": "blue"}
    path.write_text(json.dumps(template))
    engine = OMREngine(path)
    assert engine.needs_color
    assert [s.mode for s in engine.template.zone_dropout_specs()] == ["blue"]


class _FakeStep:
    needs_full_resolution = True

    def __init__(self, ops, geometry):
        self.ops = ops
        self.geometry = geometry
        self.calls = 0

    def apply_filter(self, image, _name):
        self.calls += 1
        if self.geometry == "recorded":
            transform = lambda im: np.ascontiguousarray(im[:, ::-1])  # noqa: E731
            if self.ops.geometry_ops is not None:
                self.ops.geometry_ops.append(transform)
            return transform(image)
        if self.geometry == "none":
            return image + 1
        return np.ascontiguousarray(image.T)


def test_companions_follow_geometry():
    from dotmap import DotMap

    from src.defaults import CONFIG_DEFAULTS

    ops = ImageInstanceOps(DotMap(CONFIG_DEFAULTS.toDict(), _dynamic=False))
    steps = [
        _FakeStep(ops, "recorded"),
        _FakeStep(ops, "none"),
        _FakeStep(ops, "unknown"),
    ]
    template = type("T", (), {"pre_processors": steps})()
    page = np.arange(12, dtype=np.uint8).reshape(3, 4)
    companion = page + 100
    companions = {"variant": companion.copy()}
    out = ops.apply_preprocessors("x", page, template, companions)
    expected = (page[:, ::-1] + 1).T
    assert (out == expected).all()
    # Mirrored (recorded), not brightened (filter only), transposed (re-run)
    assert (companions["variant"] == companion[:, ::-1].T).all()
    assert steps[2].calls == 2 and steps[1].calls == 1
    assert ops.geometry_ops is None
    # Without companions nothing is recorded or replayed
    ops.apply_preprocessors("x", page, template)
    assert ops.geometry_ops is None


# --------------------------------------------------------------------------- loading


def test_scan_path_png_and_pdf_in_colour(tmp_path, spec):
    fitz = pytest.importorskip("fitz")
    image, truth = colour_sheet(spec, 9, ink_color=PINK)
    png = tmp_path / "sheet.png"
    cv2.imwrite(str(png), image)
    pdf = tmp_path / "sheet.pdf"
    document = fitz.open()
    page = document.new_page(
        width=image.shape[1] * 72 / 150, height=image.shape[0] * 72 / 150
    )
    page.insert_image(page.rect, filename=str(png))
    document.save(str(pdf))
    document.close()

    # Pen and print share a colour: the red channel would lose the marks, so a
    # correct read proves the file was decoded in colour with this dropout
    regrade = write_template(tmp_path, spec, colorDropout={"mode": "red"})
    config = {"review_params": {"min_marked_bubbles": 5}}
    for path in (png, pdf):
        dropped = OMREngine(regrade, config_overrides=config).scan_path(path)[0]
        assert any(i["kind"] == "sheet" for i in dropped.review), path
    grey_engine = OMREngine(regrade, template_overrides={"colorDropout": "grey"})
    for path in (png, pdf):
        assert_reads(grey_engine.scan_path(path)[0], truth)

    from src.utils.image import ImageUtils

    config = OMREngine(regrade).tuning_config
    assert ImageUtils.load_omr_image(png, config)[0][1].ndim == 2
    assert ImageUtils.load_omr_image(png, config, color=True)[0][1].ndim == 3
    pages = ImageUtils.load_omr_image(pdf, config, color=True)
    assert pages[0][1].ndim == 3 and pages[0][1].shape[2] == 3


def test_batch_and_worker_honour_dropout(tmp_path, spec):
    from src.api.worker import SAVE_NONE, scan_and_store
    from src.batch import scan_files

    image, truth = colour_sheet(spec, 11)
    path = tmp_path / "sheet.png"
    cv2.imwrite(str(path), image)
    template = write_template(tmp_path, spec, colorDropout="red")
    results = list(scan_files([path], template, workers=1))
    assert results[0]["responses"] == {
        **results[0]["responses"],
        **truth["answers"],
    }
    # Aligned image is the dropout result: blank bubbles read as white
    empty = [
        b["mean_intensity"]
        for f in results[0]["fields"].values()
        for b in f["bubbles"]
        if not b["marked"]
    ]
    assert np.median(empty) > 225

    stored = scan_and_store(
        OMREngine(template),
        path,
        {"template_id": "t"},
        tmp_path / "scans",
        SAVE_NONE,
        copy_input=False,
    )
    assert stored[0]["responses"]["q1"] == truth["answers"]["q1"]


def test_cli_applies_dropout(tmp_path, spec, mocker):
    import pandas as pd

    from src.tests.utils import run_entry_point, setup_mocker_patches

    setup_mocker_patches(mocker)
    image, truth = colour_sheet(spec, 12, ink_color=PINK)

    def run(name, dropout):
        inputs = tmp_path / name
        inputs.mkdir()
        cv2.imwrite(str(inputs / "sheet.png"), image)
        template = spec.to_template(pre_processors=[])
        template["colorDropout"] = dropout
        (inputs / "template.json").write_text(json.dumps(template))
        run_entry_point(str(inputs), str(tmp_path / f"out_{name}"))
        csv_path = next((tmp_path / f"out_{name}").rglob("Results_*.csv"))
        return pd.read_csv(csv_path, keep_default_na=False, dtype=str).iloc[0]

    # Pink pen on pink print: the red channel drops the marks with the print
    dropped = run("red", {"mode": "red"})
    assert all(dropped[k] == "" for k in truth["answers"])
    kept = run("grey", "grey")
    assert all(kept[k] == v for k, v in truth["answers"].items())


def _correlation(a, b):
    return float(np.corrcoef(a.astype(float).ravel(), b.astype(float).ravel())[0, 1])


def _assert_follows(engine, image):
    template = engine.template
    companions = {"variant": image.copy()}
    aligned = template.image_instance_ops.apply_preprocessors(
        "x", image, template, companions
    )
    variant = companions["variant"]
    assert aligned is not None and variant.shape == aligned.shape
    exact = _correlation(aligned, variant)
    assert exact > 0.95
    for axis in (0, 1):
        # Two pixels off already correlates clearly worse
        assert exact > _correlation(aligned, np.roll(variant, 2, axis=axis)) + 0.03


@pytest.mark.parametrize(
    "sample, image",
    [
        ("samples/sample1", "samples/sample1/MobileCamera/sheet1.jpg"),
        ("samples/sample2", "samples/sample2/AdrianSample/adrian_omr.png"),
    ],
)
def test_companions_follow_crop_preprocessors(sample, image):
    # sample1: CropPage + CropOnMarkers; sample2: CropPage
    engine = OMREngine(Path(sample, "template.json"))
    _assert_follows(engine, cv2.imread(image, cv2.IMREAD_GRAYSCALE))


@pytest.mark.parametrize("name", ["FeatureBasedAlignment", "EccAlignment"])
def test_companions_follow_reference_alignment(tmp_path, name):
    # A real photo has the texture feature matching and ECC need
    reference = cv2.imread(
        "samples/sample2/AdrianSample/adrian_omr.png", cv2.IMREAD_GRAYSCALE
    )
    cv2.imwrite(str(tmp_path / "ref.png"), reference)
    h, w = reference.shape
    rotation = cv2.getRotationMatrix2D((w / 2, h / 2), 1.5, 1.0)
    rotation[:, 2] += (6, -4)
    image = cv2.warpAffine(reference, rotation, (w, h), borderValue=255)
    options = {"reference": "ref.png"}
    if name == "EccAlignment":
        options["motion"] = "affine"
    template = {
        "pageDimensions": [w, h],
        "bubbleDimensions": [20, 20],
        "preProcessors": [{"name": name, "options": options}],
        "fieldBlocks": {
            "b": {
                "origin": [100, 100],
                "bubblesGap": 30,
                "labelsGap": 30,
                "fieldLabels": ["q1"],
                "bubbleValues": ["A", "B"],
                "direction": "horizontal",
            }
        },
    }
    (tmp_path / "template.json").write_text(json.dumps(template))
    _assert_follows(OMREngine(tmp_path / "template.json"), image)


def test_benchmark_colour_options():
    from src.benchmark.runner import run_synthetic

    metrics = run_synthetic(
        3,
        preset="clean",
        print_color="#E8618C",
        ink_color="#1F3C9A",
        color_dropout={"mode": "red"},
        questions=20,
    )
    assert metrics["config"]["color_dropout"] == {"mode": "red"}
    assert metrics["config"]["print_color"] == "#E8618C"
    assert "dropout" in metrics["throughput"]["latency_ms"]
