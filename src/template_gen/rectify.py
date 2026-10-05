"""
Page rectification and sheet-to-sheet registration.

1. Page detection: Otsu separates the bright sheet from a darker background, a
   morphological close removes printed content, and the largest external
   contour is reduced to a quadrilateral (Douglas-Peucker, minAreaRect as a
   fallback). Each side is then re-fitted with a robust (Huber) line fit over
   all contour pixels on that side and the corners are the line intersections,
   which gives sub-pixel corners even when the paper corners are dog-eared.
   When no background is visible (flatbed scans) the whole image is the page.
2. All pages are warped to one canonical size (median measured size, optionally
   capped by max_page_width).
   Uneven lighting is removed by flat-field correction (division by a
   closing-based background estimate).
3. Registration: every rectified page is aligned to a reference page with
   enhanced correlation coefficient maximisation (Evangelidis & Psarakis 2008,
   cv2.findTransformECC, homography model, coarse-to-fine). If ECC does not
   converge or produces an implausible warp, ORB features + RANSAC homography
   are used instead. A 180 degree rotation is tried for sheets fed upside down.
"""

from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from src.template_gen.assignment import odd


def order_corners(points):
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    centre = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - centre[1], points[:, 0] - centre[0])
    ordered = points[np.argsort(angles)]  # starts near -pi: top-left (y down)
    # rotate so the first point is the one with the smallest x + y
    start = int(np.argmin(ordered.sum(axis=1)))
    return np.roll(ordered, -start, axis=0)


def _line_intersection(line_a, line_b):
    (vx1, vy1, x1, y1), (vx2, vy2, x2, y2) = line_a, line_b
    matrix = np.array([[vx1, -vx2], [vy1, -vy2]], dtype=np.float64)
    if abs(np.linalg.det(matrix)) < 1e-9:
        return None
    t, _ = np.linalg.solve(matrix, np.array([x2 - x1, y2 - y1], dtype=np.float64))
    return [x1 + t * vx1, y1 + t * vy1]


def _refine_quad(contour, quad):
    """Fit a line to the contour points along each side; intersect neighbours."""
    points = contour.reshape(-1, 2).astype(np.float32)
    lines = []
    for k in range(4):
        a, b = quad[k], quad[(k + 1) % 4]
        side = b - a
        length = float(np.linalg.norm(side))
        if length < 1:
            return quad
        direction = side / length
        rel = points - a
        along = rel @ direction
        across = np.abs(rel[:, 0] * direction[1] - rel[:, 1] * direction[0])
        keep = (along > 0.08 * length) & (along < 0.92 * length)
        keep &= across < max(3.0, 0.02 * length)
        if keep.sum() < 10:
            return quad
        line = cv2.fitLine(points[keep], cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
        lines.append(line)
    refined = []
    for k in range(4):
        corner = _line_intersection(lines[k - 1], lines[k])
        if corner is None:
            return quad
        refined.append(corner)
    refined = np.array(refined, dtype=np.float32)
    if np.max(np.linalg.norm(refined - quad, axis=1)) > 0.05 * max(
        np.ptp(quad[:, 0]), np.ptp(quad[:, 1])
    ):
        return quad
    return refined


def find_page_quad(gray, work_size=1600):
    """Return the page corners (tl, tr, br, bl) in image pixels, or None."""
    h, w = gray.shape[:2]
    scale = min(1.0, work_size / max(h, w))
    small = (
        cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1
        else gray
    )
    sh, sw = small.shape[:2]
    blurred = cv2.GaussianBlur(small, (5, 5), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (odd(max(sw, sh) / 60), odd(max(sw, sh) / 60))
    )
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(contour)
    if area < 0.08 * sw * sh:
        return None
    # The sheet should be close to a filled rectangle (not a bright floor patch)
    (_, _), (rw, rh), _ = cv2.minAreaRect(contour)
    if area < 0.85 * rw * rh:
        return None
    hull = cv2.convexHull(contour)
    perimeter = cv2.arcLength(hull, True)
    quad = None
    for factor in (0.01, 0.02, 0.03, 0.05):
        approx = cv2.approxPolyDP(hull, factor * perimeter, True)
        if len(approx) == 4:
            quad = approx.reshape(4, 2).astype(np.float32)
            break
    if quad is None:
        quad = cv2.boxPoints(cv2.minAreaRect(hull)).astype(np.float32)
    quad = order_corners(quad)
    quad = order_corners(_refine_quad(contour, quad))
    # A "page" touching all four image borders is the image itself (flatbed scan)
    margin = 0.01 * max(sw, sh)
    touches = [
        quad[:, 0].min() <= margin,
        quad[:, 1].min() <= margin,
        quad[:, 0].max() >= sw - 1 - margin,
        quad[:, 1].max() >= sh - 1 - margin,
    ]
    if all(touches):
        return None
    if not cv2.isContourConvex(quad.reshape(-1, 1, 2).astype(np.int32)):
        return None
    return quad / scale


def quad_size(quad):
    tl, tr, br, bl = quad
    width = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    height = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    return float(width), float(height)


def image_quad(gray):
    h, w = gray.shape[:2]
    return np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])


def warp_quad(gray, quad, size):
    width, height = size
    target = np.float32(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]
    )
    matrix = cv2.getPerspectiveTransform(np.float32(quad), target)
    warped = cv2.warpPerspective(
        gray,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped, matrix


def _scale_homography(matrix, scale):
    s = np.diag([scale, scale, 1.0])
    return s @ matrix @ np.linalg.inv(s)


def _corner_shift(matrix, size):
    w, h = size
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2)
    moved = cv2.perspectiveTransform(corners, matrix.astype(np.float64))
    return float(np.max(np.linalg.norm(moved - corners, axis=2)))


def ecc_register(
    reference, moving, initial=None, scales=(0.25, 0.5), iterations=(60, 12)
):
    """
    Estimate H mapping reference coordinates to moving coordinates
    (moving(H x) ~ reference(x)), coarse to fine. Returns (H, correlation).
    """
    matrix = np.eye(3, dtype=np.float32) if initial is None else initial
    matrix = matrix.astype(np.float32)
    correlation = None
    for level, scale in enumerate(scales):
        ref_small = cv2.resize(
            reference, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
        )
        mov_small = cv2.resize(
            moving, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
        )
        warp = _scale_homography(matrix.astype(np.float64), scale).astype(np.float32)
        criteria = (
            cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
            iterations[min(level, len(iterations) - 1)],
            1e-4,
        )
        try:
            correlation, warp = cv2.findTransformECC(
                ref_small.astype(np.float32),
                mov_small.astype(np.float32),
                warp,
                cv2.MOTION_HOMOGRAPHY,
                criteria,
                None,
                5,
            )
        except cv2.error:
            return None, 0.0
        matrix = _scale_homography(warp.astype(np.float64), 1 / scale).astype(
            np.float32
        )
    return matrix, float(correlation)


def orb_register(reference, moving, max_features=4000):
    """ORB + ratio test + RANSAC. Returns H (reference -> moving) or None."""
    orb = cv2.ORB_create(max_features)
    kp_ref, des_ref = orb.detectAndCompute(reference, None)
    kp_mov, des_mov = orb.detectAndCompute(moving, None)
    if des_ref is None or des_mov is None:
        return None, 0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = matcher.knnMatch(des_ref, des_mov, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.8 * p[1].distance]
    if len(good) < 12:
        return None, len(good)
    src = np.float32([kp_ref[m.queryIdx].pt for m in good])
    dst = np.float32([kp_mov[m.trainIdx].pt for m in good])
    matrix, inliers = cv2.findHomography(src, dst, cv2.RANSAC, 4.0)
    if matrix is None:
        return None, 0
    return matrix.astype(np.float32), int(inliers.sum())


def _coarse(image, scale=0.125):
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small.astype(np.float32), (5, 5), 0)
    return (small - small.mean()) / (small.std() + 1e-6)


def pose_scores(reference, moving):
    """Normalised correlation of the upright and the 180 degree pose."""
    ref, mov = _coarse(reference), _coarse(moving)
    if ref.shape != mov.shape:
        mov = cv2.resize(mov, (ref.shape[1], ref.shape[0]))
    return float((ref * mov).mean()), float((ref * mov[::-1, ::-1]).mean())


def register_to_reference(
    reference, moving, max_shift_ratio=0.08, min_correlation=0.3, refine=True
):
    """
    Align a rectified page to the reference page. Returns (aligned, info).
    info: method, correlation, ok, rotated_180. refine=False skips the fine
    ECC level (enough when both pages were rectified from detected edges).
    """
    h, w = reference.shape[:2]
    max_shift = max_shift_ratio * max(w, h)
    coarse, fine = min(1.0, 300.0 / w), min(1.0, 600.0 / w)
    # Orientation: correlation at identity decides clear cases; coarse ECC on
    # both poses settles ambiguous ones
    upright, flipped = pose_scores(reference, moving)
    if abs(upright - flipped) > 0.1:
        pose_options = (flipped > upright,)
    else:
        pose_options = (False, True)
    poses = []
    for rotated in pose_options:
        candidate = cv2.rotate(moving, cv2.ROTATE_180) if rotated else moving
        matrix, correlation = ecc_register(reference, candidate, scales=(coarse,))
        if matrix is not None and _corner_shift(matrix, (w, h)) > max_shift:
            matrix, correlation = None, 0.0
        poses.append((correlation if matrix is not None else -1.0, rotated, matrix))
    _, rotated, initial = max(poses, key=lambda p: p[0])
    candidate = cv2.rotate(moving, cv2.ROTATE_180) if rotated else moving
    matrix, correlation = (None, 0.0)
    if initial is not None and not refine:
        matrix, correlation = initial, max(poses)[0]
    elif initial is not None:
        matrix, correlation = ecc_register(
            reference, candidate, initial, scales=(fine,), iterations=(15,)
        )
        if matrix is None:
            matrix, correlation = initial, max(poses)[0]
    method = "ecc"
    if matrix is None or correlation < 0.5 or _corner_shift(matrix, (w, h)) > max_shift:
        orb_matrix, _ = orb_register(reference, candidate)
        if (
            orb_matrix is not None
            and _corner_shift(orb_matrix, (w, h)) <= 3 * max_shift
        ):
            refined, refined_corr = ecc_register(
                reference, candidate, orb_matrix, scales=(coarse, fine)
            )
            if refined is not None and refined_corr >= (correlation or 0):
                matrix, correlation, method = refined, refined_corr, "orb+ecc"
            elif matrix is None:
                matrix, correlation, method = orb_matrix, 0.0, "orb"
    if matrix is None:
        return candidate.copy(), {
            "method": "none",
            "correlation": 0.0,
            "ok": False,
            "rotated_180": bool(rotated),
        }
    aligned = cv2.warpPerspective(
        candidate,
        matrix.astype(np.float64),
        (w, h),
        flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )
    shift = _corner_shift(matrix, (w, h)) / max(w, h)
    # A high correlation, or a small correction of an already rectified page,
    # is trusted; a weak correlation reached by a large warp is not (repetitive
    # bubble grids give ECC false optima a few rows away)
    ok = bool(correlation >= 0.5 or (correlation >= min_correlation and shift <= 0.03))
    return aligned, {
        "method": method,
        "correlation": round(float(correlation), 4),
        "shift_ratio": round(float(shift), 4),
        "ok": ok,
        "rotated_180": bool(rotated),
    }


def flatten_illumination(gray, scale=0.25):
    """
    Divide by a smooth paper-background estimate (closing removes ink, blur
    smooths) so shadows and uneven lighting vanish: flat-field correction.
    """
    h, w = gray.shape[:2]
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (odd(small.shape[1] / 40), odd(small.shape[1] / 40))
    )
    background = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
    background = cv2.GaussianBlur(background, (0, 0), small.shape[1] / 80)
    background = cv2.resize(background, (w, h), interpolation=cv2.INTER_LINEAR)
    flat = gray.astype(np.float32) * (
        245.0 / np.maximum(background, 1).astype(np.float32)
    )
    return np.clip(flat, 0, 255).astype(np.uint8)


def rectify_pages(
    images, page_size=None, max_page_width=1700, workers=4, min_page_width=1000
):
    """
    Find and warp every page to a common canonical size.
    Returns (rectified images, page size [w, h], per-image info list).
    """
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        found_quads = list(pool.map(find_page_quad, images))
    quads, infos = [], []
    for gray, quad in zip(images, found_quads):
        found = quad is not None
        if not found:
            quad = image_quad(gray)
        quads.append(quad)
        infos.append({"page_found": found, "corners": np.round(quad, 1).tolist()})
    sizes = np.array([quad_size(q) for q in quads])
    # Keep orientation consistent with the majority of pages
    portrait = np.median(sizes[:, 1] >= sizes[:, 0]) >= 0.5
    for index, (width, height) in enumerate(sizes):
        if (height >= width) != portrait:
            quads[index] = np.roll(quads[index], 1, axis=0)
            sizes[index] = [height, width]
            infos[index]["rotated_90"] = True
    if page_size is None:
        width, height = np.median(sizes, axis=0)
        if max_page_width and width > max_page_width:
            height *= max_page_width / width
            width = max_page_width
        elif min_page_width and width < min_page_width:
            # Small captures are upsampled so bubbles span enough pixels
            height *= min_page_width / width
            width = min_page_width
        page_size = [int(round(width)), int(round(height))]
    page_size = [int(page_size[0]), int(page_size[1])]

    def rectify(pair):
        return flatten_illumination(warp_quad(pair[0], pair[1], page_size)[0])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rectified = list(pool.map(rectify, zip(images, quads)))
    return rectified, page_size, infos
