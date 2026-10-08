"""
Page outline detection for phone photos (CamScanner style), plan item 40.

Off by default (alignment "page_outline"). When on, the sheet's four page
corners are looked for before any other registration step; when found, the
photo is flattened onto a rectangle and the rest of the pipeline (timing
marks, markers, resize) runs on that. Flatbed scans, where the page fills the
image or no clear outline exists, fall back cleanly: nothing is changed and
the geometry record's page_outline stays null.
"""

import cv2
import numpy as np

from src.utils.image import ImageUtils

# The outline must cover this share of the photo, but not (nearly) all of it
MIN_AREA_FRACTION = 0.25
MAX_AREA_FRACTION = 0.97
WORK_SIDE = 1000.0


def _quad_candidates(small):
    blurred = cv2.GaussianBlur(small, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 140)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8))
    yield edges
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    yield binary


def _corner_angles_ok(quad):
    for i in range(4):
        u = quad[i - 1] - quad[i]
        v = quad[(i + 1) % 4] - quad[i]
        cosine = float(np.dot(u, v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-9))
        if abs(np.degrees(np.arccos(np.clip(cosine, -1, 1))) - 90) > 35:
            return False
    return True


def find_page_outline(image):
    """[[x, y] * 4] (tl, tr, br, bl) of the page in image pixels, or None."""
    if image is None:
        return None
    grey = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = grey.shape[:2]
    scale = min(1.0, WORK_SIDE / max(h, w))
    small = cv2.resize(grey, None, fx=scale, fy=scale) if scale < 1 else grey
    area = float(small.shape[0] * small.shape[1])
    best = None
    for mask in _quad_candidates(small):
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:3]:
            hull = cv2.convexHull(contour)
            fraction = cv2.contourArea(hull) / area
            if fraction < MIN_AREA_FRACTION or fraction > MAX_AREA_FRACTION:
                continue
            quad = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
            if len(quad) != 4 or not cv2.isContourConvex(quad):
                continue
            quad = ImageUtils.order_points(quad.reshape(4, 2).astype(np.float32))
            if not _corner_angles_ok(quad):
                continue
            # A quad hugging the image border is the photo frame, not the page
            margin = 3
            touching = (
                (quad[:, 0] <= margin).sum()
                + (quad[:, 0] >= small.shape[1] - 1 - margin).sum()
                + (quad[:, 1] <= margin).sum()
                + (quad[:, 1] >= small.shape[0] - 1 - margin).sum()
            )
            if touching >= 4:
                continue
            if best is None or fraction > best[0]:
                best = (fraction, quad)
            break
        if best is not None:
            break
    if best is None:
        return None
    return (best[1] / scale).astype(np.float32)


def flatten_page(image, outline):
    """(flattened image, matrix, (w, h)) for an outline from find_page_outline."""
    return ImageUtils.four_point_transform_with_matrix(image, outline)
