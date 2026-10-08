"""
Timing tracks and corner markers.

Solid marks are connected components of the Otsu-thresholded reference page
with high extent (filled rectangles), high solidity and dark interiors. Marks
near an edge are clustered along the edge-parallel coordinate; a cluster with
at least min_marks marks of consistent size and a regular pitch is a timing
track. Big isolated solid squares near the page corners are corner markers.

markDimensions follow the convention used by the TimingMarkAlignment
options and src/synth: [w, h] of a mark on a vertical (left/right) track;
marks on horizontal tracks are the same rectangle rotated by 90 degrees.
"""

import cv2
import numpy as np

from src.template_gen.assignment import cluster_1d


def detect_solid_marks(gray, page_size):
    page_w, page_h = page_size
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    min_side, max_side = max(3, 0.003 * page_w), 0.06 * page_w
    # "Solid" relative to the paper so blurred or grey-printed marks still count
    max_mean = 0.65 * float(np.median(gray))
    marks = []
    for k in range(1, count):
        x, y, w, h, area = stats[k]
        if min(w, h) < min_side or max(w, h) > max_side:
            continue
        if not 0.15 <= w / h <= 6.5:
            continue
        if area / float(w * h) < 0.8:
            continue
        if float(gray[y : y + h, x : x + w].mean()) > max_mean:
            continue
        cx, cy = centroids[k]
        marks.append([float(cx), float(cy), float(w), float(h)])
    return np.array(marks) if marks else np.zeros((0, 4))


def _track_from(group, axis):
    """Validate a candidate track (axis 1: vertical track, sorted by y)."""
    if len(group) < 4:
        return None
    w_med, h_med = np.median(group[:, 2]), np.median(group[:, 3])
    same = (np.abs(group[:, 2] - w_med) <= 0.3 * w_med) & (
        np.abs(group[:, 3] - h_med) <= 0.3 * h_med
    )
    group = group[same]
    if len(group) < 4:
        return None
    group = group[np.argsort(group[:, axis])]
    gaps = np.diff(group[:, axis])
    pitch = np.median(gaps)
    if pitch <= 0:
        return None
    # Gaps should be (close to) whole multiples of the base pitch
    multiples = gaps / pitch
    irregular = np.abs(multiples - np.round(multiples)) > 0.2
    if irregular.mean() > 0.2:
        return None
    return group, float(pitch)


def detect_timing_tracks(gray, page_size, edge_fraction=0.15, min_marks=4):
    page_w, page_h = page_size
    marks = detect_solid_marks(gray, page_size)
    tracks = {}
    if len(marks) == 0:
        return tracks, marks
    sides = {
        "left": (marks[:, 0] < edge_fraction * page_w, 0, 1),
        "right": (marks[:, 0] > (1 - edge_fraction) * page_w, 0, 1),
        "top": (marks[:, 1] < edge_fraction * page_h, 1, 0),
        "bottom": (marks[:, 1] > (1 - edge_fraction) * page_h, 1, 0),
    }
    for side, (mask, across, along) in sides.items():
        subset = marks[mask]
        if len(subset) < min_marks:
            continue
        tolerance = 0.5 * np.median(subset[:, 2 + across])
        _, labels = cluster_1d(subset[:, across], tolerance)
        candidates = []
        for label in np.unique(labels):
            result = _track_from(subset[labels == label], along)
            if result is not None:
                candidates.append(result)
        candidates.sort(key=lambda c: -len(c[0]))
        for index, (group, pitch) in enumerate(candidates):
            name = side if index == 0 else f"{side}_{index + 1}"
            if along == 1:
                dims = [float(np.median(group[:, 2])), float(np.median(group[:, 3]))]
            else:  # rotate horizontal-track marks into the vertical convention
                dims = [float(np.median(group[:, 3])), float(np.median(group[:, 2]))]
            tracks[name] = {
                "marks": [[round(x, 1), round(y, 1)] for x, y in group[:, :2]],
                "pitch": round(pitch, 2),
                "mark_dimensions": [round(d, 1) for d in dims],
                "boxes": [
                    [x - w / 2, y - h / 2, w, h] for x, y, w, h in group.tolist()
                ],
            }
    return tracks, marks


def detect_corner_markers(marks, page_size, tracks, corner_fraction=0.15):
    page_w, page_h = page_size
    track_points = {
        tuple(p) for t in tracks.values() for p in np.round(t["marks"], 1).tolist()
    }
    track_area = [np.prod(t["mark_dimensions"]) for t in tracks.values() if t["marks"]]
    min_area = 1.5 * max(track_area) if track_area else 0
    corners = {}
    targets = {
        "top_left": (0, 0),
        "top_right": (page_w, 0),
        "bottom_right": (page_w, page_h),
        "bottom_left": (0, page_h),
    }
    for name, (tx, ty) in targets.items():
        best = None
        for cx, cy, w, h in marks:
            if (round(cx, 1), round(cy, 1)) in track_points:
                continue
            if not 0.7 <= w / h <= 1.4 or w * h < min_area:
                continue
            if abs(cx - tx) > corner_fraction * page_w:
                continue
            if abs(cy - ty) > corner_fraction * page_h:
                continue
            distance = np.hypot(cx - tx, cy - ty)
            if best is None or distance < best[0]:
                best = (distance, [round(cx, 1), round(cy, 1)], [w, h])
        if best:
            corners[name] = {"centre": best[1], "dimensions": best[2]}
    return corners


def _inside_any(x, y, boxes, pad=0.0):
    return any(
        bx - pad <= x <= bx + bw + pad and by - pad <= y <= by + bh + pad
        for bx, by, bw, bh in boxes
    )


def detect_index_candidates(
    gray, page_size, tracks, exclude_boxes=(), min_frac=0.005, max_frac=0.04
):
    """
    Small solid printed marks that can serve as index points: dots (as on the
    GNDU forms), squares and corner markers. A mark must be dark, solid, about
    as wide as tall and stand alone (clear paper around it), and lie outside
    the timing tracks and the given boxes (bubble grids, zones).
    Returns [{"center", "size", "shape", "area"}], biggest first.
    """
    page_w, page_h = page_size
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    min_side, max_side = max(4, min_frac * page_w), max_frac * page_w
    max_mean = 0.65 * float(np.median(gray))
    track_boxes = [b for t in tracks.values() for b in t.get("boxes", [])]
    track_labels = {0}
    for t in tracks.values():
        for mx, my in t["marks"]:
            ix, iy = int(round(mx)), int(round(my))
            if 0 <= iy < labels.shape[0] and 0 <= ix < labels.shape[1]:
                track_labels.add(int(labels[iy, ix]))
    track_labels = np.array(sorted(track_labels))
    out = []
    for k in range(1, count):
        x, y, w, h, area = stats[k]
        if min(w, h) < min_side or max(w, h) > max_side or not 0.7 <= w / h <= 1.4:
            continue
        extent = area / float(w * h)
        if extent < 0.6:
            continue
        if float(gray[y : y + h, x : x + w].mean()) > max_mean:
            continue
        cx, cy = (float(v) for v in centroids[k])
        if _inside_any(cx, cy, track_boxes, 2) or _inside_any(cx, cy, exclude_boxes, 4):
            continue
        # Isolated: little other ink in a ring one mark-size wide around it
        x0, y0 = max(0, x - w), max(0, y - h)
        x1, y1 = min(page_w, x + 2 * w), min(page_h, y + 2 * h)
        ring = labels[y0:y1, x0:x1]
        other = np.count_nonzero((ring != k) & ~np.isin(ring, track_labels))
        if other > 0.08 * ring.size:
            continue
        out.append(
            {
                "center": [round(cx, 1), round(cy, 1)],
                "size": [int(w), int(h)],
                "shape": "circle" if extent < 0.88 else "square",
                "area": int(area),
            }
        )
    out.sort(key=lambda c: -c["area"])
    return out


def tracks_symmetric(tracks, page_size, tolerance=0.3):
    """True when the track marks land on track marks after a 180 degree turn."""
    points = [p for t in tracks.values() for p in t["marks"]]
    if len(points) < 4:
        return False
    pitches = [t["pitch"] for t in tracks.values() if t.get("pitch")]
    tol = tolerance * (min(pitches) if pitches else 20.0)
    points = np.array(points, dtype=np.float64)
    rotated = np.column_stack([page_size[0] - points[:, 0], page_size[1] - points[:, 1]])
    distances = np.linalg.norm(rotated[:, None, :] - points[None, :, :], axis=2).min(axis=1)
    return bool(np.mean(distances <= tol) >= 0.9)


def asymmetric_points(candidates, page_size, max_points=4, gray=None):
    """
    Candidates with no candidate at their 180-degree-turned position: these
    tell which way up a sheet is. Spread out, biggest first.
    """
    if not candidates:
        return []
    largest = max(max(c["size"]) for c in candidates)
    big = [c for c in candidates if max(c["size"]) >= 0.5 * largest]
    chosen = _asymmetric(big, candidates, page_size, max_points, gray)
    if not chosen:
        chosen = _asymmetric(candidates, candidates, page_size, max_points, gray)
    return chosen


def _asymmetric(pool, candidates, page_size, max_points, gray):
    chosen = []
    for c in pool:
        cx, cy = c["center"]
        rx, ry = page_size[0] - cx, page_size[1] - cy
        tol = 2.0 * max(c["size"])
        twin = any(
            np.hypot(o["center"][0] - rx, o["center"][1] - ry) <= tol
            and abs(o["size"][0] - c["size"][0]) <= 0.4 * c["size"][0]
            for o in candidates
        )
        if not twin and gray is not None:
            # Also look at the page itself (a twin may have failed the
            # candidate tests, e.g. touching other print)
            other = find_mark_near(gray, [rx, ry], page_size, radius=tol)
            twin = bool(
                other
                and other["shape"] == c["shape"]
                and all(
                    abs(a - b) <= 0.4 * b for a, b in zip(other["size"], c["size"])
                )
            )
        if twin:
            continue
        far = all(
            np.hypot(cx - o["center"][0], cy - o["center"][1]) > 0.1 * page_size[1]
            for o in chosen
        )
        if far:
            chosen.append(c)
        if len(chosen) >= max_points:
            break
    return chosen


def index_point(candidate, name, required=True):
    """A TimingMarkAlignment indexPoints entry (contract shape)."""
    return {
        "name": name,
        "center": [float(v) for v in candidate["center"]],
        "size": [int(v) for v in candidate["size"]],
        "shape": candidate.get("shape", "any"),
        "required": bool(required),
    }


def find_marks_in_box(gray, box, page_size):
    """
    Solid marks inside a user-drawn box (x, y, w, h), as a timing track:
    {"marks", "pitch", "mark_dimensions", "boxes", "orientation"} or None.
    """
    x, y, w, h = (int(round(v)) for v in box)
    page_w, page_h = page_size
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(page_w, x + w), min(page_h, y + h)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    crop = gray[y0:y1, x0:x1]
    _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    found = []
    for k in range(1, count):
        bx, by, bw, bh, area = stats[k]
        if bw < 3 or bh < 3 or area / float(bw * bh) < 0.6:
            continue
        found.append([centroids[k][0] + x0, centroids[k][1] + y0, float(bw), float(bh)])
    if len(found) < 2:
        return None
    found = np.array(found)
    w_med, h_med = np.median(found[:, 2]), np.median(found[:, 3])
    same = (np.abs(found[:, 2] - w_med) <= 0.35 * w_med) & (
        np.abs(found[:, 3] - h_med) <= 0.35 * h_med
    )
    found = found[same]
    if len(found) < 2:
        return None
    vertical = (y1 - y0) >= (x1 - x0)
    axis = 1 if vertical else 0
    found = found[np.argsort(found[:, axis])]
    gaps = np.diff(found[:, axis])
    pitch = float(np.median(gaps)) if len(gaps) else 0.0
    if vertical:
        dims = [float(np.median(found[:, 2])), float(np.median(found[:, 3]))]
    else:
        dims = [float(np.median(found[:, 3])), float(np.median(found[:, 2]))]
    return {
        "marks": [[round(float(a), 1), round(float(b), 1)] for a, b in found[:, :2]],
        "pitch": round(pitch, 2),
        "mark_dimensions": [round(d, 1) for d in dims],
        "boxes": [[a - c / 2, b - d / 2, c, d] for a, b, c, d in found.tolist()],
        "orientation": "vertical" if vertical else "horizontal",
    }


def find_mark_near(gray, point, page_size, radius=None, box=None):
    """
    The solid mark under a click (or inside a drawn box): an index point
    candidate {"center", "size", "shape", "area"} or None. Works for small dots.
    """
    page_w, page_h = page_size
    if box is not None:
        x, y, w, h = (int(round(v)) for v in box)
    else:
        r = int(radius or max(12, 0.02 * page_w))
        x, y, w, h = int(point[0]) - r, int(point[1]) - r, 2 * r, 2 * r
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(page_w, x + w), min(page_h, y + h)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    crop = gray[y0:y1, x0:x1]
    _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    count, _, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    target = np.array(point if point is not None else [(x0 + x1) / 2, (y0 + y1) / 2])
    best = None
    for k in range(1, count):
        bx, by, bw, bh, area = stats[k]
        extent = area / float(bw * bh)
        if bw < 3 or bh < 3 or extent < 0.5:
            continue
        cx, cy = centroids[k][0] + x0, centroids[k][1] + y0
        score = area / (1.0 + np.hypot(cx - target[0], cy - target[1]))
        if best is None or score > best[0]:
            best = (
                score,
                {
                    "center": [round(float(cx), 1), round(float(cy), 1)],
                    "size": [int(bw), int(bh)],
                    "shape": "circle" if extent < 0.88 else "square",
                    "area": int(area),
                },
            )
    return best[1] if best else None


def match_counts(gray, tracks, index_points=(), tolerance=0.35):
    """
    How many template track marks (and index points) are found on one aligned
    sheet: {"tracks": {name: [found, expected]}, "missed": [[x, y], ...],
    "index_points": {name: bool}}.
    """
    h, w = gray.shape[:2]
    solid = detect_solid_marks(gray, (w, h))
    out = {"tracks": {}, "missed": [], "index_points": {}}
    for name, track in tracks.items():
        expected = np.array(track["marks"], dtype=np.float64)
        pitch = track.get("pitch") or 20.0
        if len(solid):
            d = np.linalg.norm(expected[:, None, :] - solid[None, :, :2], axis=2).min(axis=1)
            hit = d <= tolerance * pitch
        else:
            hit = np.zeros(len(expected), bool)
        out["tracks"][name] = [int(hit.sum()), int(len(expected))]
        out["missed"].extend([[float(x), float(y)] for x, y in expected[~hit]])
    for point in index_points:
        found = find_mark_near(gray, point["center"], (w, h), radius=1.5 * max(point["size"]))
        ok = bool(
            found
            and np.hypot(found["center"][0] - point["center"][0],
                         found["center"][1] - point["center"][1]) <= max(point["size"])
        )
        out["index_points"][point["name"]] = ok
    return out
