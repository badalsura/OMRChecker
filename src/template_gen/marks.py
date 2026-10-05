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
