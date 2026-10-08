"""
Result geometry record (src/geometry.py) and the alignment steps that fill it:
index points with timing tracks (item 13), block border / bubble-outline
fitting with a print-kept search image (item 14), page outline (item 40),
verify_bubble_fit (item 36), and the "what you see is what was read"
invariants: previews replay the stored geometry and land exactly where the
engine sampled.
"""

import json
import random
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.geometry import (
    empty_geometry,
    map_points,
    print_kept_image,
    warp_to_aligned,
)
from src.pipeline import OMREngine
from src.synth import augment, default_spec, random_answers, render_sheet
from src.utils.image import ImageUtils

SCAN = dict(
    rotation=1.0,
    perspective=0.008,
    blur=1.0,
    noise=3.0,
    shadow=0.1,
    jpeg_quality=(85, 95),
)
PINK = "#f07090"
CORNERS = [(60, 60), (1180, 60), (60, 1694), (1180, 1694)]


def write(tmp_path, template, name="t"):
    directory = Path(tmp_path, name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "template.json"
    path.write_text(json.dumps(template))
    return path


def wrong(result, truth):
    return [k for k, v in truth["answers"].items() if result.responses.get(k) != v]


def replay(geometry, image, engine):
    """What a preview shows: the stored geometry replayed on the source."""
    if image.ndim == 3:
        image = engine.template.prepare_image(image)[0]
    out = warp_to_aligned(geometry, image)
    return ImageUtils.normalize_util(out) if out.max() > out.min() else out


def assert_sampled_where_drawn(result, image):
    """Every bubble's recorded mean is the mean of the image at its box
    (after the background flattening the reader applies by default)."""
    from src.core import ImageInstanceOps

    sizes = [max(b["w"], b["h"]) for f in result.fields.values() for b in f["bubbles"]]
    image = ImageInstanceOps.flatten_background(image, float(np.median(sizes)))
    for field in result.fields.values():
        for bubble in field["bubbles"]:
            x, y, w, h = bubble["x"], bubble["y"], bubble["w"], bubble["h"]
            mean = cv2.mean(image[y : y + h, x : x + w])[0]
            assert abs(mean - bubble["mean_intensity"]) < 0.01, bubble


@pytest.fixture(scope="module")
def spec():
    return default_spec(questions=40, roll_digits=6, with_zones=False)


# --------------------------------------------------------------------------- module


def test_empty_geometry_and_point_mapping():
    geometry = empty_geometry(100, 80)
    assert geometry["aligned_size"] == [100, 80]
    assert map_points(geometry, [[3, 4]]) == [[3.0, 4.0]]
    image = np.random.default_rng(0).integers(0, 255, (80, 100), np.uint8)
    assert np.array_equal(warp_to_aligned(geometry, image), image)

    matrix = cv2.getPerspectiveTransform(
        np.float32([[0, 0], [99, 0], [99, 79], [0, 79]]),
        np.float32([[5, 3], [90, 8], [95, 70], [2, 75]]),
    )
    tps = {
        "points": [[10.0, 10.0], [80.0, 12.0], [50.0, 60.0], [15.0, 70.0]],
        # A gentle bend: kernel weights are per r^2 log r (~1e4 at 50 px)
        "weights": [[5e-5, -2e-5], [-3e-5, 1e-5], [2e-5, 4e-5], [-4e-5, -3e-5],
                    [0.3, 0.2], [0.001, 0.0], [0.0, -0.002]],
        "grid_step": 16,
        "size": [100, 80],
    }
    geometry = dict(
        geometry,
        steps=[
            {"op": "resize", "from": [200, 160], "size": [100, 80]},
            {"op": "warp", "matrix": matrix.tolist(), "size": [100, 80],
             "inverse": False},
            dict(tps, op="tps"),
        ],
        source_size=[200, 160],
    )
    points = [[20.0, 30.0], [150.0, 100.0], [77.0, 21.0]]
    aligned = map_points(geometry, points, "source_to_aligned")
    back = map_points(geometry, aligned, "aligned_to_source")
    assert np.allclose(back, points, atol=1e-3)


# --------------------------------------------------------------------------- engine


@pytest.mark.parametrize("non_rigid", [False, True])
def test_engine_records_geometry_and_replay_is_pixel_identical(
    tmp_path, spec, non_rigid
):
    rng = random.Random(1)
    image, truth = render_sheet(spec, random_answers(spec, rng), rng=rng)
    image, _ = augment(image, rng, **SCAN)
    template = spec.to_template()
    template["preProcessors"][0]["options"]["nonRigid"] = non_rigid
    engine = OMREngine(write(tmp_path, template))
    result = engine.scan(image)
    assert not wrong(result, truth)
    geometry = result.geometry
    assert geometry["source_size"] == [image.shape[1], image.shape[0]]
    assert geometry["aligned_size"] == spec.page
    assert (geometry["tps"] is not None) == non_rigid
    assert geometry["residual"]["page"] < 1.0
    assert set(geometry["margin_trim"]) == {"top", "bottom", "left", "right"}
    assert json.loads(json.dumps(result.to_dict()))["geometry"] == geometry

    replayed = replay(geometry, image, engine)
    assert np.array_equal(replayed, result.aligned_image)
    assert_sampled_where_drawn(result, replayed)

    # A bubble centre maps to the source and back to where it was sampled
    bubble = result.fields["q1"]["bubbles"][0]
    centre = [bubble["x"] + bubble["w"] / 2, bubble["y"] + bubble["h"] / 2]
    source = map_points(geometry, [centre], "aligned_to_source")
    assert np.allclose(map_points(geometry, source), [centre], atol=0.05)


def test_geometry_through_processing_resize_and_filters(tmp_path, spec):
    rng = random.Random(2)
    image, truth = render_sheet(spec, random_answers(spec, rng), rng=rng)
    template = spec.to_template(
        pre_processors=[{"name": "GaussianBlur", "options": {"kSize": [3, 3]}}]
    )
    engine = OMREngine(write(tmp_path, template))
    result = engine.scan(image)
    ops = [step["op"] for step in result.geometry["steps"]]
    assert ops[0] == "resize" and "filter" in ops
    processors = engine.template.pre_processors
    out = warp_to_aligned(
        result.geometry,
        image,
        lambda step, im: processors[step["index"]].apply_filter(im, "x"),
    )
    out = ImageUtils.normalize_util(out)
    assert np.array_equal(out, result.aligned_image)


def symmetric_spec():
    spec = default_spec(questions=40, roll_digits=6, with_zones=False)
    ys = [877.0 + 55 * k for k in range(-12, 13)]
    spec.timing_tracks = {
        "left": [[50.0, y] for y in ys],
        "right": [[1190.0, y] for y in ys],
    }
    return spec


def index_points(extra=()):
    points = [
        {"name": f"corner{i}", "center": list(c), "size": [40, 40], "shape": "square"}
        for i, c in enumerate(CORNERS)
    ]
    points.append(
        {"name": "dot", "center": [300, 120], "size": [30, 30], "shape": "circle"}
    )
    points.extend(extra)
    return points


def index_sheet(spec, seed, flip=False, mid=True, extra_top=0, background=True):
    rng = random.Random(seed)
    image, truth = render_sheet(spec, random_answers(spec, rng), rng=rng)
    cv2.circle(image, (300, 120), 15, 0, -1)
    if mid:
        cv2.circle(image, (460, 900), 8, 0, -1)
    if extra_top:
        image = np.vstack([np.full((extra_top, image.shape[1]), 255, np.uint8), image])
    image, _ = augment(image, rng, flip_180=flip, background=background, **SCAN)
    return image, truth


def test_asymmetric_index_points_decide_orientation(tmp_path):
    spec = symmetric_spec()
    template = spec.to_template()
    template["preProcessors"][0]["options"]["indexPoints"] = index_points()
    engine = OMREngine(write(tmp_path, template))
    processor = engine.template.pre_processors[0]
    for flip in (False, True):
        image, truth = index_sheet(spec, 5, flip=flip)
        result = engine.scan(image)
        assert not wrong(result, truth)
        assert result.geometry["rotation"] == (180 if flip else 0)
        assert all(p["found"] for p in result.geometry["index_points"])

    # The symmetric tracks alone can't tell the two ways up apart; the
    # asymmetric dot can
    image, _ = index_sheet(spec, 5)
    corners = processor.find_page_corners(image)
    candidates = processor.blob_centres(image, corners)
    upright = processor.fit_orientation(corners, candidates, 0)
    flipped = processor.fit_orientation(corners, candidates, 2)
    assert upright["matched"] == flipped["matched"] == len(processor.expected)
    processor.locate_index_points(image, upright)
    processor.locate_index_points(image, flipped)
    assert upright["index_found"] > flipped["index_found"]
    assert processor._orientation_key(upright) > processor._orientation_key(flipped)


def test_missing_required_index_point_goes_to_review(tmp_path, spec):
    mid = {"name": "mid", "center": [460, 900], "size": [16, 16], "shape": "circle"}
    template = spec.to_template()
    template["preProcessors"][0]["options"]["indexPoints"] = index_points([mid])
    engine = OMREngine(write(tmp_path, template))
    image, truth = index_sheet(spec, 7, mid=True)
    result = engine.scan(image)
    assert result.status == "ok" and not wrong(result, truth)
    image, truth = index_sheet(spec, 7, mid=False)
    result = engine.scan(image)
    assert not wrong(result, truth)
    items = [r for r in result.review if r.get("name") == "index_points"]
    assert items and items[0]["missing"] == ["mid"]
    assert result.status == "needs_review"


def test_index_points_alone_register_and_report_margins(tmp_path, spec):
    template = spec.to_template()
    options = template["preProcessors"][0]["options"]
    options["tracks"] = {}
    options["indexPoints"] = index_points()
    engine = OMREngine(write(tmp_path, template))
    image, truth = index_sheet(spec, 8, extra_top=90, background=False)
    result = engine.scan(image)
    assert not wrong(result, truth)
    assert result.geometry["alignment_method"] == "index_points"
    trim = result.geometry["margin_trim"]
    # The extra paper at the top is cut off and reported
    assert trim["top"] > 70 and trim["top"] > 3 * trim["bottom"]
    assert np.array_equal(replay(result.geometry, image, engine), result.aligned_image)


def test_non_rigid_auto_and_region_residual(tmp_path, spec):
    image, truth = index_sheet(spec, 9)
    # Left and top tracks alone don't spread across the page: no curve fit
    template = spec.to_template()
    template["preProcessors"][0]["options"]["nonRigid"] = "auto"
    plain = OMREngine(write(tmp_path, template, "a")).scan(image)
    assert plain.geometry["tps"] is None
    # With index points in the other corners it turns itself on
    mid = {"name": "mid", "center": [460, 900], "size": [16, 16], "shape": "circle"}
    template["preProcessors"][0]["options"]["indexPoints"] = index_points([mid])
    engine = OMREngine(write(tmp_path, template, "b"))
    result = engine.scan(image)
    assert result.geometry["tps"] is not None and not wrong(result, truth)
    assert np.array_equal(replay(result.geometry, image, engine), result.aligned_image)

    # A strict per-region limit sends the sheet to review
    template = spec.to_template()
    template["preProcessors"][0]["options"]["maxRegionResidual"] = 0.01
    strict = OMREngine(write(tmp_path, template, "c")).scan(image)
    items = [r for r in strict.review if r.get("name") == "alignment_residual"]
    assert items and items[0]["regions"]
    assert strict.status == "needs_review"


# --------------------------------------------------------------------------- blocks


def bordered(spec, seed, warps=None, print_color=None, border=8, outer=0):
    rng = random.Random(seed)
    answers = random_answers(spec, rng, blank_rate=0.0)
    original = spec.block_border
    spec.block_border = border
    try:
        image, truth = render_sheet(
            spec,
            answers,
            rng=rng,
            mark_style="mixed",
            block_warps=warps,
            print_color=print_color,
            ink_color="#202020" if print_color else None,
        )
    finally:
        spec.block_border = original
    if outer:
        for block in spec.blocks:
            x0, y0, x1, y1 = spec.block_box(block)
            colour = (40, 40, 40) if image.ndim == 3 else 40
            cv2.rectangle(
                image, (x0 - outer, y0 - outer), (x1 + outer, y1 + outer), colour, 2
            )
    return image, truth


def test_pink_sheet_borders_found_on_print_kept_copy(tmp_path, spec):
    image, truth = bordered(spec, 3, {"MCQ_1": (6, -5, 0.8)}, print_color=PINK)
    image, _ = augment(image, random.Random(3), background=False, **SCAN)
    template = spec.to_template(pre_processors=[])
    template["colorDropout"] = "red"
    template["alignment"] = {"rectify_on_border": True}
    for block in template["fieldBlocks"].values():
        block["borderPadding"] = 8
    engine = OMREngine(write(tmp_path, template))
    result = engine.scan(image)
    assert not wrong(result, truth)
    blocks = result.geometry["blocks"]
    assert {b["status"] for b in blocks.values()} == {"found"}
    assert result.print_image is not None

    # Both views come from one transform and match the engine pixel for pixel
    dropout = replay(result.geometry, image, engine)
    assert np.array_equal(dropout, result.aligned_image)
    printed = warp_to_aligned(result.geometry, print_kept_image(image))
    printed = ImageUtils.normalize_util(printed)
    assert np.array_equal(printed, result.print_image)
    assert_sampled_where_drawn(result, dropout)
    # The stored outline lies on the printed border in the print-kept view;
    # the dropout view has no border there
    corners = np.float32(blocks["MCQ_1"]["corners"])
    along = [corners[0] + t * (corners[1] - corners[0]) for t in np.linspace(0.1, 0.9, 9)]
    ink = [printed[int(round(y)), int(round(x))] for x, y in along]
    paper = [dropout[int(round(y)), int(round(x))] for x, y in along]
    assert np.mean(ink) < np.mean(paper) - 60

    # On the dropout image alone the borders are gone
    from src.rectify import find_border

    block = next(b for b in engine.template.field_blocks if b.name == "MCQ_1")
    assert isinstance(find_border(dropout, block, (8.0, 8.0), 20), str)


def test_two_level_outer_then_inner(tmp_path, spec):
    template = spec.to_template(pre_processors=[])
    for block in template["fieldBlocks"].values():
        block.update(rectifyOnBorder=True, borderPadding=8, outerBorderPadding=24)
    engine = OMREngine(write(tmp_path, template))
    image, truth = bordered(spec, 4, outer=24)
    result = engine.scan(image)
    assert not wrong(result, truth)
    levels = {b["level"] for b in result.geometry["blocks"].values()}
    assert levels == {"inner"}
    # Only the outer frame printed: its fit is used
    image, truth = bordered(spec, 4, border=0, outer=24)
    result = engine.scan(image)
    assert not wrong(result, truth)
    blocks = result.geometry["blocks"].values()
    assert {(b["status"], b["level"]) for b in blocks} == {("found", "outer")}


def test_block_perspective_from_bubble_outlines(tmp_path, spec):
    warps = {"MCQ_1": (9, -7, 0.8)}
    image, truth = bordered(spec, 6, warps, border=0)
    template = spec.to_template(pre_processors=[])
    plain = OMREngine(write(tmp_path, template, "plain")).scan(image)
    labels = spec.blocks[1].field_labels
    assert wrong(plain, truth) or any(plain.fields[k]["needs_review"] for k in labels)

    template["alignment"] = {"block_perspective": True}
    result = OMREngine(write(tmp_path, template, "fit")).scan(image)
    assert not wrong(result, truth)
    block = result.geometry["blocks"]["MCQ_1"]
    assert (block["status"], block["method"]) == ("fitted", "bubbles")
    expected = np.float32(block["expected"])
    assert np.abs(np.float32(block["corners"]) - expected).max() > 2


def test_verify_bubble_fit_switch(tmp_path, spec):
    # Wrong padding: the border is found, but the correction makes the
    # printed bubbles fit worse, so the default check refuses it
    image, _ = bordered(spec, 6)
    template = spec.to_template(pre_processors=[])
    template["fieldBlocks"]["MCQ_1"].update(rectifyOnBorder=True, borderPadding=0)
    template["alignment"] = {"rectify_search_px": 12}
    checked = OMREngine(write(tmp_path, template, "on")).scan(image)
    block = checked.geometry["blocks"]["MCQ_1"]
    assert block["status"] == "failed" and "fit worse" in block["reason"]
    template["alignment"]["verify_bubble_fit"] = False
    unchecked = OMREngine(write(tmp_path, template, "off")).scan(image)
    assert unchecked.geometry["blocks"]["MCQ_1"]["status"] == "found"


def test_page_outline_for_photos_and_fallback(tmp_path, spec):
    rng = random.Random(11)
    flat, truth = render_sheet(spec, random_answers(spec, rng), rng=rng)
    photo, _ = augment(flat, rng, rotation=3.0, perspective=0.03, jpeg_quality=None)
    template = spec.to_template(pre_processors=[])
    template["alignment"] = {"page_outline": True}
    engine = OMREngine(write(tmp_path, template))
    result = engine.scan(photo)
    assert result.geometry["page_outline"] is not None
    assert not wrong(result, truth)
    assert np.array_equal(replay(result.geometry, photo, engine), result.aligned_image)
    # A flatbed scan without a page edge is left as it is
    result = engine.scan(flat)
    assert result.geometry["page_outline"] is None and not wrong(result, truth)


# --------------------------------------------------------------------------- api


def test_results_preview_replays_geometry_in_both_views(tmp_path, spec):
    from src.tests.test_api import make_client, png_bytes

    image, truth = bordered(spec, 3, {"MCQ_1": (6, -5, 0.8)}, print_color=PINK)
    template = spec.to_template(pre_processors=[])
    template["colorDropout"] = "red"
    template["alignment"] = {"rectify_on_border": True}
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates",
            files=[("files", ("template.json", json.dumps(template), "application/json"))],
            data={"name": "Pink"},
        )
        template_id = response.json()["id"]
        response = client.post(
            "/scans",
            data={"template_id": template_id},
            files=[("files", ("pink.png", png_bytes(image), "image/png"))],
        )
        stored = response.json()["scans"][0]
        geometry = stored["geometry"]
        assert set(geometry["blocks"]) == {b.name for b in spec.blocks}
        scan_id = stored["scan_id"]

        engine = OMREngine(write(tmp_path, template))
        direct = engine.scan(image)
        rendered = client.get(f"/scans/{scan_id}/render").json()
        assert rendered["geometry_replayed"] is True
        overlay = rendered["geometry"]
        assert {b["name"]: b["corners"] for b in overlay["blocks"]} == {
            name: b["corners"] for name, b in geometry["blocks"].items()
        }
        assert all(b["color"] == "#1e9e3a" for b in overlay["blocks"])
        # Overlay bubbles are the sampled ones, on a pixel-identical page
        for field in rendered["fields"]:
            sampled = stored["fields"][field["name"]]["bubbles"]
            assert [(b["x"], b["y"]) for b in field["bubbles"]] == [
                (b["x"], b["y"]) for b in sampled
            ]
        for view, expected in (("dropout", direct.aligned_image), ("print", direct.print_image)):
            data = client.get(
                f"/scans/{scan_id}/render/image?format=png&view={view}"
            ).content
            picture = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            assert np.array_equal(picture, expected), view
        assert_sampled_where_drawn(direct, direct.aligned_image)

        # A different file at the recorded path is refused, not replayed
        ctx = client.app.state.ctx
        assert stored["source_sha256"]
        source = Path(stored["source_path"])
        original = source.read_bytes()
        source.write_bytes(original + b"changed")
        ctx.results.renders.items.clear()
        changed = client.get(f"/scans/{scan_id}/render").json()
        assert changed["image_source"] == "stored"
        assert any("not the file that was read" in w for w in changed["warnings"])
        source.write_bytes(original)
        ctx.results.renders.items.clear()

        # Old results without geometry still render (by re-reading)
        ctx = client.app.state.ctx
        path = ctx.data.scan_dir(scan_id) / "result.json"
        old = json.loads(path.read_text())
        old.pop("geometry")
        path.write_text(json.dumps(old))
        ctx.results.renders.items.clear()
        again = client.get(f"/scans/{scan_id}/render").json()
        assert again["image_source"] == "source" and again["geometry"] is None
        assert "geometry_replayed" not in again


def test_editor_align_preview_and_test_on_samples(tmp_path, spec):
    from src.tests.test_api import make_client, png_bytes

    image, _ = bordered(spec, 5, border=8)
    template = spec.to_template(pre_processors=[])
    with make_client(tmp_path) as client:
        response = client.post(
            "/templates",
            files=[("files", ("template.json", json.dumps(template), "application/json"))],
            data={"name": "Borders"},
        )
        template_id = response.json()["id"]
        base = f"/templates/{template_id}/align"
        response = client.post(
            f"{base}/samples",
            files=[
                ("files", ("a.png", png_bytes(image), "image/png")),
                ("files", ("blank.png", png_bytes(np.full_like(image, 255)), "image/png")),
            ],
        )
        assert response.json()["samples"] == ["a.png", "blank.png"]
        # Unsaved template with borders switched on, straight from the editor
        unsaved = dict(template, alignment={"rectify_on_border": True})
        preview = client.post(
            f"{base}/preview", json={"sample": "a.png", "template": unsaved}
        ).json()
        assert {b["status"] for b in preview["blocks"]} == {"found"}
        assert preview["image_data"].startswith("data:image/jpeg")
        assert preview["fields"]["q1"][0]["w"] == spec.bubble[0]
        test = client.post(f"{base}/test", json={"template": unsaved}).json()
        assert test["total"] == 2 and test["failed"] == 1
        failed = {row["sample"]: row["failed_blocks"] for row in test["samples"]}
        assert not failed["a.png"] and len(failed["blank.png"]) == len(spec.blocks)
        # The saved template is untouched and samples don't change its version
        assert "alignment" not in client.get(f"/templates/{template_id}").json()["template"]
        assert client.delete(f"{base}/samples/blank.png").json()["samples"] == ["a.png"]
