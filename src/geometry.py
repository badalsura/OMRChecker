"""
Result geometry record helpers (see the "geometry" key of a result).

MINIMAL STUB written by the results-views workstream so the Results screen can
be built against the agreed API; the alignment workstream owns this module and
its version replaces this one. Missing geometry is treated as identity, or as a
plain resize when the source and aligned sizes differ. The non-rigid ("tps")
part is ignored here.

    geometry = {
        "source_size": [w, h],        # source pixels (after the page rotation)
        "rotation": 0|90|180|270,
        "page_homography": 3x3,       # source pixels -> aligned pixels
        "aligned_size": [w, h],
        "blocks": {...},
        ...
    }
"""

import cv2
import numpy as np


def empty_geometry(w, h):
    """Identity geometry for an image that is already aligned (w x h)."""
    return {
        "source_size": [int(w), int(h)],
        "rotation": 0,
        "page_outline": None,
        "page_homography": np.eye(3).tolist(),
        "tps": None,
        "margin_trim": None,
        "residual": None,
        "aligned_size": [int(w), int(h)],
        "blocks": {},
    }


def _homography(geometry):
    geometry = geometry or {}
    matrix = geometry.get("page_homography")
    if matrix is not None:
        matrix = np.asarray(matrix, dtype=np.float64)
        if matrix.shape == (3, 3) and abs(np.linalg.det(matrix)) > 1e-12:
            return matrix
    source = geometry.get("source_size")
    aligned = geometry.get("aligned_size")
    if source and aligned and source[0] and source[1]:
        return np.diag(
            [float(aligned[0]) / source[0], float(aligned[1]) / source[1], 1.0]
        )
    return np.eye(3)


def map_points(geometry, pts, direction="source_to_aligned"):
    """Map [[x, y], ...] between source and aligned pixels; returns a list."""
    points = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    if not len(points):
        return []
    matrix = _homography(geometry)
    if direction == "aligned_to_source":
        matrix = np.linalg.inv(matrix)
    elif direction != "source_to_aligned":
        raise ValueError(f"Unknown direction '{direction}'")
    mapped = cv2.perspectiveTransform(points.reshape(-1, 1, 2), matrix)
    return mapped.reshape(-1, 2).tolist()


def warp_to_aligned(geometry, src_img):
    """The source image (in source pixels) warped onto the aligned page."""
    geometry = geometry or {}
    aligned = geometry.get("aligned_size")
    if not aligned:
        return src_img.copy()
    size = (int(aligned[0]), int(aligned[1]))
    source = geometry.get("source_size")
    image = src_img
    if source and (image.shape[1], image.shape[0]) != (int(source[0]), int(source[1])):
        image = cv2.resize(image, (int(source[0]), int(source[1])))
    return cv2.warpPerspective(
        image,
        _homography(geometry),
        size,
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
