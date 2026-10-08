"""
Printed bubble detection and grouping into regular grids (field blocks).

Detection runs on the "blank page": a per-pixel high quantile of all
registered sheets. Marks only darken paper, so as long as a bubble is left
empty on a fifth of the sheets its clean printed outline reappears (an
order-statistic background estimate, like temporal median background
subtraction but robust to bubbles that most sheets fill). Candidates are contours of
an adaptive (local mean) threshold filtered by size, aspect ratio, extent,
solidity and circularity, which accepts circles, ellipses/ovals and rounded
squares alike.

Grouping links every bubble to its nearest right and lower neighbour of
similar size, takes connected components, and recursively splits each
component at gaps that break the regular row/column pitch (and at changes in
the per-row occupancy pattern) until every part is a regular lattice. Pitch
and origin are fitted by least squares over all lattice centres.
"""

from dataclasses import dataclass, field
from typing import List

import cv2
import numpy as np

from src.template_gen.assignment import cluster_1d, odd


@dataclass
class Grid:
    """A regular lattice of bubbles. Centres are in page pixels."""

    x0: float  # centre of the first column
    y0: float  # centre of the first row
    dx: float  # column pitch
    dy: float  # row pitch
    cols: int
    rows: int
    bubble: List[float]  # [w, h]
    occupancy: float = 1.0
    residual: float = 0.0
    support: List[List[int]] = field(default_factory=list)  # [row][col] 0/1

    def centres(self):
        xs = self.x0 + self.dx * np.arange(self.cols)
        ys = self.y0 + self.dy * np.arange(self.rows)
        return np.stack(np.meshgrid(xs, ys), axis=-1)  # (rows, cols, 2)

    def bbox(self):
        w, h = self.bubble
        x1 = self.x0 + self.dx * (self.cols - 1)
        y1 = self.y0 + self.dy * (self.rows - 1)
        return [self.x0 - w / 2, self.y0 - h / 2, x1 - self.x0 + w, y1 - self.y0 + h]

    @property
    def confidence(self):
        pitch = max(min(self.dx or 1e9, self.dy or 1e9), 1.0)
        regularity = max(0.0, 1.0 - self.residual / (0.25 * pitch))
        return round(float(self.occupancy * regularity), 3)


def detect_bubble_candidates(gray, page_size):
    """Return an (N, 5) array: cx, cy, w, h, filled (0/1)."""
    page_w = page_size[0]
    min_d, max_d = 0.006 * page_w, 0.07 * page_w
    block = odd(page_w / 40)
    contours = []
    # Two sensitivities: crisp outlines, and faint/grey-printed ones
    for offset in (8, 4):
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, offset
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        found_contours, _ = cv2.findContours(
            binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )
        contours.extend(found_contours)
    found = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (min_d <= w <= max_d and min_d <= h <= max_d):
            continue
        if not 0.5 <= w / h <= 2.0:
            continue
        area = cv2.contourArea(contour)
        if area <= 0:
            continue
        extent = area / (w * h)
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        solidity = area / max(hull_area, 1)
        perimeter = cv2.arcLength(contour, True)
        circularity = 4 * np.pi * area / max(perimeter**2, 1)
        if extent < 0.6 or solidity < 0.9 or circularity < 0.6:
            continue
        inner = gray[
            y + int(h * 0.3) : y + max(int(h * 0.7), int(h * 0.3) + 1),
            x + int(w * 0.3) : x + max(int(w * 0.7), int(w * 0.3) + 1),
        ]
        filled = float(inner.mean()) < 110 if inner.size else False
        found.append([x + w / 2.0, y + h / 2.0, float(w), float(h), float(filled)])
    if not found:
        return np.zeros((0, 5))
    return _dedupe(np.array(found))


def _dedupe(cands):
    """Merge near-coincident contours (outer/inner edge of the same ring)."""
    order = np.argsort(-(cands[:, 2] * cands[:, 3]))
    kept = []
    for index in order:
        cx, cy, w, h, _ = cands[index]
        duplicate = False
        for k in kept:
            if (
                abs(cands[k, 0] - cx) < 0.3 * cands[k, 2]
                and abs(cands[k, 1] - cy) < 0.3 * cands[k, 3]
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(index)
    return cands[sorted(kept)]


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def _components(cands, max_link=3.0):
    n = len(cands)
    uf = _UnionFind(n)
    cx, cy, w, h = cands[:, 0], cands[:, 1], cands[:, 2], cands[:, 3]
    for i in range(n):
        size_ok = (np.abs(w - w[i]) < 0.3 * w[i]) & (np.abs(h - h[i]) < 0.3 * h[i])
        dx, dy = cx - cx[i], cy - cy[i]
        right = size_ok & (dx > 0.6 * w[i]) & (dx < max_link * w[i])
        right &= np.abs(dy) < 0.3 * h[i]
        if right.any():
            uf.union(i, int(np.argmin(np.where(right, dx, np.inf))))
        down = size_ok & (dy > 0.6 * h[i]) & (dy < max_link * h[i])
        down &= np.abs(dx) < 0.3 * w[i]
        if down.any():
            uf.union(i, int(np.argmin(np.where(down, dy, np.inf))))
    groups = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    return [np.array(g) for g in groups.values()]


def _gap_split(centres, labels, factor=1.4):
    """Index sets split at gaps larger than factor * median pitch, or None."""
    if len(centres) < 3:
        return None
    gaps = np.diff(centres)
    pitch = np.median(gaps)
    big = np.nonzero(gaps > factor * pitch)[0]
    if len(big) == 0:
        return None
    bounds = [0] + list(big + 1) + [len(centres)]
    return [
        np.nonzero((labels >= a) & (labels < b))[0] for a, b in zip(bounds, bounds[1:])
    ]


def _fit_axis(centres):
    """Least-squares (start, pitch, residual) for roughly evenly spaced centres."""
    if len(centres) == 1:
        return float(centres[0]), 0.0, 0.0
    pitch = np.median(np.diff(centres))
    index = np.round((centres - centres[0]) / pitch)
    slope, intercept = np.polyfit(index, centres, 1)
    residual = float(np.abs(centres - (intercept + slope * index)).max())
    return float(intercept), float(slope), residual


def _row_runs(cells):
    """Consecutive rows that share the same occupied columns."""
    patterns = [tuple(np.nonzero(r)[0]) for r in cells]
    runs, start = [], 0
    for k in range(1, len(patterns) + 1):
        if k == len(patterns) or patterns[k] != patterns[start]:
            runs.append((start, k))
            start = k
    return runs


def _bubble_like(image, cands, points, min_score=0.5):
    """Share of `points` (x, y) whose appearance matches the mean candidate."""
    if image is None or len(points) == 0 or len(cands) < 2:
        return 0.0
    w, h = np.median(cands[:, 2]), np.median(cands[:, 3])
    half_w, half_h = int(round(w * 0.65)), int(round(h * 0.65))
    page_h, page_w = image.shape[:2]

    def crop(cx, cy, pad=0):
        x, y = int(round(cx)), int(round(cy))
        x0, y0 = x - half_w - pad, y - half_h - pad
        x1, y1 = x + half_w + pad + 1, y + half_h + pad + 1
        if x0 < 0 or y0 < 0 or x1 > page_w or y1 > page_h:
            return None
        return image[y0:y1, x0:x1].astype(np.float32)

    patches = [crop(cx, cy) for cx, cy in cands[:, :2]]
    patches = [p for p in patches if p is not None]
    if len(patches) < 2:
        return 0.0
    mean_patch = np.mean(patches, axis=0)
    if mean_patch.std() < 1:
        return 0.0
    hits = 0
    for cx, cy in points:
        window = crop(cx, cy, pad=2)
        if window is not None:
            score = cv2.matchTemplate(window, mean_patch, cv2.TM_CCOEFF_NORMED).max()
            hits += int(score >= min_score)
    return hits / float(len(points))


def _prune_sparse_lines(cands, sparse=0.35, dense=0.8, min_lines=4, image=None):
    """
    Drop lattice columns (rows) that only a few rows (columns) occupy while
    others are nearly full: printed question numbers, shaded cells or the 0/1
    "hundreds" column of a ragged digit grid that joined a regular block.
    (Ragged columns are re-attached later by find_ragged_columns.)
    A sparse line whose empty cells look like bubbles on the page (faint
    print, a row of missed outlines) is kept for verify_missing_cells.
    """
    for _ in range(3):
        if len(cands) < 4:
            break
        w, h = np.median(cands[:, 2]), np.median(cands[:, 3])
        col_c, col_l = cluster_1d(cands[:, 0], 0.35 * w)
        row_c, row_l = cluster_1d(cands[:, 1], 0.35 * h)
        keep = np.ones(len(cands), bool)
        for line_c, line_l, other_c, other_l, axis in (
            (col_c, col_l, row_c, row_l, 0),
            (row_c, row_l, col_c, col_l, 1),
        ):
            if len(other_c) < min_lines or len(line_c) < 2:
                continue
            share = np.bincount(line_l, minlength=len(line_c)) / float(len(other_c))
            if share.max() < dense:
                continue
            dense_mask = share[line_l] >= dense
            for k in np.nonzero(share < sparse)[0]:
                present = set(other_l[line_l == k].tolist())
                missing = [j for j in range(len(other_c)) if j not in present]
                points = [
                    (line_c[k], other_c[j]) if axis == 0 else (other_c[j], line_c[k])
                    for j in missing
                ]
                if _bubble_like(image, cands[dense_mask], points) >= 0.75:
                    continue
                keep &= line_l != k
        if keep.all() or not keep.any():
            break
        cands = cands[keep]
    return cands


def _trim_odd_edges(cands, tolerance=0.2):
    """
    Drop a first/last row or column whose outlines are a different size from
    the rest: the handwriting box row above a digit grid, or printed boxed
    question numbers beside an answer row.
    """
    for axis, size_axis in ((1, 3), (0, 2)):
        for _ in range(2):
            if len(cands) < 6:
                break
            ref = np.median(cands[:, 3 if axis == 1 else 2])
            centres, labels = cluster_1d(cands[:, axis], 0.35 * ref)
            if len(centres) < 3:
                break
            sizes_w = np.array([np.median(cands[labels == k, 2]) for k in range(len(centres))])
            sizes_h = np.array([np.median(cands[labels == k, 3]) for k in range(len(centres))])
            dropped = False
            for edge in (0, len(centres) - 1):
                others = [k for k in range(len(centres)) if k != edge]
                mw, mh = np.median(sizes_w[others]), np.median(sizes_h[others])
                if (
                    abs(sizes_w[edge] - mw) > tolerance * mw
                    or abs(sizes_h[edge] - mh) > tolerance * mh
                ):
                    cands = cands[labels != edge]
                    dropped = True
                    break
            if not dropped:
                break
    return cands


def _regular_blocks(cands, depth=0, min_occupancy=0.85):
    if len(cands) < 2 or depth > 12:
        return []
    cands = _trim_odd_edges(cands)
    if len(cands) < 2:
        return []
    w, h = np.median(cands[:, 2]), np.median(cands[:, 3])
    col_c, col_l = cluster_1d(cands[:, 0], 0.35 * w)
    row_c, row_l = cluster_1d(cands[:, 1], 0.35 * h)
    for centres, labels in ((col_c, col_l), (row_c, row_l)):
        parts = _gap_split(centres, labels)
        if parts:
            return [
                block
                for part in parts
                for block in _regular_blocks(cands[part], depth + 1, min_occupancy)
            ]
    cells = np.zeros((len(row_c), len(col_c)), dtype=int)
    cells[row_l, col_l] = 1
    occupancy = cells.mean()
    if occupancy < 0.85 and len(row_c) > 1:
        runs = _row_runs(cells)
        # A few long runs: genuinely different blocks stacked on one lattice.
        # Many short runs: scattered missed detections, handled by verification.
        clean = 1 < len(runs) <= max(2, len(row_c) // 3)
        if (clean or occupancy < min_occupancy) and len(runs) > 1:
            return [
                block
                for a, b in runs
                for block in _regular_blocks(
                    cands[(row_l >= a) & (row_l < b)], depth + 1, min_occupancy
                )
            ]
    if occupancy < min_occupancy or len(col_c) * len(row_c) < 2:
        return []
    x0, dx, rx = _fit_axis(col_c)
    y0, dy, ry = _fit_axis(row_c)
    return [
        Grid(
            x0=x0,
            y0=y0,
            dx=dx,
            dy=dy,
            cols=len(col_c),
            rows=len(row_c),
            bubble=[float(w), float(h)],
            occupancy=round(float(occupancy), 3),
            residual=round(max(rx, ry), 3),
            support=cells.tolist(),
        )
    ]


def verify_missing_cells(grid, image, min_score=0.4):
    """
    Check lattice cells without a detected outline against the mean appearance
    of the detected ones (normalised cross-correlation, +-2 px search).
    Updates grid.support / grid.occupancy in place.
    """
    support = np.array(grid.support)
    if support.all():
        return grid
    w, h = grid.bubble
    half_w, half_h = int(round(w * 0.65)), int(round(h * 0.65))
    centres = grid.centres()
    page_h, page_w = image.shape[:2]

    def crop(cx, cy, pad=0):
        x, y = int(round(cx)), int(round(cy))
        x0, y0 = x - half_w - pad, y - half_h - pad
        x1, y1 = x + half_w + pad + 1, y + half_h + pad + 1
        if x0 < 0 or y0 < 0 or x1 > page_w or y1 > page_h:
            return None
        return image[y0:y1, x0:x1].astype(np.float32)

    patches = [crop(*centres[r, c]) for r, c in zip(*np.nonzero(support))]
    patches = [p for p in patches if p is not None]
    if len(patches) < 2:
        return grid
    mean_patch = np.mean(patches, axis=0)
    if mean_patch.std() < 1:
        return grid
    for r, c in zip(*np.nonzero(support == 0)):
        window = crop(*centres[r, c], pad=2)
        if window is None:
            continue
        score = cv2.matchTemplate(window, mean_patch, cv2.TM_CCOEFF_NORMED).max()
        if score >= min_score:
            support[r, c] = 1
    grid.support = support.tolist()
    grid.occupancy = round(float(support.mean()), 3)
    return grid


def group_into_grids(
    cands, image=None, min_bubbles=4, min_pitch_ratio=1.12, min_occupancy=0.85
):
    """
    Cluster bubble candidates into regular grids; returns (grids, rejected).
    With the page image, sparse lattices (occupancy >= 0.5) are accepted when
    appearance verification of the missing cells lifts them to min_occupancy.
    """
    grids, rejected = [], []
    if len(cands) == 0:
        return grids, rejected
    first_pass = 0.5 if image is not None else min_occupancy
    for component in _components(cands):
        if len(component) < 2:
            continue
        pruned = _prune_sparse_lines(cands[component], image=image)
        for grid in _regular_blocks(pruned, min_occupancy=first_pass):
            if image is not None and grid.occupancy < 1:
                verify_missing_cells(grid, image)
                if grid.occupancy < min_occupancy:
                    rejected.append({"grid": grid, "reason": "sparse_lattice"})
                    continue
            pitches = [p for p in (grid.dx, grid.dy) if p > 0]
            sizes = [grid.bubble[0] if grid.dx > 0 else None]
            sizes.append(grid.bubble[1] if grid.dy > 0 else None)
            ratios = [p / s for p, s in zip((grid.dx, grid.dy), sizes) if s and p > 0]
            if grid.rows * grid.cols < min_bubbles or not pitches:
                rejected.append({"grid": grid, "reason": "too_few_bubbles"})
            elif min(ratios) < min_pitch_ratio:
                # Touching cells are character boxes, not bubbles
                rejected.append({"grid": grid, "reason": "touching_cells"})
            else:
                grids.append(grid)
    grids = _merge_stacked(_drop_overlapping(grids, rejected))
    if image is not None:
        for grid in grids:
            _extend_faint_edges(grid, grids, image)
        grids = _merge_stacked(grids)
    grids.sort(key=lambda g: (g.x0, g.y0))
    return grids, rejected


def _extend_faint_edges(grid, grids, image, min_share=1.0):
    """
    Add a whole first/last row whose outlines were too faint to detect (e.g. a
    shaded first answer row): every cell one pitch beyond the edge must look
    like the grid's bubbles and lie outside every other grid.
    """
    if grid.rows < 2 or grid.dy <= 0:
        return grid
    w, h = grid.bubble
    centres = grid.centres().reshape(-1, 2)
    cands = np.column_stack([centres, np.full(len(centres), w), np.full(len(centres), h)])
    others = [g.centres().reshape(-1, 2) for g in grids if g is not grid]
    for side in ("top", "bottom"):
        y = grid.y0 - grid.dy if side == "top" else grid.y0 + grid.rows * grid.dy
        if y - h < 0 or y + h >= image.shape[0]:
            continue
        xs = grid.x0 + grid.dx * np.arange(grid.cols)
        points = [(x, y) for x in xs]
        if any(
            ((np.abs(o[:, 0] - x) < 0.6 * w) & (np.abs(o[:, 1] - y) < 0.6 * h)).any()
            for o in others
            for x, y in points
        ):
            continue
        if _bubble_like(image, cands, points, min_score=0.6) < min_share:
            continue
        if side == "top":
            grid.y0 -= grid.dy
            grid.support = [[1] * grid.cols] + list(grid.support)
        else:
            grid.support = list(grid.support) + [[1] * grid.cols]
        grid.rows += 1
        grid.extended = getattr(grid, "extended", 0) + 1
    return grid


def _merge_stacked(grids):
    """
    Join grids that continue each other vertically on the same lattice (same
    columns and pitch, next row exactly one pitch below). Missed or filled
    bubbles can otherwise break a long answer column into several pieces.
    """
    grids = sorted(grids, key=lambda g: (g.x0, g.y0))
    merged = True
    while merged:
        merged = False
        for a in grids:
            for b in grids:
                if a is b or a.cols != b.cols or b.y0 <= a.y0:
                    continue
                w, h = a.bubble
                if abs(a.x0 - b.x0) > 0.3 * w:
                    continue
                if abs(a.bubble[0] - b.bubble[0]) > 0.2 * w or abs(
                    a.bubble[1] - b.bubble[1]
                ) > 0.2 * h:
                    # e.g. the handwriting box row above a digit grid
                    continue
                if a.cols > 1 and abs(a.dx - b.dx) > 0.05 * max(a.dx, b.dx):
                    continue
                pitches = [p for p in (a.dy, b.dy) if p > 0]
                pitch = np.mean(pitches) if pitches else None
                if pitch is None or any(abs(p - pitch) > 0.05 * pitch for p in pitches):
                    continue
                gap = b.y0 - (a.y0 + a.dy * (a.rows - 1))
                if abs(gap - pitch) > 0.25 * pitch:
                    continue
                rows = a.rows + b.rows
                support = a.support + b.support
                ys = np.concatenate(
                    [a.y0 + a.dy * np.arange(a.rows), b.y0 + b.dy * np.arange(b.rows)]
                )
                y0, dy, ry = _fit_axis(ys)
                a.y0, a.dy, a.rows, a.support = y0, dy, rows, support
                a.x0 = (a.x0 * a.cols + b.x0 * b.cols) / (a.cols + b.cols)
                a.occupancy = round(float(np.mean(support)), 3)
                a.residual = round(max(a.residual, b.residual, ry), 3)
                grids.remove(b)
                merged = True
                break
            if merged:
                break
    return grids


def _drop_overlapping(grids, rejected):
    """Keep the larger of two grids whose bubbles land inside each other."""
    kept = []
    for grid in sorted(grids, key=lambda g: -g.rows * g.cols):
        centres = grid.centres().reshape(-1, 2)
        inside = 0
        for other in kept:
            x, y, w, h = other.bbox()
            inside = max(
                inside,
                np.mean(
                    (centres[:, 0] >= x)
                    & (centres[:, 0] <= x + w)
                    & (centres[:, 1] >= y)
                    & (centres[:, 1] <= y + h)
                ),
            )
        if inside > 0.5:
            rejected.append({"grid": grid, "reason": "inside_another_grid"})
        else:
            kept.append(grid)
    return kept


def _box_means(integral, boxes):
    x0, y0, x1, y1, area = boxes
    total = integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
    return total / area


def sample_fill(images, grids, reference):
    """
    Darkness of every bubble interior (central 60% box) relative to the blank
    reference form, for each sheet. Returns one (S, rows, cols) array per grid,
    positive values meaning darker than the blank form.
    """
    page_h, page_w = reference.shape[:2]
    boxes = []
    for grid in grids:
        centres = grid.centres().reshape(-1, 2)
        half_w = max(int(grid.bubble[0] * 0.3), 1)
        half_h = max(int(grid.bubble[1] * 0.3), 1)
        xs = np.round(centres[:, 0]).astype(int)
        ys = np.round(centres[:, 1]).astype(int)
        x0 = np.clip(xs - half_w, 0, page_w - 1)
        x1 = np.clip(xs + half_w + 1, 1, page_w)
        y0 = np.clip(ys - half_h, 0, page_h - 1)
        y1 = np.clip(ys + half_h + 1, 1, page_h)
        boxes.append((x0, y0, x1, y1, np.maximum((x1 - x0) * (y1 - y0), 1)))

    def per_image(image):
        integral = cv2.integral(image, sdepth=cv2.CV_32S)
        return [_box_means(integral, b) for b in boxes]

    base = [_constant_marks_lifted(b) for b in per_image(reference)]
    sheets = [per_image(image) for image in images]
    return [
        base[k].reshape(1, g.rows, g.cols)
        - np.stack([sheet[k] for sheet in sheets]).reshape(len(images), g.rows, g.cols)
        for k, g in enumerate(grids)
    ]


def _constant_marks_lifted(base, margin=40.0):
    """
    A bubble marked on every sheet (e.g. a constant exam code) is dark on the
    blank page too, so its fill would read as zero. Such bubbles get the
    grid's typical unmarked level as their blank value instead.
    """
    base = np.asarray(base, dtype=np.float64)
    if base.size < 3:
        return base
    paper = float(np.percentile(base, 75))
    return np.where(base < paper - margin, paper, base)


def find_ragged_columns(grids, cands, min_rows=5):
    """
    Short columns beside a digit grid that start on its first row and share its
    row pitch: the 0/1 "hundreds" or "tens" column of a number such as marks
    (0..199). Returns (new ragged Grids, indices of grids they replace).

    Each ragged column becomes its own one-field block (grid.ragged_of is the
    index of its main grid) so the engine reads it with fewer values; the
    template joins them with a customLabel. Spurious grids whose bubbles all
    belong to ragged columns (e.g. two 0/1 columns taken for an "AB" row) are
    replaced.
    """
    if len(cands) == 0:
        return [], set()
    ragged = []
    for index, grid in enumerate(grids):
        if grid.rows < min_rows or grid.dy <= 0:
            continue
        pitch_x = grid.dx if grid.cols > 1 and grid.dx > 0 else grid.dy
        w, h = grid.bubble
        size_ok = (np.abs(cands[:, 2] - w) < 0.3 * w) & (np.abs(cands[:, 3] - h) < 0.3 * h)
        for x in (grid.x0 - pitch_x, grid.x0 + grid.cols * pitch_x):
            near_x = size_ok & (np.abs(cands[:, 0] - x) < 0.3 * w)
            present = []
            for r in range(grid.rows):
                y = grid.y0 + r * grid.dy
                present.append(bool((near_x & (np.abs(cands[:, 1] - y) < 0.3 * h)).any()))
            k = 0
            while k < len(present) and present[k]:
                k += 1
            if not 2 <= k < grid.rows or any(present[k:]):
                continue
            if any(_covers(g, x, grid.y0, w, h) for g in grids if g is not grid):
                # Already part of a real grid (only spurious ones are replaced below)
                owner = next(g for g in grids if g is not grid and _covers(g, x, grid.y0, w, h))
                if owner.rows >= min_rows:
                    continue
            column = Grid(
                x0=float(x),
                y0=float(grid.y0),
                dx=0.0,
                dy=float(grid.dy),
                cols=1,
                rows=k,
                bubble=[float(w), float(h)],
                occupancy=1.0,
                residual=0.0,
                support=[[1] for _ in range(k)],
            )
            column.ragged_of = index
            ragged.append(column)
    replaced = set()
    for index, grid in enumerate(grids):
        centres = grid.centres().reshape(-1, 2)
        w, h = grid.bubble
        claimed = [
            any(
                abs(cx - col.x0) < 0.3 * w
                and -0.3 * h < cy - col.y0 < (col.rows - 0.7) * col.dy + 0.3 * h
                for col in ragged
            )
            for cx, cy in centres
        ]
        if claimed and all(claimed):
            replaced.add(index)
    return ragged, replaced


def _covers(grid, x, y, w, h):
    centres = grid.centres().reshape(-1, 2)
    return bool(
        ((np.abs(centres[:, 0] - x) < 0.3 * w) & (np.abs(centres[:, 1] - y) < 0.3 * h)).any()
    )


def split_size_outliers(grids, tolerance=0.25):
    """
    (kept, outliers): grids whose bubbles differ from the dominant bubble size
    by more than `tolerance` (printed question numbers, handwriting boxes) are
    outliers unless they are big regular blocks themselves.
    """
    if len(grids) < 2:
        return list(grids), []
    sizes = np.array([g.bubble for g in grids], dtype=np.float64)
    weights = np.array([g.rows * g.cols for g in grids], dtype=np.float64)
    order = np.argsort(sizes[:, 0])
    cum = np.cumsum(weights[order])
    w_dom = sizes[order][np.searchsorted(cum, cum[-1] / 2.0), 0]
    order = np.argsort(sizes[:, 1])
    cum = np.cumsum(weights[order])
    h_dom = sizes[order][np.searchsorted(cum, cum[-1] / 2.0), 1]
    kept, outliers = [], []
    for grid in grids:
        w, h = grid.bubble
        off = abs(w - w_dom) > tolerance * w_dom or abs(h - h_dom) > tolerance * h_dom
        if off and grid.rows * grid.cols < 20:
            outliers.append(grid)
        else:
            kept.append(grid)
    return kept, outliers


def box_row_above(row, grids):
    """The digit grid a one-row grid of boxes sits directly above (or None)."""
    if row.rows != 1:
        return None
    for index, grid in enumerate(grids):
        if grid.cols != row.cols or grid.rows < 2:
            continue
        if grid.cols > 1 and abs(grid.dx - row.dx) > 0.1 * grid.dx:
            continue
        if abs(grid.x0 - row.x0) > 0.4 * grid.bubble[0]:
            continue
        gap = grid.y0 - row.y0
        if 0 < gap < 2.5 * max(grid.dy, grid.bubble[1]):
            return index
    return None
