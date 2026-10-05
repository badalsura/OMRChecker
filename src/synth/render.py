"""
Render OMR sheets with known ground truth.

A SheetSpec describes the printed form (page size, bubble grids, timing marks,
corner markers and text/barcode zones). render_sheet() draws it, fills bubbles
for the given answers with configurable pen styles, and returns the image and
the ground truth. augment() then simulates scanning or phone capture.

spec.to_template() produces the matching template.json, so the same spec drives
both the reader and the evaluation.
"""

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import cv2
import numpy as np

MCQ_VALUES = ["A", "B", "C", "D"]
DIGIT_VALUES = [str(d) for d in range(10)]


@dataclass
class BlockSpec:
    name: str
    origin: List[int]
    field_labels: List[str]
    bubble_values: List[str]
    direction: str  # "horizontal": values left to right, fields stacked downwards
    bubbles_gap: int
    labels_gap: int


@dataclass
class ZoneSpec:
    name: str
    type: str  # barcode | qrcode | ocr | icr
    origin: List[int]
    dimensions: List[int]
    options: dict = field(default_factory=dict)


@dataclass
class SheetSpec:
    page: List[int] = field(default_factory=lambda: [1240, 1754])  # A4 at 150 DPI
    bubble: List[int] = field(default_factory=lambda: [30, 30])
    blocks: List[BlockSpec] = field(default_factory=list)
    zones: List[ZoneSpec] = field(default_factory=list)
    # Timing marks: {track_name: [[cx, cy], ...]}
    timing_tracks: Dict[str, List[List[float]]] = field(default_factory=dict)
    timing_mark: List[int] = field(default_factory=lambda: [24, 12])
    corner_marker: int = 40

    def bubble_positions(self):
        """Yield (block, field_label, value, x, y) for every bubble."""
        for block in self.blocks:
            for f_index, label in enumerate(block.field_labels):
                for v_index, value in enumerate(block.bubble_values):
                    if block.direction == "horizontal":
                        x = block.origin[0] + v_index * block.bubbles_gap
                        y = block.origin[1] + f_index * block.labels_gap
                    else:
                        x = block.origin[0] + f_index * block.labels_gap
                        y = block.origin[1] + v_index * block.bubbles_gap
                    yield block, label, value, x, y

    def field_labels(self):
        return [label for block in self.blocks for label in block.field_labels]

    def to_template(self, pre_processors=None):
        if pre_processors is None:
            pre_processors = []
            if self.timing_tracks:
                pre_processors.append(
                    {
                        "name": "TimingMarkAlignment",
                        "options": {
                            "tracks": {
                                name: {"marks": marks}
                                for name, marks in self.timing_tracks.items()
                            },
                            "markDimensions": list(self.timing_mark),
                        },
                    }
                )
        return {
            "pageDimensions": list(self.page),
            "bubbleDimensions": list(self.bubble),
            "preProcessors": pre_processors,
            "fieldBlocks": {
                block.name: {
                    "origin": list(block.origin),
                    "bubblesGap": block.bubbles_gap,
                    "labelsGap": block.labels_gap,
                    "fieldLabels": list(block.field_labels),
                    "bubbleValues": list(block.bubble_values),
                    "direction": block.direction,
                }
                for block in self.blocks
            },
            "zones": {
                zone.name: {
                    "type": zone.type,
                    "origin": list(zone.origin),
                    "dimensions": list(zone.dimensions),
                    "options": dict(zone.options),
                }
                for zone in self.zones
            },
        }


def default_spec(questions=40, roll_digits=6, with_zones=True):
    """A typical exam sheet: roll-number grid, MCQ columns, timing tracks and zones."""
    page_w, page_h = 1240, 1754
    blocks = [
        BlockSpec(
            "Roll",
            [140, 330],
            [f"roll{i}" for i in range(1, roll_digits + 1)],
            DIGIT_VALUES,
            "vertical",
            bubbles_gap=40,
            labels_gap=40,
        )
    ]
    per_column = 20
    for column in range((questions + per_column - 1) // per_column):
        start = column * per_column + 1
        end = min(questions, start + per_column - 1)
        blocks.append(
            BlockSpec(
                f"MCQ_{column + 1}",
                [520 + column * 330, 330],
                [f"q{i}" for i in range(start, end + 1)],
                MCQ_VALUES,
                "horizontal",
                bubbles_gap=50,
                labels_gap=55,
            )
        )
    # Left and top timing tracks: one mark per printed row / column band
    left = [[50.0, float(y)] for y in range(200, page_h - 150, 55)]
    top = [[float(x), 50.0] for x in range(200, page_w - 100, 80)]
    zones = []
    if with_zones:
        zones = [
            ZoneSpec(
                "sheet_id", "barcode", [700, 110], [460, 120], {"formats": ["Code128"]}
            ),
            ZoneSpec("qr", "qrcode", [560, 90], [130, 130]),
            ZoneSpec("exam_code", "ocr", [140, 120], [380, 60]),
            ZoneSpec(
                "candidate_no",
                "icr",
                [140, 230],
                [360, 60],
                {"characterBoxes": 6, "whitelist": "0123456789"},
            ),
        ]
    return SheetSpec(
        page=[page_w, page_h],
        blocks=blocks,
        zones=zones,
        timing_tracks={"left": left, "top": top},
    )


def random_answers(spec, rng=None, blank_rate=0.05, multi_rate=0.0):
    rng = rng or random.Random()
    answers = {}
    for block in spec.blocks:
        for label in block.field_labels:
            roll = rng.random()
            if roll < blank_rate:
                answers[label] = ""
            elif roll < blank_rate + multi_rate:
                answers[label] = "".join(sorted(rng.sample(block.bubble_values, 2)))
            else:
                answers[label] = rng.choice(block.bubble_values)
    return answers


def _draw_text(img, text, origin, height, thickness=2):
    scale = cv2.getFontScaleFromHeight(cv2.FONT_HERSHEY_SIMPLEX, height, thickness)
    cv2.putText(
        img, text, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, 0, thickness, cv2.LINE_AA
    )


def _draw_mark(img, cx, cy, w, h, rng, style):
    """Draw a pen/pencil mark inside a bubble centred at (cx, cy)."""
    rx, ry = int(w * 0.38), int(h * 0.38)
    darkness = {"pen": rng.randint(10, 50), "pencil": rng.randint(60, 110)}.get(
        style, rng.randint(10, 60)
    )
    if style == "partial":
        cv2.ellipse(img, (cx, cy), (rx, ry), 0, 0, rng.randint(200, 300), darkness, -1)
    elif style == "tick":
        pts = np.array(
            [[cx - rx, cy], [cx - rx // 3, cy + ry], [cx + rx, cy - ry]], np.int32
        )
        cv2.polylines(img, [pts], False, darkness, max(2, w // 8), cv2.LINE_AA)
    else:
        jitter = rng.randint(-2, 2)
        cv2.ellipse(
            img,
            (cx + jitter, cy - jitter),
            (rx, ry),
            0,
            0,
            360,
            darkness,
            -1,
            cv2.LINE_AA,
        )


def render_sheet(
    spec: SheetSpec,
    answers: Dict[str, str],
    zone_values: Optional[Dict[str, str]] = None,
    rng=None,
    mark_style="pen",
    erasures=0,
):
    """Return (grayscale image, ground truth dict)."""
    rng = rng or random.Random()
    page_w, page_h = spec.page
    img = np.full((page_h, page_w), 255, np.uint8)
    bw, bh = spec.bubble

    # Corner markers (solid squares) inset from the page edge
    m = spec.corner_marker
    for cx, cy in [
        (m, m),
        (page_w - 2 * m, m),
        (m, page_h - 2 * m),
        (page_w - 2 * m, page_h - 2 * m),
    ]:
        cv2.rectangle(img, (cx, cy), (cx + m, cy + m), 0, -1)

    tw, th = spec.timing_mark
    for marks in spec.timing_tracks.values():
        for cx, cy in marks:
            horizontal_track = len({round(p[1]) for p in marks}) == 1
            w, h = (th, tw) if horizontal_track else (tw, th)
            cv2.rectangle(
                img,
                (int(cx - w / 2), int(cy - h / 2)),
                (int(cx + w / 2), int(cy + h / 2)),
                0,
                -1,
            )

    for block, label, value, x, y in spec.bubble_positions():
        cx, cy = x + bw // 2, y + bh // 2
        cv2.ellipse(
            img, (cx, cy), (bw // 2 - 2, bh // 2 - 2), 0, 0, 360, 90, 1, cv2.LINE_AA
        )
        _draw_text(img, value, (cx - bw // 6, cy + bh // 6), max(bh // 3, 6), 1)
    for block in spec.blocks:
        first = block.field_labels[0]
        _draw_text(img, block.name, (block.origin[0], block.origin[1] - 15), 14, 1)
        del first

    positions = {
        (label, value): (x, y) for _, label, value, x, y in spec.bubble_positions()
    }
    marked = {}
    for label, answer in answers.items():
        for value in _split_answer(answer, label, spec):
            x, y = positions[(label, value)]
            style = (
                mark_style
                if mark_style != "mixed"
                else rng.choice(["pen", "pencil", "pen", "partial"])
            )
            _draw_mark(img, x + bw // 2, y + bh // 2, bw, bh, rng, style)
            marked[(label, value)] = style

    # Erasures: faint smudges in unmarked bubbles (should still read as empty)
    candidates = [k for k in positions if k not in marked]
    for label, value in rng.sample(candidates, min(erasures, len(candidates))):
        x, y = positions[(label, value)]
        overlay = img.copy()
        cv2.ellipse(
            overlay, (x + bw // 2, y + bh // 2), (bw // 3, bh // 3), 0, 0, 360, 170, -1
        )
        img = cv2.addWeighted(overlay, 0.5, img, 0.5, 0)

    zone_values = dict(zone_values or {})
    for zone in spec.zones:
        value = zone_values.setdefault(zone.name, _default_zone_value(zone, rng))
        _draw_zone(img, zone, value, rng)

    truth = {
        "answers": dict(answers),
        "zones": zone_values,
        "marks": {
            f"{label}:{value}": style for (label, value), style in marked.items()
        },
    }
    return img, truth


def _split_answer(answer, label, spec):
    if not answer:
        return []
    for block in spec.blocks:
        if label in block.field_labels:
            values = block.bubble_values
            break
    # Answers are concatenations of single-character values
    return [v for v in values if v in answer]


def _default_zone_value(zone, rng):
    if zone.type in ("barcode", "qrcode"):
        return f"SHEET-{rng.randint(100000, 999999)}"
    if zone.type == "icr":
        return "".join(
            rng.choice("0123456789")
            for _ in range(zone.options.get("characterBoxes", 6))
        )
    return f"EXAM {rng.randint(1000, 9999)}"


def _draw_zone(img, zone, value, rng):
    x, y = zone.origin
    w, h = zone.dimensions
    if zone.type in ("barcode", "qrcode"):
        import zxingcpp

        fmt = (
            zxingcpp.BarcodeFormat.QRCode
            if zone.type == "qrcode"
            else (
                zxingcpp.barcode_format_from_str(
                    zone.options.get("formats", ["Code128"])[0]
                )
            )
        )
        symbol = zxingcpp.create_barcode(value, fmt)
        code = np.array(zxingcpp.write_barcode_to_image(symbol, scale=4))
        scale = min((w - 10) / code.shape[1], (h - 10) / code.shape[0])
        code = cv2.resize(
            code, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST
        )
        ch, cw = code.shape
        oy, ox = y + (h - ch) // 2, x + (w - cw) // 2
        img[oy : oy + ch, ox : ox + cw] = code
    elif zone.type == "ocr":
        _draw_text(img, value, (x + 5, y + h - 15), int(h * 0.55), 2)
    elif zone.type == "icr":
        count = zone.options.get("characterBoxes", len(value))
        box_w = w / count
        for i in range(count):
            x0 = int(x + i * box_w)
            cv2.rectangle(img, (x0, y), (int(x0 + box_w), y + h), 120, 1)
            if i < len(value):
                # Hand-drawn look: a slanted, wobbly Hershey script glyph
                glyph = np.full((h, int(box_w)), 255, np.uint8)
                _draw_handwritten_char(glyph, value[i], rng)
                region = img[y : y + h, x0 : x0 + int(box_w)]
                np.minimum(
                    region, glyph[: region.shape[0], : region.shape[1]], out=region
                )


def _draw_handwritten_char(glyph, char, rng):
    h, w = glyph.shape
    font = rng.choice([cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, cv2.FONT_HERSHEY_SIMPLEX])
    thickness = rng.randint(2, 3)
    scale = cv2.getFontScaleFromHeight(font, int(h * 0.55), thickness)
    size, _ = cv2.getTextSize(char, font, scale, thickness)
    org = (
        max((w - size[0]) // 2 + rng.randint(-2, 2), 0),
        (h + size[1]) // 2 + rng.randint(-2, 2),
    )
    cv2.putText(
        glyph, char, org, font, scale, rng.randint(0, 60), thickness, cv2.LINE_AA
    )
    shear = rng.uniform(-0.15, 0.15)
    matrix = np.float32([[1, shear, -shear * h / 2], [0, 1, 0]])
    glyph[:] = cv2.warpAffine(glyph, matrix, (w, h), borderValue=255)


def augment(
    img,
    rng=None,
    rotation=2.0,
    perspective=0.02,
    blur=1.0,
    noise=6.0,
    shadow=0.3,
    jpeg_quality=(60, 95),
    background=True,
    flip_180=False,
):
    """Simulate scanner/phone capture. Returns (image, 3x3 page->image homography)."""
    rng = rng or random.Random()
    h, w = img.shape[:2]
    pad = int(0.06 * max(h, w)) if background else 0
    canvas_w, canvas_h = w + 2 * pad, h + 2 * pad
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    jitter = perspective * min(w, h)
    dst = (
        np.float32(
            [
                [pad + rng.uniform(-jitter, jitter), pad + rng.uniform(-jitter, jitter)]
                for _ in range(4)
            ]
        )
        + src
    )
    centre = (canvas_w / 2, canvas_h / 2)
    angle = rng.uniform(-rotation, rotation) + (180 if flip_180 else 0)
    rot = cv2.getRotationMatrix2D(centre, angle, 1.0)
    dst = cv2.transform(dst[None], rot)[0]
    homography = cv2.getPerspectiveTransform(src, dst)
    bg_value = rng.randint(40, 120) if background else 255
    out = cv2.warpPerspective(
        img,
        homography,
        (canvas_w, canvas_h),
        borderValue=bg_value,
        flags=cv2.INTER_LINEAR,
    )
    if shadow > 0:
        gradient = np.linspace(
            1.0 - rng.uniform(0, shadow), 1.0, canvas_w, dtype=np.float32
        )
        if rng.random() < 0.5:
            gradient = gradient[::-1]
        out = np.clip(out.astype(np.float32) * gradient[None, :], 0, 255).astype(
            np.uint8
        )
    if blur > 0:
        k = rng.choice([1, 3, 3, 5]) if blur >= 1 else 1
        if k > 1:
            out = cv2.GaussianBlur(out, (k, k), blur)
    if noise > 0:
        out = np.clip(
            out.astype(np.float32)
            + np.random.default_rng(rng.randint(0, 1 << 30)).normal(
                0, noise, out.shape
            ),
            0,
            255,
        ).astype(np.uint8)
    if jpeg_quality:
        quality = rng.randint(*jpeg_quality)
        ok, buffer = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, quality])
        out = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
    return out, homography
