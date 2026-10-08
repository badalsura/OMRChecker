"""
Image quality gate: three cheap measures taken before a sheet is read, so a
truly unreadable photo goes to review (flag poor_image) instead of reaching
the threshold logic. Thresholds are conservative (review_params).

- sharpness: variance of the Laplacian on the page scaled to 1000 px wide;
- contrast: the 1st to 99th percentile grey range;
- bubble_px: the bubble's size in the scan's own pixels (template bubble size
  times the scale of the page transform): too few pixels can't be read.
"""

import cv2
import numpy as np

WIDTH = 1000


def measure(image, geometry=None, bubble_size=None):
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if gray.shape[1] > WIDTH:
        scale = WIDTH / gray.shape[1]
        gray = cv2.resize(
            gray, (WIDTH, max(1, int(round(gray.shape[0] * scale)))), interpolation=cv2.INTER_AREA
        )
    h, w = gray.shape[:2]
    inner = gray[h // 10 : h - h // 10, w // 10 : w - w // 10]
    if inner.size == 0:
        inner = gray
    _, std = cv2.meanStdDev(cv2.Laplacian(inner, cv2.CV_32F))
    sharpness = float(std[0, 0]) ** 2
    # 1st and 99th percentile grey levels from the histogram (no sort)
    cumulative = np.cumsum(cv2.calcHist([inner], [0], None, [256], [0, 256]).ravel())
    total = cumulative[-1]
    low = int(np.searchsorted(cumulative, 0.01 * total))
    high = int(np.searchsorted(cumulative, 0.99 * total))
    out = {"sharpness": round(sharpness, 1), "contrast": round(float(high - low), 1)}
    matrix = (geometry or {}).get("page_homography")
    if matrix is not None:
        m = np.asarray(matrix, np.float64)
        det = abs(np.linalg.det(m[:2, :2] / m[2, 2])) if m[2, 2] else 0.0
        if det > 1e-12 and bubble_size:
            # page_homography maps source -> aligned (template) pixels
            out["bubble_px"] = round(float(bubble_size / np.sqrt(det)), 1)
    return out


def review(quality, params):
    """A poor_image sheet item when a measure is under its minimum."""
    limits = {
        "sharpness": params.get("min_sharpness", 0),
        "contrast": params.get("min_contrast", 0),
        "bubble_px": params.get("min_bubble_px", 0),
    }
    low = [
        name
        for name, minimum in limits.items()
        if minimum and name in quality and quality[name] < minimum
    ]
    if not low:
        return []
    return [
        {
            "kind": "sheet",
            "name": "poor_image",
            "flags": ["poor_image"],
            "measures": {name: quality[name] for name in low},
            "minimums": {name: limits[name] for name in low},
        }
    ]
