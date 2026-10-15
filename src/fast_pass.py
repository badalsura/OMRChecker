"""
Fast first pass (test mode): read a sheet at half resolution and keep the
result only when every value is clear; any doubt re-reads it the normal way.

Off unless config.json sets speed_params.fast_pass to true. The normal read
is unchanged, so switching the option off restores today's results exactly.

How a fast read works:
- the JPEG is decoded straight to half size (cv2.IMREAD_REDUCED_*), which
  also skips most of the decode work;
- a copy of the template with every page coordinate halved reads it, so
  registration, warping, bubble reading and zones all see a quarter of the
  pixels;
- only the upright orientation is tried (a turned sheet fails and is re-read);
- the result is accepted only when the sheet has nothing to review, every
  bubble is clearly empty or clearly filled (speed_params.fast_clear_empty /
  fast_clear_marked) and every zone was read. Coordinates in the accepted
  result are scaled back to full size, so overlays and re-renders line up.

Non-rigid alignment ("Bend to fit the marks": Exact tracks or a smooth
curve) runs at half size too: its correction is measured from the marks in
the halved template, and the recorded geometry replays at full size.

Templates with steps not checked at half size (other preprocessors,
block border fitting, OCR/ICR/image zones, a bubble
model, auto_align) never use the fast pass.
"""

import copy
import io
import json
from pathlib import Path

import cv2
import numpy as np

from src.logger import logger

# Decode flags for the supported scales: (grey, colour)
REDUCED_FLAGS = {
    0.5: (cv2.IMREAD_REDUCED_GRAYSCALE_2, cv2.IMREAD_REDUCED_COLOR_2),
    0.25: (cv2.IMREAD_REDUCED_GRAYSCALE_4, cv2.IMREAD_REDUCED_COLOR_4),
}
FAST_ZONE_TYPES = ("barcode", "qrcode")
# Flags that never stop a fast result on their own (a blank answer is normal)
HARMLESS_FLAGS = {"empty"}
DEFAULT_MAX_RESIDUAL = 3.0


def _scale_value(value, s, integer=False):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(round(value * s)) if integer else value * s
    if isinstance(value, list):
        return [_scale_value(v, s, integer) for v in value]
    return value


def scaled_template(template_json, s):
    """A copy of the template dict with every page coordinate times s."""
    t = copy.deepcopy(template_json)
    for key in ("pageDimensions", "bubbleDimensions"):
        if key in t:
            t[key] = _scale_value(t[key], s, integer=True)
    for block in (t.get("fieldBlocks") or {}).values():
        for key in (
            "origin",
            "bubbleDimensions",
            "bubblesGap",
            "labelsGap",
            "borderPadding",
            "outerBorderPadding",
        ):
            if key in block:
                block[key] = _scale_value(block[key], s, key == "origin")
    for zone in (t.get("zones") or {}).values():
        for key in ("origin", "dimensions"):
            if key in zone:
                zone[key] = _scale_value(zone[key], s, integer=True)
    for step in t.get("preProcessors") or []:
        options = step.get("options") or {}
        for track in (options.get("tracks") or {}).values():
            track["marks"] = _scale_value(track["marks"], s)
        for point in options.get("indexPoints") or []:
            for key in ("center", "size"):
                if key in point:
                    point[key] = _scale_value(point[key], s)
        for key in ("markDimensions", "searchRadius", "maxRegionResidual"):
            if key in options:
                options[key] = _scale_value(options[key], s)
        options["maxResidual"] = options.get("maxResidual", DEFAULT_MAX_RESIDUAL) * s
        # Upright only: a turned sheet fails here and is read the normal way
        options["detectOrientation"] = False
        step["options"] = options
    return t


def unsupported_reason(template_json, tuning_config, engine):
    """Why this template can't use the fast pass, or None."""
    steps = template_json.get("preProcessors") or []
    if not steps or any(step.get("name") != "TimingMarkAlignment" for step in steps):
        return "only timing-mark templates are supported"
    alignment = template_json.get("alignment") or {}
    if any(
        alignment.get(key)
        for key in ("rectify_on_border", "block_perspective", "page_outline", "rotate", "flip", "straighten")
    ):
        return "alignment options"
    for block in (template_json.get("fieldBlocks") or {}).values():
        if block.get("rectifyOnBorder") or block.get("blockPerspective"):
            return "block border fitting"
    for zone in (template_json.get("zones") or {}).values():
        if zone.get("type") not in FAST_ZONE_TYPES:
            return f"{zone.get('type')} zones"
    if tuning_config.alignment_params.get("auto_align"):
        return "auto_align"
    if tuning_config.review_params.get("min_grid_fit", 0):
        return "min_grid_fit"
    if engine.image_ops.bubble_classifier is not None:
        return "bubble model"
    return None


def load_template_json(template_path, overrides=None):
    with open(template_path, encoding="utf-8") as handle:
        data = json.load(handle)
    for key, value in (overrides or {}).items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def full_size(file_path, data=None):
    """(width, height) of the image file from its header, or None."""
    try:
        from PIL import Image

        source = io.BytesIO(data) if data is not None else str(file_path)
        with Image.open(source) as image:
            return image.size
    except Exception:
        return None


def decode_reduced(file_path, data, s, color):
    flag = REDUCED_FLAGS[s][1 if color else 0]
    if data is not None:
        return cv2.imdecode(np.frombuffer(data, np.uint8), flag)
    return cv2.imread(str(file_path), flag)


def accept(result, params):
    """The fast result is final: nothing to review and every value is clear."""
    if result.status != "ok" or result.error or result.review:
        return False
    empty_max = params.get("fast_clear_empty", 0.25)
    marked_min = params.get("fast_clear_marked", 0.6)
    for details in result.fields.values():
        if set(details.get("flags") or []) - HARMLESS_FLAGS:
            return False
        for bubble in details.get("bubbles") or []:
            ratio = bubble["fill_ratio"]
            if bubble["marked"]:
                if ratio < marked_min:
                    return False
            elif ratio > empty_max:
                return False
    for zone in result.zones.values():
        if not zone.get("value") or zone.get("flags") or zone.get("needs_review"):
            return False
    return True


def _scale_points(points, sx, sy):
    if points is None:
        return None
    return [[round(p[0] * sx, 2), round(p[1] * sy, 2)] for p in points]


def to_full_scale(result, s, source_size, reduced_size, page_dimensions):
    """Rewrite a fast result's coordinates in full-size page and scan pixels."""
    up = 1.0 / s
    for details in result.fields.values():
        for bubble in details.get("bubbles") or []:
            for key in ("x", "y", "w", "h"):
                bubble[key] = int(round(bubble[key] * up))
    for zone in result.zones.values():
        if zone.get("box"):
            zone["box"] = [int(round(v * up)) for v in zone["box"]]
    geometry = result.geometry
    if geometry is None:
        return result
    full_w, full_h = (int(v) for v in source_size)
    red_w, red_h = (int(v) for v in reduced_size)
    page_w, page_h = (int(round(v)) for v in page_dimensions)
    half_page = [int(v) for v in geometry["aligned_size"]]
    down = np.diag([red_w / full_w, red_h / full_h, 1.0])
    up_page = np.diag([page_w / half_page[0], page_h / half_page[1], 1.0])
    matrix = up_page @ np.asarray(geometry["page_homography"], np.float64) @ down
    geometry["page_homography"] = (matrix / matrix[2, 2]).tolist()
    geometry["steps"] = (
        [{"op": "resize", "from": [full_w, full_h], "size": [red_w, red_h]}]
        + geometry.get("steps", [])
        + [{"op": "resize", "from": half_page, "size": [page_w, page_h]}]
    )
    geometry["source_size"] = [full_w, full_h]
    geometry["aligned_size"] = [page_w, page_h]
    sx, sy = full_w / red_w, full_h / red_h
    if geometry.get("page_outline"):
        geometry["page_outline"] = _scale_points(geometry["page_outline"], sx, sy)
    for point in geometry.get("index_points") or []:
        if point.get("image"):
            point["image"] = [round(point["image"][0] * sx, 2), round(point["image"][1] * sy, 2)]
    residual = geometry.get("residual")
    if isinstance(residual, dict):
        residual["page"] = round(residual.get("page", 0.0) * up, 3)
        for region in (residual.get("regions") or {}).values():
            region["mean"] = round(region["mean"] * up, 3)
            region["max"] = round(region["max"] * up, 3)
    trim = geometry.get("margin_trim")
    if isinstance(trim, dict):
        geometry["margin_trim"] = {
            k: (round(v * up, 2) if isinstance(v, (int, float)) else v)
            for k, v in trim.items()
        }
    for block in (geometry.get("blocks") or {}).values():
        for key in ("corners", "expected"):
            if block.get(key):
                block[key] = [[c[0] * up, c[1] * up] for c in block[key]]
    if result.quality and "bubble_px" in result.quality:
        result.quality["bubble_px"] = round(result.quality["bubble_px"] * up, 1)
    return result


def build_fast_engine(engine_cls, template_path, config_path, evaluation_path, config_overrides, template_overrides, base_engine):
    """The half-size engine for base_engine, or None (reason logged)."""
    params = base_engine.tuning_config.get("speed_params") or {}
    s = float(params.get("fast_scale", 0.5))
    if s not in REDUCED_FLAGS:
        logger.warning(f"Fast pass off: fast_scale must be one of {sorted(REDUCED_FLAGS)}")
        return None, s
    template_json = load_template_json(template_path, template_overrides)
    reason = unsupported_reason(template_json, base_engine.tuning_config, base_engine)
    if reason:
        logger.warning(f"Fast pass off for this template: {reason}")
        return None, s
    overrides = dict(config_overrides or {})
    overrides["speed_params"] = {**params, "fast_pass": False}
    scaled = scaled_template(template_json, s)
    fast = engine_cls(
        Path(template_path),
        config_path=config_path,
        evaluation_path=evaluation_path,
        config_overrides=overrides,
        template_overrides={k: v for k, v in scaled.items() if k != "$schema"},
    )
    return fast, s
