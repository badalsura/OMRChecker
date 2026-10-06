"""
Field block rectification onto a printed border.

After full-page alignment a block can still sit a little off (paper curl, a
locally stretched print, a phone photo of a bent page). When the form prints a
rectangle around the block, the rectangle tells us exactly where the block's
bubbles are: find its four sides near where the template expects them, fit the
quadrilateral and map the bubble grid onto it.

Enable with "rectifyOnBorder": true on a field block or
alignment_params.rectify_on_border in config.json. The border is searched within
alignment_params.rectify_search_px of its expected place. "borderPadding" on the
block gives the gap between the bubbles' bounding box and the border; without it
the gap is estimated per sheet (which corrects shift, rotation and skew, but not
a uniform scale error).

A border that is not found, or a correction that is not plausible (corner moves
beyond the search margin, not near-rectangular, or the bubbles fit worse at the
corrected positions than before), leaves the page alignment untouched; the block's
fields get the rectify_failed flag.
"""

import cv2
import numpy as np

# A border side counts as found when this share of its segments see a line
MIN_SEGMENT_SHARE = 0.6
MIN_COVERAGE = 0.5
MAX_LINE_RMS = 1.5
MAX_CORNER_ANGLE_DEVIATION = 6.0  # degrees from 90
MAX_SIDE_RATIO_DEVIATION = 0.08
# Corrected positions must fit the printed bubbles at least this well (relative)
MIN_FIT_RATIO = 0.97


class RectifyResult:
    def __init__(self, ok, reason="", offsets=None, corners=None, expected=None):
        self.ok = ok
        self.reason = reason
        self.offsets = offsets
        self.corners = corners
        self.expected = expected

    @property
    def max_shift(self):
        if self.corners is None or self.expected is None:
            return None
        return float(np.max(np.linalg.norm(self.corners - self.expected, axis=1)))

    def to_dict(self):
        out = {"ok": self.ok, "reason": self.reason}
        if self.max_shift is not None:
            out["max_corner_shift"] = round(self.max_shift, 2)
        return out


def block_bubbles(field_block):
    return [bubble for strip in field_block.traverse_bubbles for bubble in strip]


def padding_of(field_block):
    padding = getattr(field_block, "border_padding", None)
    if padding is None:
        return None
    if isinstance(padding, (int, float)):
        return float(padding), float(padding)
    return float(padding[0]), float(padding[1])


def rectify_field_block(img, field_block, search_px):
    """
    Find the block's printed border and return a RectifyResult whose offsets
    are per-bubble (dx, dy) corrections relative to the template positions.
    """
    x0, y0 = (float(v) for v in field_block.origin)
    width, height = (float(v) for v in field_block.dimensions)
    x1, y1 = x0 + width, y0 + height
    padding = padding_of(field_block)
    search = float(search_px)
    # Where to look, as offsets outside the bubbles' bounding box
    if padding is None:
        centre_x = centre_y = search
    else:
        centre_x, centre_y = padding
    margin = int(np.ceil(max(centre_x, centre_y) + search + 3))
    img_h, img_w = img.shape[:2]
    left, top = int(x0) - margin, int(y0) - margin
    right, bottom = int(np.ceil(x1)) + margin, int(np.ceil(y1)) + margin
    if left < 0 or top < 0 or right > img_w or bottom > img_h:
        return RectifyResult(False, "search window outside the page")
    region = img[top:bottom, left:right]
    _, dark = cv2.threshold(region, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    box_w, box_h = field_block.bubble_dimensions
    run = int(max(box_w, box_h) * 1.5) + 1
    # Thicken across the line so slightly tilted lines stay continuous, then keep
    # only long straight runs (bubbles, letters and marks are shorter)
    horizontal = cv2.morphologyEx(
        cv2.dilate(dark, np.ones((3, 1), np.uint8)),
        cv2.MORPH_OPEN,
        np.ones((1, run), np.uint8),
    )
    vertical = cv2.morphologyEx(
        cv2.dilate(dark, np.ones((1, 3), np.uint8)),
        cv2.MORPH_OPEN,
        np.ones((run, 1), np.uint8),
    )

    # Expected line positions in region coordinates
    lo_x, hi_x = x0 - left, x1 - left
    lo_y, hi_y = y0 - top, y1 - top
    sides = {}
    for name, mask, along, expected, outward in (
        ("top", horizontal, (lo_x, hi_x), lo_y - centre_y, -1),
        ("bottom", horizontal, (lo_x, hi_x), hi_y + centre_y, 1),
        ("left", vertical.T, (lo_y, hi_y), lo_x - centre_x, -1),
        ("right", vertical.T, (lo_y, hi_y), hi_x + centre_x, 1),
    ):
        line = _fit_side(mask, along, expected, search)
        if line is None:
            return RectifyResult(False, f"{name} border not found")
        sides[name] = line

    corners = np.float32(
        [
            _intersect(sides["top"], sides["left"]),
            _intersect(sides["top"], sides["right"]),
            _intersect(sides["bottom"], sides["right"]),
            _intersect(sides["bottom"], sides["left"]),
        ]
    ) + np.float32([left, top])

    if padding is None:
        # Symmetric gap estimated from the border's mid-lines
        mid_y = (lo_y + hi_y) / 2
        mid_x = (lo_x + hi_x) / 2
        found_left = _at(sides["left"], mid_y)
        found_right = _at(sides["right"], mid_y)
        found_top = _at(sides["top"], mid_x)
        found_bottom = _at(sides["bottom"], mid_x)
        pad_x = ((lo_x - found_left) + (found_right - hi_x)) / 2
        pad_y = ((lo_y - found_top) + (found_bottom - hi_y)) / 2
        if min(pad_x, pad_y) < -2 or max(pad_x, pad_y) > 2 * search + 2:
            return RectifyResult(False, "border gap implausible")
    else:
        pad_x, pad_y = padding
    expected = np.float32(
        [
            [x0 - pad_x, y0 - pad_y],
            [x1 + pad_x, y0 - pad_y],
            [x1 + pad_x, y1 + pad_y],
            [x0 - pad_x, y1 + pad_y],
        ]
    )
    result = RectifyResult(False, corners=corners, expected=expected)
    if result.max_shift > search:
        result.reason = "correction larger than the search margin"
        return result
    if not _near_rectangular(corners):
        result.reason = "border is not near-rectangular"
        return result

    homography = cv2.getPerspectiveTransform(expected, corners)
    bubbles = block_bubbles(field_block)
    centres = np.float32(
        [[b.x + box_w / 2.0, b.y + box_h / 2.0] for b in bubbles]
    ).reshape(-1, 1, 2)
    moved = cv2.perspectiveTransform(centres, homography).reshape(-1, 2)
    offsets = np.rint(moved - centres.reshape(-1, 2)).astype(int)

    # Never make things worse silently: the printed bubbles must fit at least as
    # well at the corrected positions as at the page-aligned ones
    shift = (field_block.shift, getattr(field_block, "shift_y", 0))
    before = _bubble_fit(img, bubbles, box_w, box_h, [shift] * len(bubbles))
    after = _bubble_fit(img, bubbles, box_w, box_h, offsets)
    if after < before * MIN_FIT_RATIO:
        result.reason = "bubbles fit worse after rectification"
        return result
    result.ok = True
    result.offsets = [tuple(int(v) for v in o) for o in offsets]
    return result


def apply_offsets(field_block, offsets):
    for bubble, (dx, dy) in zip(block_bubbles(field_block), offsets):
        bubble.dx, bubble.dy = dx, dy
    field_block.shift, field_block.shift_y = 0, 0
    field_block.rectified = True


def reset_offsets(field_block):
    if getattr(field_block, "rectified", False):
        for bubble in block_bubbles(field_block):
            bubble.dx = bubble.dy = 0
        field_block.rectified = False


# --------------------------------------------------------------------------- helpers


def _fit_side(mask, along, expected, search):
    """
    Fit a near-horizontal line (in mask's orientation) close to `expected`.

    Returns (slope, intercept): position = slope * t + intercept, or None.
    """
    rows = mask.shape[0]
    lo = int(max(0, np.floor(expected - search)))
    hi = int(min(rows, np.ceil(expected + search) + 1))
    start, end = int(round(along[0])), int(round(along[1]))
    if hi - lo < 3 or end - start < 20:
        return None
    count = int(np.clip((end - start) // 40, 4, 12))
    edges = np.linspace(start, end, count + 1).astype(int)
    points = []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        coverage = mask[lo:hi, a:b].mean(axis=1)
        peak = int(np.argmax(coverage))
        if coverage[peak] < MIN_COVERAGE:
            continue
        # Prefer the line closest to the expected position among strong ones
        strong = np.flatnonzero(coverage >= max(MIN_COVERAGE, 0.8 * coverage[peak]))
        groups = np.split(strong, np.flatnonzero(np.diff(strong) > 1) + 1)
        best = min(groups, key=lambda g: abs(lo + g.mean() - expected))
        weights = coverage[best]
        position = lo + float(np.sum(best * weights) / np.sum(weights))
        points.append(((a + b) / 2.0, position))
    if len(points) < max(3, int(np.ceil(MIN_SEGMENT_SHARE * count))):
        return None
    points = np.float32(points)
    keep = np.ones(len(points), bool)
    for _ in range(2):
        slope, intercept = np.polyfit(points[keep, 0], points[keep, 1], 1)
        residual = np.abs(points[:, 1] - (slope * points[:, 0] + intercept))
        keep = residual <= 2.0
        if keep.sum() < max(3, int(np.ceil(MIN_SEGMENT_SHARE * count))):
            return None
    slope, intercept = np.polyfit(points[keep, 0], points[keep, 1], 1)
    residual = points[keep, 1] - (slope * points[keep, 0] + intercept)
    if float(np.sqrt(np.mean(residual * residual))) > MAX_LINE_RMS:
        return None
    if abs(slope) > np.tan(np.radians(MAX_CORNER_ANGLE_DEVIATION * 2)):
        return None
    return float(slope), float(intercept)


def _at(line, t):
    slope, intercept = line
    return slope * t + intercept


def _intersect(horizontal, vertical):
    """horizontal: y = a*x + b; vertical: x = c*y + d."""
    a, b = horizontal
    c, d = vertical
    y = (a * d + b) / (1.0 - a * c)
    x = c * y + d
    return x, y


def _near_rectangular(corners):
    for i in range(4):
        p_prev, p, p_next = corners[i - 1], corners[i], corners[(i + 1) % 4]
        u, v = p_prev - p, p_next - p
        cosine = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-9))
        if abs(np.degrees(np.arccos(np.clip(cosine, -1, 1))) - 90) > (
            MAX_CORNER_ANGLE_DEVIATION
        ):
            return False
    lengths = [np.linalg.norm(corners[(i + 1) % 4] - corners[i]) for i in range(4)]
    for a, b in ((lengths[0], lengths[2]), (lengths[1], lengths[3])):
        if abs(a - b) > MAX_SIDE_RATIO_DEVIATION * max(a, b):
            return False
    return True


def _bubble_fit(img, bubbles, box_w, box_h, offsets):
    """Mean darkness on the expected bubble outlines at the given positions."""
    centres = [
        (int(b.x + dx + box_w / 2), int(b.y + dy + box_h / 2))
        for b, (dx, dy) in zip(bubbles, offsets)
    ]
    xs = [c[0] for c in centres]
    ys = [c[1] for c in centres]
    pad = int(max(box_w, box_h))
    left, top = max(min(xs) - pad, 0), max(min(ys) - pad, 0)
    right = min(max(xs) + pad, img.shape[1])
    bottom = min(max(ys) + pad, img.shape[0])
    if right <= left or bottom <= top:
        return 0.0
    mask = np.zeros((bottom - top, right - left), np.uint8)
    axes = (max(int(box_w / 2) - 1, 1), max(int(box_h / 2) - 1, 1))
    for cx, cy in centres:
        cv2.ellipse(mask, (cx - left, cy - top), axes, 0, 0, 360, 255, 2)
    if not mask.any():
        return 0.0
    return 255.0 - cv2.mean(img[top:bottom, left:right], mask=mask)[0]
