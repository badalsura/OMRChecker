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

Two-level search ("outerBorderPadding"): the outer frame first, then the inner
box relative to it. Blocks without a printed box can be fitted to their bubble
outlines instead ("blockPerspective" / alignment block_perspective), with the
same safety limits. The bubble-fit safety check can be switched off with
alignment verify_bubble_fit (default on).
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
    def __init__(
        self,
        ok,
        reason="",
        offsets=None,
        corners=None,
        expected=None,
        method="border",
        level=None,
        status=None,
    ):
        self.ok = ok
        self.reason = reason
        self.offsets = offsets
        self.corners = corners
        self.expected = expected
        # "border" (printed box) or "bubbles" (fitted to the bubble outlines)
        self.method = method
        # Two-level search: "outer" frame or "inner" box used; None: one level
        self.level = level
        self._status = status

    @property
    def max_shift(self):
        if self.corners is None or self.expected is None:
            return None
        return float(np.max(np.linalg.norm(self.corners - self.expected, axis=1)))

    @property
    def status(self):
        if self.ok:
            return "found" if self.method == "border" else "fitted"
        return self._status or "failed"

    def to_dict(self):
        out = {
            "ok": self.ok,
            "reason": self.reason,
            "status": self.status,
            "method": self.method,
            "level": self.level,
        }
        if self.max_shift is not None:
            out["max_corner_shift"] = round(self.max_shift, 2)
        if self.corners is not None:
            out["corners"] = _round_points(self.corners)
        if self.expected is not None:
            out["expected"] = _round_points(self.expected)
        return out


def _round_points(points):
    return [[round(float(x), 2), round(float(y), 2)] for x, y in points]


def block_bubbles(field_block):
    return [bubble for strip in field_block.traverse_bubbles for bubble in strip]


def _pair(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value), float(value)
    return float(value[0]), float(value[1])


def padding_of(field_block):
    return _pair(getattr(field_block, "border_padding", None))


def outer_padding_of(field_block):
    return _pair(getattr(field_block, "outer_border_padding", None))


def block_box(field_block):
    x0, y0 = (float(v) for v in field_block.origin)
    width, height = (float(v) for v in field_block.dimensions)
    return x0, y0, x0 + width, y0 + height


def _box_corners(x0, y0, x1, y1, pad_x=0.0, pad_y=0.0):
    return np.float32(
        [
            [x0 - pad_x, y0 - pad_y],
            [x1 + pad_x, y0 - pad_y],
            [x1 + pad_x, y1 + pad_y],
            [x0 - pad_x, y1 + pad_y],
        ]
    )


def find_border(img, field_block, padding, search_px, offset=(0.0, 0.0)):
    """
    Look for a printed rectangle `padding` outside the block's bubbles, its
    search window moved by `offset`. Returns (corners, expected corners) in
    image pixels (expected = where the template puts it, without the offset),
    or a reason string.
    """
    x0, y0, x1, y1 = block_box(field_block)
    ox, oy = float(offset[0]), float(offset[1])
    sx0, sy0, sx1, sy1 = x0 + ox, y0 + oy, x1 + ox, y1 + oy
    search = float(search_px)
    # Where to look, as offsets outside the bubbles' bounding box
    if padding is None:
        centre_x = centre_y = search
    else:
        centre_x, centre_y = padding
    margin = int(np.ceil(max(centre_x, centre_y) + search + 3))
    img_h, img_w = img.shape[:2]
    left, top = int(sx0) - margin, int(sy0) - margin
    right, bottom = int(np.ceil(sx1)) + margin, int(np.ceil(sy1)) + margin
    if left < 0 or top < 0 or right > img_w or bottom > img_h:
        return "search window outside the page"
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
    lo_x, hi_x = sx0 - left, sx1 - left
    lo_y, hi_y = sy0 - top, sy1 - top
    sides = {}
    for name, mask, along, expected, outward in (
        ("top", horizontal, (lo_x, hi_x), lo_y - centre_y, -1),
        ("bottom", horizontal, (lo_x, hi_x), hi_y + centre_y, 1),
        ("left", vertical.T, (lo_y, hi_y), lo_x - centre_x, -1),
        ("right", vertical.T, (lo_y, hi_y), hi_x + centre_x, 1),
    ):
        line = _fit_side(mask, along, expected, search)
        if line is None:
            return f"{name} border not found"
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
            return "border gap implausible"
    else:
        pad_x, pad_y = padding
    return corners, _box_corners(x0, y0, x1, y1, pad_x, pad_y)


def rectify_field_block(img, field_block, search_px, verify=True):
    """
    Find the block's printed border and return a RectifyResult whose offsets
    are per-bubble (dx, dy) corrections relative to the template positions.

    With "outerBorderPadding" on the block the search has two levels: the
    large outer frame is found first and the inner box is searched for where
    the outer frame puts it; when only the outer frame is found, its fit is
    used. verify=False (alignment verify_bubble_fit) skips the check that the
    printed bubbles fit at least as well after the correction.
    """
    search = float(search_px)
    outer_padding = outer_padding_of(field_block)
    offset = (0.0, 0.0)
    outer = None
    level = None
    if outer_padding is not None:
        outer = find_border(img, field_block, outer_padding, search)
        if isinstance(outer, str):
            return RectifyResult(
                False,
                f"outer {outer}",
                level="outer",
                status="skipped" if "outside the page" in outer else None,
            )
        homography = cv2.getPerspectiveTransform(outer[1], outer[0])
        x0, y0, x1, y1 = block_box(field_block)
        centre = np.float32([[[(x0 + x1) / 2, (y0 + y1) / 2]]])
        moved = cv2.perspectiveTransform(centre, homography)[0, 0]
        offset = (float(moved[0] - centre[0, 0, 0]), float(moved[1] - centre[0, 0, 1]))
        level = "inner"

    inner = find_border(img, field_block, padding_of(field_block), search, offset)
    allowed = search
    if isinstance(inner, str):
        if outer is None:
            return RectifyResult(
                False,
                inner,
                status="skipped" if "outside the page" in inner else None,
            )
        corners, expected = outer
        level = "outer"
    else:
        corners, expected = inner
        allowed = search + float(np.hypot(*offset))

    result = RectifyResult(False, corners=corners, expected=expected, level=level)
    if result.max_shift > allowed:
        result.reason = "correction larger than the search margin"
        return result
    if not _near_rectangular(corners):
        result.reason = "border is not near-rectangular"
        return result
    homography = cv2.getPerspectiveTransform(expected, corners)
    return _finish(img, field_block, result, homography, verify)


def _finish(img, field_block, result, homography, verify):
    """Per-bubble offsets from a block homography, after the bubble-fit check."""
    box_w, box_h = field_block.bubble_dimensions
    bubbles = block_bubbles(field_block)
    centres = np.float32(
        [[b.x + box_w / 2.0, b.y + box_h / 2.0] for b in bubbles]
    ).reshape(-1, 1, 2)
    moved = cv2.perspectiveTransform(centres, homography).reshape(-1, 2)
    offsets = np.rint(moved - centres.reshape(-1, 2)).astype(int)

    # Never make things worse silently: the printed bubbles must fit at least as
    # well at the corrected positions as at the page-aligned ones
    if verify:
        shift = (field_block.shift, getattr(field_block, "shift_y", 0))
        before = _bubble_fit(img, bubbles, box_w, box_h, [shift] * len(bubbles))
        after = _bubble_fit(img, bubbles, box_w, box_h, offsets)
        if after < before * MIN_FIT_RATIO:
            result.reason = "bubbles fit worse after rectification"
            return result
    result.ok = True
    result.offsets = [tuple(int(v) for v in o) for o in offsets]
    return result


# Bubble-outline fit (blocks without a printed box)
MIN_FITTED_BUBBLES = 6
MIN_RING_SCORE = 0.25


def fit_block_by_bubbles(img, field_block, search_px, verify=True):
    """
    Per-block perspective correction for a block without a printed box: find
    each printed bubble outline near its expected place, fit a homography to
    them (RANSAC) and map the block's corners and bubbles through it. Same
    safety limits as the border fit.
    """
    search = float(search_px)
    box_w, box_h = (float(v) for v in field_block.bubble_dimensions)
    bubbles = block_bubbles(field_block)
    x0, y0, x1, y1 = block_box(field_block)
    expected_box = _box_corners(x0, y0, x1, y1)
    if len(bubbles) < MIN_FITTED_BUBBLES:
        return RectifyResult(
            False, "too few bubbles to fit", expected=expected_box, method="bubbles"
        )
    centres = np.float32([[b.x + box_w / 2.0, b.y + box_h / 2.0] for b in bubbles])
    distances = np.linalg.norm(centres[:, None] - centres[None], axis=2)
    np.fill_diagonal(distances, np.inf)
    pitch = float(distances.min())
    radius = int(max(2, min(search, 0.45 * pitch)))
    shift = np.float32([field_block.shift, getattr(field_block, "shift_y", 0)])

    ring_w, ring_h = int(round(box_w)) + 4, int(round(box_h)) + 4
    ring = np.zeros((ring_h, ring_w), np.float32)
    cv2.ellipse(
        ring,
        (ring_w // 2, ring_h // 2),
        (max(int(box_w / 2) - 1, 1), max(int(box_h / 2) - 1, 1)),
        0,
        0,
        360,
        1.0,
        2,
    )
    img_h, img_w = img.shape[:2]
    darkness = 255.0 - img.astype(np.float32)
    expected_pts, found_pts = [], []
    for centre in centres:
        cx, cy = centre + shift
        left = int(round(cx - ring_w / 2.0)) - radius
        top = int(round(cy - ring_h / 2.0)) - radius
        right, bottom = left + ring_w + 2 * radius, top + ring_h + 2 * radius
        if left < 0 or top < 0 or right > img_w or bottom > img_h:
            continue
        window = darkness[top:bottom, left:right]
        if float(window.std()) < 1.0:
            continue
        scores = cv2.matchTemplate(window, ring, cv2.TM_CCOEFF_NORMED)
        _, best, _, (bx, by) = cv2.minMaxLoc(scores)
        if best < MIN_RING_SCORE or bx in (0, 2 * radius) or by in (0, 2 * radius):
            continue
        # Window centred on the expected place (to within half a pixel)
        found = (
            cx + bx - radius + _parabola(scores[by, bx - 1 : bx + 2]),
            cy + by - radius + _parabola(scores[by - 1 : by + 2, bx]),
        )
        expected_pts.append(centre)
        found_pts.append(found)
    if len(found_pts) < MIN_FITTED_BUBBLES:
        return RectifyResult(
            False, "too few bubble outlines found", expected=expected_box,
            method="bubbles",
        )
    expected_pts = np.float32(expected_pts)
    found_pts = np.float32(found_pts)
    spread = np.linalg.eigvalsh(np.cov((expected_pts - expected_pts.mean(0)).T))
    if spread[0] < 1.0:
        return RectifyResult(
            False, "bubbles lie on one line", expected=expected_box, method="bubbles"
        )
    homography, inliers = cv2.findHomography(
        expected_pts, found_pts, cv2.RANSAC, 2.0
    )
    if homography is None or int(inliers.sum()) < max(
        MIN_FITTED_BUBBLES, len(found_pts) // 2
    ):
        return RectifyResult(
            False, "bubble outlines do not agree", expected=expected_box,
            method="bubbles",
        )
    corners = cv2.perspectiveTransform(expected_box[None], homography)[0]
    result = RectifyResult(
        False, corners=corners, expected=expected_box, method="bubbles"
    )
    if result.max_shift > search:
        result.reason = "correction larger than the search margin"
        return result
    if not _near_rectangular(corners):
        result.reason = "fitted block is not near-rectangular"
        return result
    return _finish(img, field_block, result, homography, verify)


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


def _parabola(values):
    """Sub-pixel peak offset in [-0.5, 0.5] from three neighbouring scores."""
    if len(values) != 3:
        return 0.0
    a, b, c = (float(v) for v in values)
    denominator = a - 2 * b + c
    if abs(denominator) < 1e-9:
        return 0.0
    return float(np.clip(0.5 * (a - c) / denominator, -0.5, 0.5))


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
