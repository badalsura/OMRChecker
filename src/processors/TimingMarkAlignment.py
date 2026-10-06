"""
Sheet registration from printed timing marks.

Timing tracks (rows of solid rectangles along the sheet edges) are the standard
registration feature on machine-read forms. This preprocessor:

1. finds the page outline (or uses the whole image) for a coarse page-to-image
   mapping, once per candidate orientation (0/90/180/270 degrees);
2. detects dark, solid, mark-sized blobs with connected components;
3. matches them to the expected mark centres from the template, fits a
   homography with RANSAC, then re-matches with a tight radius and refits,
   starting from a few shifted and rotated guesses and rejecting fits that
   slid one mark along a track (a mark found just past the track's end);
4. keeps the orientation with the most matched marks and lowest residual,
   rejecting the sheet when too few marks match or the residual is too large;
5. optionally adds a thin-plate-spline correction fitted to the remaining
   per-mark residuals, which absorbs paper curl and lens distortion;
6. warps the original-resolution image straight into template coordinates.
"""

import cv2
import numpy as np

from src.logger import logger
from src.processors.interfaces.ImagePreprocessor import ImagePreprocessor
from src.utils.image import ImageUtils

DEFAULT_SIZE_TOLERANCE = 0.5
DEFAULT_MIN_MATCHED_MARKS = 8
DEFAULT_MAX_RESIDUAL = 3.0
# Fraction of the image a page outline must cover to be trusted
MIN_PAGE_AREA_FRACTION = 0.3
# A fit matching this share of marks with none past the track ends is final
GOOD_FIT_FRACTION = 0.95
# Smaller measured tilts are left to the shifted guesses and the fit itself
MIN_TILT_DEGREES = 0.3
# Thin-plate-spline displacement field is evaluated on this grid step (px)
TPS_GRID_STEP = 16


class TimingMarkAlignment(ImagePreprocessor):
    needs_full_resolution = True
    geometry = "recorded"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        options = self.options
        self.tracks = {
            name: np.array(track["marks"], dtype=np.float32)
            for name, track in options["tracks"].items()
        }
        self.expected = np.concatenate(list(self.tracks.values()))
        self.mark_w, self.mark_h = options["markDimensions"]
        self.size_tolerance = options.get("sizeTolerance", DEFAULT_SIZE_TOLERANCE)
        self.search_radius = options.get(
            "searchRadius", 0.45 * self._min_mark_spacing()
        )
        self.min_matched = options.get(
            "minMatchedMarks", min(DEFAULT_MIN_MATCHED_MARKS, len(self.expected))
        )
        self.max_residual = options.get("maxResidual", DEFAULT_MAX_RESIDUAL)
        self.non_rigid = options.get("nonRigid", False)
        self.detect_orientation = options.get("detectOrientation", True)
        self.last_registration = {}

    def __str__(self):
        return f"TimingMarkAlignment({len(self.expected)} marks)"

    def _min_mark_spacing(self):
        spacings = []
        for marks in self.tracks.values():
            if len(marks) > 1:
                d = np.linalg.norm(np.diff(marks, axis=0), axis=1)
                spacings.append(d.min())
        return float(min(spacings)) if spacings else 50.0

    def apply_filter(self, image, file_path):
        page_w, page_h = self.page_dimensions
        page_corners = self.find_page_corners(image)
        candidates = self.blob_centres(image, page_corners)
        if len(candidates) < self.min_matched:
            logger.error(
                f"Timing marks not found in '{file_path}': {len(candidates)} candidate blobs"
            )
            return None

        rotations = (0, 1, 2, 3) if self.detect_orientation else (0,)
        best = None
        for rotation in rotations:
            fit = self.fit_orientation(page_corners, candidates, rotation)
            if fit is None:
                continue
            if best is None or (fit["matched"], -fit["residual"]) > (
                best["matched"],
                -best["residual"],
            ):
                best = fit

        if best is None or best["matched"] < self.min_matched:
            matched = 0 if best is None else best["matched"]
            logger.error(
                f"Timing mark registration failed for '{file_path}': matched {matched}/{len(self.expected)} marks"
            )
            return None
        if best["residual"] > self.max_residual:
            logger.error(
                f"Timing mark registration rejected for '{file_path}': residual {best['residual']:.2f}px > {self.max_residual}px"
            )
            return None

        homography = best["homography"]

        def warp_page(im):
            return cv2.warpPerspective(
                im,
                homography,
                (int(page_w), int(page_h)),
                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                borderValue=255,
            )

        warped = warp_page(image)
        self.record_geometry(warp_page)
        if self.non_rigid and best["matched"] >= 6:
            warped = self.thin_plate_correction(warped, best)

        self.last_registration = {
            "method": "timing_marks",
            "orientation": best["rotation"] * 90,
            "matched_marks": best["matched"],
            "expected_marks": int(len(self.expected)),
            "residual_px": round(best["residual"], 3),
        }
        logger.info(f"Timing marks: {self.last_registration}")
        return warped

    # --- coarse page mapping ---------------------------------------------

    def find_page_corners(self, image):
        """Return the page quadrilateral (tl, tr, br, bl) in image pixels."""
        h, w = image.shape[:2]
        scale = 800.0 / max(h, w)
        small = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image
        scale = min(scale, 1.0)
        blurred = cv2.GaussianBlur(small, (5, 5), 0)
        _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        binary = cv2.morphologyEx(
            binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        )
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        full = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
        if not contours:
            return full
        largest = max(contours, key=cv2.contourArea)
        if (
            cv2.contourArea(largest)
            < MIN_PAGE_AREA_FRACTION * small.shape[0] * small.shape[1]
        ):
            return full
        quad = cv2.approxPolyDP(largest, 0.02 * cv2.arcLength(largest, True), True)
        if len(quad) != 4:
            rect = cv2.minAreaRect(largest)
            quad = cv2.boxPoints(rect)
        quad = quad.reshape(4, 2).astype(np.float32) / scale
        # A page touching the image border on all sides means no real background
        return ImageUtils.order_points(quad)

    def coarse_homography(self, page_corners, rotation):
        """Template -> image mapping assuming the page is rotated by rotation*90 degrees."""
        page_w, page_h = self.page_dimensions
        template_corners = np.float32(
            [[0, 0], [page_w, 0], [page_w, page_h], [0, page_h]]
        )
        rotated = np.roll(page_corners, -rotation, axis=0)
        return cv2.getPerspectiveTransform(template_corners, rotated)

    # --- mark detection --------------------------------------------------

    def blob_centres(self, image, page_corners):
        """Centres and sizes of solid dark blobs roughly the size of a timing mark."""
        page_w, page_h = self.page_dimensions
        side_a = np.linalg.norm(page_corners[1] - page_corners[0])
        side_b = np.linalg.norm(page_corners[3] - page_corners[0])
        # Pixels per template unit, orientation agnostic
        scale = np.sqrt((side_a * side_b) / float(page_w * page_h))
        expected_area = self.mark_w * self.mark_h * scale * scale
        low = expected_area * (1 - self.size_tolerance) ** 2
        high = expected_area * (1 + self.size_tolerance) ** 2

        # Detect on a downscaled copy where a mark's short side is ~8 px: blob
        # centroids stay sub-pixel accurate and the homography averages many marks
        shrink = min(1.0, 8.0 / max(min(self.mark_w, self.mark_h) * scale, 1e-6))
        if shrink < 0.9:
            image = cv2.resize(
                image, None, fx=shrink, fy=shrink, interpolation=cv2.INTER_AREA
            )
        else:
            shrink = 1.0
        scale *= shrink
        expected_area *= shrink * shrink
        low, high = low * shrink * shrink, high * shrink * shrink

        block = int(max(15, (max(self.mark_w, self.mark_h) * scale * 4) // 2 * 2 + 1))
        binary = cv2.adaptiveThreshold(
            image, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 15
        )
        count, _, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
        areas = stats[1:, cv2.CC_STAT_AREA]
        widths = stats[1:, cv2.CC_STAT_WIDTH].astype(np.float32)
        heights = stats[1:, cv2.CC_STAT_HEIGHT].astype(np.float32)
        solidity = areas / np.maximum(widths * heights, 1)
        long_side = max(self.mark_w, self.mark_h) * scale
        keep = (
            (areas >= low)
            & (areas <= high)
            & (solidity > 0.6)
            & (
                np.maximum(widths, heights)
                <= long_side * (1 + self.size_tolerance) * 1.5
            )
        )
        return (centroids[1:][keep] / shrink).astype(np.float32)

    # --- fitting ---------------------------------------------------------

    def fit_orientation(self, page_corners, candidates, rotation):
        """Best fit for one orientation over a few shifted starting guesses.

        Tracks are periodic, so a coarse guess that is off by about one mark
        pitch (a page scanned flush with the glass and slightly rotated) can
        lock each track onto its neighbouring mark. Starting from shifted and
        rotated guesses and penalising marks found just past a track's ends
        rejects those off-by-one fits.
        """
        coarse = self.coarse_homography(page_corners, rotation)
        radius = self.search_radius * self._pixels_per_unit(coarse)
        best = None
        seen = []
        for start in self._starting_guesses(coarse, candidates):
            fit = self._refine(start, candidates, radius)
            if fit is None:
                continue
            if any(np.allclose(fit["homography"], h, atol=1e-3) for h in seen):
                continue
            seen.append(fit["homography"])
            fit["rotation"] = rotation
            fit["beyond_ends"] = self._marks_beyond_track_ends(
                fit["homography"], candidates, radius * 0.5
            )
            if best is None or self._fit_key(fit) > self._fit_key(best):
                best = fit
            # Nearly every mark matched and none past the ends: not slid, stop
            if fit["beyond_ends"] == 0 and fit["matched"] >= GOOD_FIT_FRACTION * len(
                self.expected
            ):
                break
        return best

    @staticmethod
    def _fit_key(fit):
        return (fit["matched"] - 2 * fit["beyond_ends"], -fit["residual"])

    def _starting_guesses(self, coarse, candidates):
        """The coarse mapping, turned by the tracks' measured tilt, then shifted.

        A page scanned flush with the glass has no outline to find, so the
        coarse mapping misses its tilt; the tilt comes from the direction of
        neighbouring candidate blobs instead. Shifts of half and one mark
        pitch cover the remaining offset.
        """
        page_w, page_h = self.page_dimensions
        pitch = self._min_mark_spacing()
        tilt = self._estimate_tilt(coarse, candidates, pitch)
        angles = (tilt, 0.0) if abs(tilt) > MIN_TILT_DEGREES else (0.0,)
        guesses = []
        for angle in angles:
            rotate = np.vstack(
                [
                    cv2.getRotationMatrix2D((page_w / 2, page_h / 2), angle, 1.0),
                    [0, 0, 1],
                ]
            )
            for dy in (0.0, -0.5, 0.5, -1.0, 1.0):
                for dx in (0.0, -0.5, 0.5):
                    shift = np.float64(
                        [[1, 0, dx * pitch], [0, 1, dy * pitch], [0, 0, 1]]
                    )
                    guesses.append(coarse @ shift @ rotate)
        return guesses

    def _estimate_tilt(self, coarse, candidates, pitch):
        """Degrees the marks are turned from the template, from neighbour directions."""
        if len(candidates) < 6:
            return 0.0
        points = cv2.perspectiveTransform(
            candidates[None].astype(np.float32), np.linalg.inv(coarse)
        )[0]
        distances = _squared_distances(points, points)
        np.fill_diagonal(distances, np.inf)
        nearest = distances.argmin(axis=1)
        steps = points[nearest] - points
        lengths = np.linalg.norm(steps, axis=1)
        steps = steps[np.abs(lengths - pitch) < 0.25 * pitch]
        if len(steps) < 6:
            return 0.0
        measured = _angle_mod_90(steps)
        template_steps = np.diff(self.expected, axis=0)
        template_steps = template_steps[
            np.abs(np.linalg.norm(template_steps, axis=1) - pitch) < 0.25 * pitch
        ]
        expected = _angle_mod_90(template_steps) if len(template_steps) else 0.0
        # Template -> image rotation; getRotationMatrix2D turns the other way
        return float(-((measured - expected + 45) % 90 - 45))

    def _refine(self, homography, candidates, radius):
        # Coarse page detection can be off; start wide and tighten after each fit
        for attempt_radius in (radius * 2.0, radius, radius * 0.5):
            pairs = self.match(homography, candidates, attempt_radius)
            if len(pairs) < max(4, self.min_matched // 2):
                return None
            template_pts = self.expected[pairs[:, 0]]
            image_pts = candidates[pairs[:, 1]]
            if not self._spans_two_dimensions(template_pts):
                return None
            fitted, inliers = cv2.findHomography(
                template_pts, image_pts, cv2.RANSAC, max(2.0, 0.5 * radius)
            )
            if fitted is None:
                return None
            homography = fitted
        pairs = self.match(homography, candidates, radius * 0.5)
        if len(pairs) < 4:
            return None
        template_pts = self.expected[pairs[:, 0]]
        image_pts = candidates[pairs[:, 1]]
        homography, inliers = cv2.findHomography(template_pts, image_pts, 0)
        if homography is None:
            return None
        residual = self.residual_in_template_units(homography, template_pts, image_pts)
        return {
            "homography": homography,
            "matched": int(len(pairs)),
            "residual": float(residual),
            "template_pts": template_pts,
            "image_pts": image_pts,
        }

    def _marks_beyond_track_ends(self, homography, candidates, radius):
        """Blobs one pitch past either end of a track: the fit slid along it."""
        beyond = []
        for marks in self.tracks.values():
            if len(marks) < 2:
                continue
            beyond.append(2 * marks[0] - marks[1])
            beyond.append(2 * marks[-1] - marks[-2])
        if not beyond or len(candidates) == 0:
            return 0
        projected = cv2.perspectiveTransform(np.float32(beyond)[None], homography)[0]
        distances = _squared_distances(projected, candidates)
        return int((distances.min(axis=1) <= radius * radius).sum())

    def _pixels_per_unit(self, homography):
        page_w, page_h = self.page_dimensions
        centre = np.float32([[[page_w / 2, page_h / 2], [page_w / 2 + 1, page_h / 2]]])
        mapped = cv2.perspectiveTransform(centre, homography)[0]
        return float(np.linalg.norm(mapped[1] - mapped[0]))

    def match(self, homography, candidates, radius):
        """Mutual nearest-neighbour matches (expected index, candidate index) within radius."""
        projected = cv2.perspectiveTransform(self.expected[None], homography)[0]
        distances = _squared_distances(projected, candidates)
        nearest_candidate = distances.argmin(axis=1)
        nearest_expected = distances.argmin(axis=0)
        expected_index = np.arange(len(projected))
        keep = (nearest_expected[nearest_candidate] == expected_index) & (
            distances[expected_index, nearest_candidate] <= radius * radius
        )
        pairs = np.stack([expected_index[keep], nearest_candidate[keep]], axis=1)
        return np.array(pairs, dtype=np.int64).reshape(-1, 2)

    @staticmethod
    def _spans_two_dimensions(points):
        if len(points) < 3:
            return False
        centred = points - points.mean(axis=0)
        eigenvalues = np.linalg.eigvalsh(np.cov(centred.T))
        # Marks from a single straight track are collinear and can't fix a homography
        return eigenvalues[0] > 1e-3 * eigenvalues[1] and eigenvalues[0] > 25

    def residual_in_template_units(self, homography, template_pts, image_pts):
        inverse = np.linalg.inv(homography)
        back = cv2.perspectiveTransform(image_pts[None], inverse)[0]
        return np.mean(np.linalg.norm(back - template_pts, axis=1))

    # --- non-rigid refinement ---------------------------------------------

    def thin_plate_correction(self, warped, fit):
        """Remove the residual per-mark displacement with a thin-plate spline."""
        inverse = np.linalg.inv(fit["homography"])
        observed = cv2.perspectiveTransform(fit["image_pts"][None], inverse)[0]
        targets = fit["template_pts"]
        page_w, page_h = int(self.page_dimensions[0]), int(self.page_dimensions[1])
        # For each output (template) pixel, sample the warped image where it really is
        weights = fit_thin_plate_spline(targets, observed - targets)
        grid_x = np.arange(0, page_w + TPS_GRID_STEP, TPS_GRID_STEP, dtype=np.float32)
        grid_y = np.arange(0, page_h + TPS_GRID_STEP, TPS_GRID_STEP, dtype=np.float32)
        gx, gy = np.meshgrid(grid_x, grid_y)
        grid = np.stack([gx.ravel(), gy.ravel()], axis=1)
        displacement = evaluate_thin_plate_spline(weights, targets, grid)
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
        map_x = map_x + dx.astype(np.float32)
        map_y = map_y + dy.astype(np.float32)

        def remap(im):
            return cv2.remap(im, map_x, map_y, cv2.INTER_LINEAR, borderValue=255)

        self.record_geometry(remap)
        return remap(warped)


def _squared_distances(a, b):
    """All squared distances between two point sets, without an n*m*2 temporary."""
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    return np.maximum(
        (a * a).sum(axis=1)[:, None] + (b * b).sum(axis=1)[None, :] - 2 * a @ b.T, 0
    )


def _angle_mod_90(steps):
    """Median direction of step vectors, folded into [-45, 45) degrees."""
    angles = np.degrees(np.arctan2(steps[:, 1], steps[:, 0]))
    return float(np.median((angles + 45) % 90 - 45))


def _tps_kernel(r):
    with np.errstate(divide="ignore", invalid="ignore"):
        k = r * r * np.log(r)
    return np.nan_to_num(k)


def fit_thin_plate_spline(control_points, values, regularization=1.0):
    """Solve TPS weights mapping control_points -> values (N x 2)."""
    n = len(control_points)
    distances = np.linalg.norm(control_points[:, None] - control_points[None], axis=2)
    k = _tps_kernel(distances) + regularization * np.eye(n)
    p = np.hstack([np.ones((n, 1)), control_points])
    system = np.zeros((n + 3, n + 3))
    system[:n, :n], system[:n, n:], system[n:, :n] = k, p, p.T
    rhs = np.zeros((n + 3, 2))
    rhs[:n] = values
    return np.linalg.lstsq(system, rhs, rcond=None)[0]


def evaluate_thin_plate_spline(weights, control_points, points):
    n = len(control_points)
    distances = np.linalg.norm(points[:, None] - control_points[None], axis=2)
    k = _tps_kernel(distances)
    p = np.hstack([np.ones((len(points), 1)), points])
    return k @ weights[:n] + p @ weights[n:]
