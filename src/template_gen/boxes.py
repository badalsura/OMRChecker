"""
Printed rectangles on the blank form.

Many forms print a box around each block of bubbles. The generator uses them
two ways:
- a box tightly around one block turns on border fitting for that block
  ("rectifyOnBorder") with the measured gap ("borderPadding");
- every box is listed in the report, so the editor can offer to adopt a box
  that has no block yet: block_from_box finds the bubble grid inside it.
"""

import cv2
import numpy as np

from src.template_gen import bubbles
from src.template_gen import labels as label_ops


def detect_printed_boxes(gray, page_size, min_area=0.002, max_area=0.6):
    """
    Rectangular outlines (thin printed lines, mostly paper inside).
    Returns [[x, y, w, h], ...] in page pixels, largest first, without
    near-duplicates (the inner and outer edge of one printed line).
    """
    page_w, page_h = page_size
    page_area = float(page_w * page_h)
    # Local threshold: light (e.g. pink) printed lines count next to black text
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12
    )
    # Long straight strokes only: printed text and bubbles drop out
    length = max(15, int(0.03 * page_w))
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1))
    )
    vertical = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, length))
    )
    lines = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8))
    found = cv2.findContours(lines, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[0] if len(found) == 2 else found[1]
    boxes = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = float(w * h)
        if not min_area * page_area <= area <= max_area * page_area:
            continue
        if min(w, h) < 0.02 * page_w:
            continue
        if cv2.contourArea(contour) < 0.85 * area:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) != 4:
            continue
        # Outline, not a filled patch: the middle is mostly paper
        inner = binary[y + h // 4 : y + 3 * h // 4, x + w // 4 : x + 3 * w // 4]
        if inner.size and np.count_nonzero(inner) > 0.5 * inner.size:
            continue
        boxes.append([int(x), int(y), int(w), int(h)])
    boxes.sort(key=lambda b: -b[2] * b[3])
    unique = []
    for box in boxes:
        if not any(_same_box(box, other) for other in unique):
            unique.append(box)
    return unique


def _same_box(a, b, tolerance=12):
    return all(abs(p - q) <= tolerance for p, q in zip(a, b))


def _contains(outer, inner, margin=2):
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ox - margin <= ix
        and oy - margin <= iy
        and ix + iw <= ox + ow + margin
        and iy + ih <= oy + oh + margin
    )


def border_for_grid(grid, boxes, other_grids=()):
    """
    The printed box tightly around one grid: (box, [gap_x, gap_y]) or None.
    The box must hold no other grid and leave about the same gap on opposite
    sides (border fitting assumes one gap per axis).
    """
    bbox = grid.bbox()
    pitch = max(min(p for p in (grid.dx, grid.dy, grid.bubble[0]) if p > 0), 1.0)
    best = None
    for box in boxes:
        if not _contains(box, bbox):
            continue
        if any(_contains(box, other.bbox()) for other in other_grids if other is not grid):
            continue
        left = bbox[0] - box[0]
        right = box[0] + box[2] - (bbox[0] + bbox[2])
        top = bbox[1] - box[1]
        bottom = box[1] + box[3] - (bbox[1] + bbox[3])
        gaps = [left, right, top, bottom]
        if min(gaps) < 0 or max(gaps) > 3 * pitch:
            continue
        if abs(left - right) > 0.5 * pitch or abs(top - bottom) > 0.5 * pitch:
            continue
        area = box[2] * box[3]
        if best is None or area < best[0]:
            gap = [round((left + right) / 2.0, 1), round((top + bottom) / 2.0, 1)]
            best = (area, box, gap)
    return (best[1], best[2]) if best else None


def boxes_report(boxes, grids, names, candidates=None):
    """
    Each box with the blocks inside it (names: grid index -> block name) and
    the number of bubble outlines seen inside (a box without a block but with
    bubbles can be adopted as a block).
    """
    out = []
    centres = np.zeros((0, 2)) if candidates is None or not len(candidates) else candidates[:, :2]
    for box in boxes:
        inside = [
            names[k]
            for k, grid in enumerate(grids)
            if k in names and _contains(box, grid.bbox(), margin=4)
        ]
        x, y, w, h = box
        count = int(
            (
                (centres[:, 0] > x) & (centres[:, 0] < x + w)
                & (centres[:, 1] > y) & (centres[:, 1] < y + h)
            ).sum()
        )
        out.append({"box": box, "blocks": inside, "bubbles_inside": count,
                    "adoptable": not inside and count >= 4})
    return out


def block_from_box(gray, box, page_size, bubble_dims=None):
    """
    Adopt a printed box as a field block: find the bubble grid inside it.
    Returns (block dict for template.json, info) or (None, reason).
    """
    page_w, page_h = page_size
    x, y, w, h = (int(round(v)) for v in box)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(page_w, x + w), min(page_h, y + h)
    if x1 - x0 < 10 or y1 - y0 < 10:
        return None, "box too small"
    crop = np.ascontiguousarray(gray[y0:y1, x0:x1])
    candidates = bubbles.detect_bubble_candidates(crop, (x1 - x0, y1 - y0))
    if len(candidates) == 0:
        return None, "no bubbles found in the box"
    grids, _ = bubbles.group_into_grids(candidates, crop)
    if not grids:
        return None, "no regular grid of bubbles found in the box"
    grid = max(grids, key=lambda g: g.rows * g.cols)
    grid.x0 += x0
    grid.y0 += y0
    direction = label_ops.default_direction(grid)
    n_values = grid.cols if direction == "horizontal" else grid.rows
    n_fields = grid.rows if direction == "horizontal" else grid.cols
    bw, bh = grid.bubble
    if direction == "horizontal":
        bubbles_gap, labels_gap = grid.dx, grid.dy
    else:
        bubbles_gap, labels_gap = grid.dy, grid.dx
    block = {
        "origin": [int(round(grid.x0 - bw / 2)), int(round(grid.y0 - bh / 2))],
        "bubblesGap": round(float(bubbles_gap or bh * 1.5), 2),
        "labelsGap": round(float(labels_gap or bw * 1.5), 2),
        "fieldLabels": [],  # named by the caller
        "bubbleValues": label_ops.default_values(n_values),
        "direction": direction,
    }
    if bubble_dims and (
        abs(bw - bubble_dims[0]) > 0.15 * bubble_dims[0]
        or abs(bh - bubble_dims[1]) > 0.15 * bubble_dims[1]
    ):
        block["bubbleDimensions"] = [int(round(bw)), int(round(bh))]
    fit = border_for_grid(grid, [[x, y, w, h]])
    if fit:
        block["rectifyOnBorder"] = True
        block["borderPadding"] = fit[1]
    return block, {
        "rows": grid.rows,
        "cols": grid.cols,
        "fields": n_fields,
        "values": n_values,
        "bbox": [round(v, 1) for v in grid.bbox()],
    }
