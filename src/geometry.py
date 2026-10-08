"""
The result geometry record: how a sheet image was mapped onto the template.

Every geometric correction the engine applies (page outline, the processing
resize, crop / marker / timing-mark registration, thin-plate-spline curve
correction, the final resize to the template's page size) is recorded as one
step while the sheet is read, and the per-block border fits are added at the
end. The record is stored in result.json under "geometry"; previews replay it
instead of detecting anything again, so they always show what was read.

    geometry = {
      "source_size": [w, h],          # the image the engine was given (after
                                      # PDF/EXIF loading; before any rotation)
      "rotation": 0|90|180|270,       # orientation found; already part of the
                                      # homography (informational)
      "page_outline": [[x, y]*4]|None,# phone-photo page outline (source px)
      "page_homography": 3x3,         # source px -> aligned px (without tps)
      "tps": {...}|None,              # thin-plate-spline correction (aligned px)
      "margin_trim": {...}|None,      # template px of scan cut off per side
      "residual": {"page": px, "regions": {...}}|None,
      "aligned_size": [w, h],         # template pageDimensions
      "blocks": {name: {"corners": [[x, y]*4], "status", "method", "level"}},
      "steps": [...],                 # exact replay list (see below)
    }

Steps (applied in order to the source image):
  {"op": "resize", "from": [w, h], "size": [w, h]}
  {"op": "warp", "matrix": 3x3, "size": [w, h], "inverse": bool,
   "affine": bool, "border": int|None}
  {"op": "tps", "points": [[x, y]...], "weights": [[a, b]...],
   "grid_step": int, "size": [w, h]}
  {"op": "filter", "index": i, "name": str}   # intensity-only preprocessor;
                                              # replayed only by callers that
                                              # pass `filters`

Helpers: empty_geometry(w, h), map_points(geometry, pts, direction),
warp_to_aligned(geometry, src_img), GeometryRecorder (engine side).
Python 3.8 compatible; numpy + OpenCV only.
"""

import cv2
import numpy as np

IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
TPS_GRID_STEP = 16


def empty_geometry(w, h):
    """Geometry of an image read as-is (identity mapping)."""
    return {
        "source_size": [int(w), int(h)],
        "rotation": 0,
        "page_outline": None,
        "page_homography": [list(row) for row in IDENTITY],
        "tps": None,
        "margin_trim": None,
        "residual": None,
        "aligned_size": [int(w), int(h)],
        "blocks": {},
        "steps": [],
    }


# --------------------------------------------------------------------------- matrices


def resize_matrix(from_size, to_size):
    """cv2.resize as a matrix on pixel coordinates (pixel-centre convention)."""
    sx = float(to_size[0]) / float(from_size[0])
    sy = float(to_size[1]) / float(from_size[1])
    return np.array(
        [[sx, 0.0, 0.5 * sx - 0.5], [0.0, sy, 0.5 * sy - 0.5], [0.0, 0.0, 1.0]]
    )


def _as3x3(matrix):
    m = np.asarray(matrix, dtype=np.float64)
    if m.shape == (2, 3):
        m = np.vstack([m, [0.0, 0.0, 1.0]])
    return m


def step_matrix(step):
    """Forward (input px -> output px) matrix of a resize or warp step."""
    if step["op"] == "resize":
        return resize_matrix(step["from"], step["size"])
    if step["op"] == "warp":
        m = _as3x3(step["matrix"])
        return np.linalg.inv(m) if step.get("inverse") else m
    return np.eye(3)


def _to_list(matrix):
    return [[float(v) for v in row] for row in np.asarray(matrix, dtype=np.float64)]


# --------------------------------------------------------------------------- tps


def _tps_kernel(r):
    with np.errstate(divide="ignore", invalid="ignore"):
        k = r * r * np.log(r)
    return np.nan_to_num(k)


def tps_displacement(tps, points):
    """Displacement (N x 2) of the spline at points (aligned px)."""
    control = np.asarray(tps["points"], dtype=np.float32)
    weights = np.asarray(tps["weights"], dtype=np.float64)
    points = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    n = len(control)
    distances = np.linalg.norm(points[:, None] - control[None], axis=2)
    k = _tps_kernel(distances)
    p = np.hstack([np.ones((len(points), 1)), points])
    return k @ weights[:n] + p @ weights[n:]


def tps_maps(tps):
    """cv2.remap maps of a recorded spline; the engine and replays share this."""
    page_w, page_h = int(tps["size"][0]), int(tps["size"][1])
    step = int(tps.get("grid_step", TPS_GRID_STEP))
    grid_x = np.arange(0, page_w + step, step, dtype=np.float32)
    grid_y = np.arange(0, page_h + step, step, dtype=np.float32)
    gx, gy = np.meshgrid(grid_x, grid_y)
    grid = np.stack([gx.ravel(), gy.ravel()], axis=1)
    displacement = tps_displacement(tps, grid)
    dx = cv2.resize(
        displacement[:, 0].reshape(gx.shape),
        (page_w, page_h),
        interpolation=cv2.INTER_LINEAR,
    )
    dy = cv2.resize(
        displacement[:, 1].reshape(gy.shape),
        (page_w, page_h),
        interpolation=cv2.INTER_LINEAR,
    )
    map_x, map_y = np.meshgrid(
        np.arange(page_w, dtype=np.float32), np.arange(page_h, dtype=np.float32)
    )
    return map_x + dx.astype(np.float32), map_y + dy.astype(np.float32)


def apply_tps(tps, image):
    map_x, map_y = tps_maps(tps)
    return cv2.remap(image, map_x, map_y, cv2.INTER_LINEAR, borderValue=255)


# --------------------------------------------------------------------------- replay


def _replay_step(step, image, filters=None):
    op = step["op"]
    if op == "resize":
        return cv2.resize(image, (int(step["size"][0]), int(step["size"][1])))
    if op == "warp":
        size = (int(step["size"][0]), int(step["size"][1]))
        flags = cv2.INTER_LINEAR
        if step.get("inverse"):
            flags |= cv2.WARP_INVERSE_MAP
        border = step.get("border")
        kwargs = {"flags": flags}
        if border is not None:
            kwargs["borderValue"] = (
                (border, border, border) if image.ndim == 3 else border
            )
        if step.get("affine"):
            matrix = np.asarray(step["matrix"], dtype=np.float64)[:2]
            return cv2.warpAffine(image, matrix, size, **kwargs)
        matrix = np.asarray(step["matrix"], dtype=np.float64)
        return cv2.warpPerspective(image, matrix, size, **kwargs)
    if op == "tps":
        return apply_tps(step, image)
    if op == "filter":
        if filters is None:
            return image
        out = filters(step, image)
        return image if out is None else out
    return image


def warp_to_aligned(geometry, src_img, filters=None):
    """
    Replay the recorded geometry on the source image (grey or colour) and
    return the aligned page. filters(step, image) may re-apply recorded
    intensity-only preprocessors; without it they are skipped.
    """
    steps = geometry.get("steps")
    if not steps and geometry.get("page_homography") is None:
        steps = _fallback_steps(geometry)
    image = src_img
    if steps:
        for step in steps:
            image = _replay_step(step, image, filters)
    else:
        size = tuple(int(v) for v in geometry["aligned_size"])
        image = cv2.warpPerspective(
            image,
            np.asarray(geometry.get("page_homography") or IDENTITY, np.float64),
            size,
            flags=cv2.INTER_LINEAR,
            borderValue=(255, 255, 255) if image.ndim == 3 else 255,
        )
        if geometry.get("tps"):
            image = apply_tps(geometry["tps"], image)
    aligned_w, aligned_h = (int(v) for v in geometry["aligned_size"])
    if image.shape[1] != aligned_w or image.shape[0] != aligned_h:
        image = cv2.resize(image, (aligned_w, aligned_h))
    return image


def _apply_matrix(matrix, points):
    if len(points) == 0:
        return points
    return cv2.perspectiveTransform(
        points.reshape(-1, 1, 2).astype(np.float64), matrix
    ).reshape(-1, 2)


def _tps_forward(tps, points):
    """Pre-spline point -> aligned point (the spline samples p + d(p))."""
    out = points.copy()
    for _ in range(20):
        out = points - tps_displacement(tps, out)
    return out


def _tps_backward(tps, points):
    return points + tps_displacement(tps, points)


def _fallback_steps(geometry):
    """Steps for a geometry without a replay list: a bare resize when no
    homography was recorded (old results), else the homography (+ tps)."""
    if geometry.get("page_homography") is None and geometry.get("source_size"):
        return [{"op": "resize", "from": list(geometry["source_size"]),
                 "size": list(geometry["aligned_size"])}]
    steps = [{"op": "warp", "matrix": geometry.get("page_homography") or IDENTITY,
              "size": geometry["aligned_size"], "inverse": False}]
    if geometry.get("tps"):
        steps.append(dict(geometry["tps"], op="tps"))
    return steps


def map_points(geometry, pts, direction="source_to_aligned"):
    """Map [[x, y], ...] between source and aligned pixels."""
    points = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    if direction not in ("source_to_aligned", "aligned_to_source"):
        raise ValueError(f"Unknown direction '{direction}'")
    forward = direction == "source_to_aligned"
    steps = geometry.get("steps") or _fallback_steps(geometry)
    sequence = steps if forward else list(reversed(steps))
    for step in sequence:
        if step["op"] in ("resize", "warp"):
            matrix = step_matrix(step)
            points = _apply_matrix(matrix if forward else np.linalg.inv(matrix), points)
        elif step["op"] == "tps":
            points = _tps_forward(step, points) if forward else _tps_backward(step, points)
    return points.tolist()


# --------------------------------------------------------------------------- recorder


class GeometryRecorder:
    """Collects the geometric steps of one sheet while the engine reads it."""

    def __init__(self, width, height):
        self.source_size = (int(width), int(height))
        self.size = self.source_size
        self.steps = []
        self.valid = True
        self.reason = None
        self.info = {}

    def invalidate(self, reason):
        if self.valid:
            self.valid = False
            self.reason = reason

    def resize(self, width, height):
        width, height = int(width), int(height)
        if (width, height) != self.size:
            self.steps.append(
                {"op": "resize", "from": list(self.size), "size": [width, height]}
            )
            self.size = (width, height)

    def warp(self, matrix, size, inverse=False, affine=False, border=None):
        self.steps.append(
            {
                "op": "warp",
                "matrix": _to_list(_as3x3(matrix)),
                "size": [int(size[0]), int(size[1])],
                "inverse": bool(inverse),
                "affine": bool(affine),
                "border": None if border is None else int(border),
            }
        )
        self.size = (int(size[0]), int(size[1]))

    def tps(self, params):
        step = dict(params)
        step["op"] = "tps"
        self.steps.append(step)

    def filter(self, index, name):
        self.steps.append({"op": "filter", "index": int(index), "name": str(name)})

    def add(self, step):
        """A step dict from a preprocessor (see record_geometry)."""
        if step is None:
            self.invalidate("a step did not describe its geometry")
            return
        op = step.get("op")
        if op == "warp":
            self.warp(
                step["matrix"],
                step["size"],
                step.get("inverse", False),
                step.get("affine", False),
                step.get("border"),
            )
        elif op == "resize":
            self.resize(*step["size"])
        elif op == "tps":
            self.tps({k: v for k, v in step.items() if k != "op"})
        else:
            self.invalidate(f"unknown step '{op}'")

    def page_homography(self):
        matrix = np.eye(3)
        for step in self.steps:
            if step["op"] in ("resize", "warp"):
                matrix = step_matrix(step) @ matrix
        return matrix

    def build(self, aligned_size, blocks=None):
        """The geometry dict, or None when some step could not be recorded."""
        if not self.valid:
            return None
        aligned_size = (int(aligned_size[0]), int(aligned_size[1]))
        self.resize(*aligned_size)
        matrix = self.page_homography()
        if abs(matrix[2, 2]) > 1e-12:
            matrix = matrix / matrix[2, 2]
        tps = next((s for s in self.steps if s["op"] == "tps"), None)
        geometry = {
            "source_size": list(self.source_size),
            "rotation": int(self.info.get("rotation", 0)),
            "page_outline": self.info.get("page_outline"),
            "page_homography": _to_list(matrix),
            "tps": None
            if tps is None
            else {k: v for k, v in tps.items() if k != "op"},
            "margin_trim": self.info.get("margin_trim"),
            "residual": self.info.get("residual"),
            "aligned_size": list(aligned_size),
            "blocks": blocks or {},
            "steps": [dict(s) for s in self.steps],
        }
        for key in ("index_points", "alignment_method"):
            if key in self.info:
                geometry[key] = self.info[key]
        return geometry


# --------------------------------------------------------------------------- views


PRINT_IMAGE_MODES = ("auto", "grey", "darkest")


def print_kept_image(image, mode="auto"):
    """
    A grey copy of a colour scan that keeps coloured print (block borders,
    bubble outlines) for border search: plain grey, or the darkest channel per
    pixel ("auto" = darkest, which keeps pink/red/blue print dark).
    """
    if image is None or image.ndim == 2:
        return image
    if mode == "grey":
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # cv2.min is bit-identical to np.min(image, axis=2) and about 16x faster
    channels = cv2.split(image)
    darkest = channels[0]
    for channel in channels[1:]:
        darkest = cv2.min(darkest, channel)
    return darkest
