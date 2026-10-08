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

from src.geometry import tps_maps
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
TRACK_GRID_STEP = 48
# One found index point outweighs this many timing marks when choosing the
# orientation (index points are placed asymmetrically on purpose)
INDEX_POINT_WEIGHT = 4
# Index points are looked for this far (template px) from where the fit puts them
INDEX_SEARCH_UNITS = 10.0
REGION_NAMES = {
    (0, 0): "top-left",
    (0, 1): "top",
    (0, 2): "top-right",
    (1, 0): "left",
    (1, 1): "middle",
    (1, 2): "right",
    (2, 0): "bottom-left",
    (2, 1): "bottom",
    (2, 2): "bottom-right",
}


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
        self.expected = (
            np.concatenate(list(self.tracks.values()))
            if self.tracks
            else np.zeros((0, 2), np.float32)
        )
        # Index points: corner squares, dots, L-corners of any size and shape
        self.index_points = [
            {
                "name": point.get("name") or f"point{i + 1}",
                "center": np.float32(point["center"]),
                "size": np.float32(point["size"]),
                "shape": point.get("shape", "any"),
                "required": point.get("required", True),
            }
            for i, point in enumerate(options.get("indexPoints") or [])
        ]
        self.joint_fit = options.get("jointFit", True)
        self.trim_margins = options.get("trimMargins", True)
        self.max_region_residual = options.get("maxRegionResidual")
        self.mark_w, self.mark_h = options["markDimensions"]
        self.size_tolerance = options.get("sizeTolerance", DEFAULT_SIZE_TOLERANCE)
        self.search_radius = options.get(
            "searchRadius", 0.45 * self._min_mark_spacing()
        )
        self.min_matched = options.get(
            "minMatchedMarks", min(DEFAULT_MIN_MATCHED_MARKS, len(self.expected))
        )
        if not len(self.expected) and len(self.index_points) < 4:
            raise ValueError(
                "TimingMarkAlignment needs timing tracks or at least 4 indexPoints"
            )
        self.max_residual = options.get("maxResidual", DEFAULT_MAX_RESIDUAL)
        self.non_rigid = options.get("nonRigid", False)
        self.detect_orientation = options.get("detectOrientation", True)
        # Stop trying orientations once one fits cleanly (the 180 degree
        # look-alike is always tried first, unless index points decided)
        self.early_stop = options.get("earlyStop", False)
        # Start the track search from the index points' own fit as well
        self.index_seed = options.get("indexSeed", False)
        self.last_registration = {}

    def __str__(self):
        return f"TimingMarkAlignment({len(self.expected)} marks, {len(self.index_points)} index points)"

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
        rotations = (0, 1, 2, 3) if self.detect_orientation else (0,)
        if self.early_stop and self.detect_orientation:
            rotations = (0, 2, 1, 3)
        best = None
        if len(self.expected):
            candidates = self.blob_centres(image, page_corners)
            self._candidates = candidates
            if len(candidates) < self.min_matched:
                logger.error(
                    f"Timing marks not found in '{file_path}': {len(candidates)} candidate blobs"
                )
                return None
            orientation_fits = []
            for rotation in rotations:
                fit = self.fit_orientation(page_corners, candidates, rotation, image)
                if fit is None:
                    continue
                if self.index_points:
                    self.locate_index_points(image, fit)
                orientation_fits.append(fit)
                if best is None or self._orientation_key(fit) > self._orientation_key(
                    best
                ):
                    best = fit
                if self._clean_stop(best, len(orientation_fits)):
                    break
            self._runner_up = max(
                (f for f in orientation_fits if f is not best),
                key=self._orientation_key,
                default=None,
            )
            if best is None or best["matched"] < self.min_matched:
                matched = 0 if best is None else best["matched"]
                logger.error(
                    f"Timing mark registration failed for '{file_path}': matched {matched}/{len(self.expected)} marks"
                )
                return None
        else:
            self._runner_up = None
            for rotation in rotations:
                fit = self.fit_index_points_only(image, page_corners, rotation)
                if fit is not None and (
                    best is None
                    or (fit["index_found"], -fit["residual"])
                    > (best["index_found"], -best["residual"])
                ):
                    best = fit
            if best is None:
                logger.error(
                    f"Index point registration failed for '{file_path}': fewer than 4 index points found"
                )
                return None
        if self.index_points and self.joint_fit:
            self.joint_refit(image, best)
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
        self.record_geometry(
            warp_page,
            {
                "op": "warp",
                "matrix": homography,
                "size": [int(page_w), int(page_h)],
                "inverse": True,
                "border": 255,
            },
        )
        tps = None
        if self.non_rigid == "tracks":
            warped, tps = self.track_grid_correction(warped, best)
        elif self._use_non_rigid(best):
            warped, tps = self.thin_plate_correction(warped, best)

        self.last_registration = {
            "method": "timing_marks" if len(self.expected) else "index_points",
            "orientation": best["rotation"] * 90,
            "matched_marks": best["matched"],
            "expected_marks": int(len(self.expected)),
            "residual_px": round(best["residual"], 3),
        }
        if self.index_points:
            self.last_registration["index_points_found"] = best.get("index_found", 0)
        self.record_alignment_info(**self.alignment_report(image, best, tps))
        logger.info(f"Timing marks: {self.last_registration}")
        return warped

    def ambiguity_review(self, best):
        """
        Sheet review items for a registration that a different fit explains
        almost as well: another orientation (tracks that look the same turned
        round) or the same orientation slid one mark pitch along a track.
        """
        review = []
        runner = getattr(self, "_runner_up", None)
        if runner is not None and self._orientations_tie(best, runner):
            review.append(
                {
                    "kind": "sheet",
                    "name": "orientation",
                    "flags": ["orientation_ambiguous"],
                    "orientations": [int(best["rotation"] * 90), int(runner["rotation"] * 90)],
                }
            )
        slid = self._slide_ties(best)
        if slid:
            review.append(
                {
                    "kind": "sheet",
                    "name": "registration_slide",
                    "flags": ["registration_suspect"],
                    "tracks": slid,
                }
            )
        return review

    def _orientations_tie(self, best, runner):
        if self.index_points and best.get("index_found", 0) > runner.get(
            "index_found", 0
        ):
            # Index points are asymmetric on purpose: they decided it
            return False
        if runner["matched"] < 0.75 * best["matched"]:
            return False
        return runner["residual"] <= max(2.0 * best["residual"], best["residual"] + 1.0)

    def _slide_ties(self, best):
        """Tracks along which a fit shifted by one pitch scores about as well."""
        candidates = getattr(self, "_candidates", None)
        if candidates is None or not len(candidates) or not len(self.expected):
            return []
        homography = best["homography"]
        radius = self.search_radius * self._pixels_per_unit(homography) * 0.5
        best_key = self._slide_key(homography, candidates, radius)
        tied = []
        for name, marks in self.tracks.items():
            if len(marks) < 3:
                continue
            step = np.median(np.diff(marks, axis=0), axis=0)
            for sign in (-1.0, 1.0):
                shift = np.float64(
                    [[1, 0, sign * step[0]], [0, 1, sign * step[1]], [0, 0, 1]]
                )
                if self._slide_key(homography @ shift, candidates, radius) >= best_key - 2:
                    tied.append(name)
                    break
        return tied

    def _slide_key(self, homography, candidates, radius):
        matched = len(self.match(homography, candidates, radius))
        beyond = self._marks_beyond_track_ends(homography, candidates, radius)
        return matched - 2 * beyond

    def _clean_stop(self, best, tried):
        """True when the remaining orientations need not be tried."""
        if not self.early_stop or best is None:
            return False
        clean = (
            best["beyond_ends"] == 0
            and best["matched"] >= GOOD_FIT_FRACTION * len(self.expected)
            and best["residual"] <= 0.5 * self.max_residual
        )
        if not clean:
            return False
        required = sum(1 for p in self.index_points if p["required"])
        if required and best.get("index_found", 0) >= required:
            return True
        # Tracks alone can look the same upside down: 0 and 180 both tried
        return tried >= 2 and best["rotation"] in (0, 2)

    def _orientation_key(self, fit):
        # Index points are asymmetric: they decide between look-alike orientations
        return (
            fit["matched"] + INDEX_POINT_WEIGHT * fit.get("index_found", 0),
            -fit["residual"],
        )

    def _use_non_rigid(self, fit):
        if self.non_rigid == "tracks":
            return False
        points = len(fit["template_pts"])
        if self.non_rigid == "auto":
            return points >= 6 and self._spread_cells(fit["template_pts"]) >= 7
        return bool(self.non_rigid) and points >= 6

    def _spread_cells(self, points):
        """Cells of a 3x3 page grid holding at least one reference point."""
        page_w, page_h = self.page_dimensions
        cols = np.clip((points[:, 0] / page_w * 3).astype(int), 0, 2)
        rows = np.clip((points[:, 1] / page_h * 3).astype(int), 0, 2)
        return len(set(zip(rows.tolist(), cols.tolist())))

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
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
        areas = stats[1:, cv2.CC_STAT_AREA]
        widths = stats[1:, cv2.CC_STAT_WIDTH].astype(np.float32)
        heights = stats[1:, cv2.CC_STAT_HEIGHT].astype(np.float32)
        solidity = areas / np.maximum(widths * heights, 1)
        sized = (areas >= low) & (areas <= high)
        # A mark turned by tens of degrees fills less of its upright bounding
        # box; judge those by the box its own second moments describe
        turned = sized & (solidity <= 0.6)
        if turned.any():
            solidity = np.where(
                turned, _moment_solidity(labels, count, areas, centroids), solidity
            )
        long_side = max(self.mark_w, self.mark_h) * scale
        keep = (
            sized
            & (solidity > 0.6)
            & (
                np.maximum(widths, heights)
                <= long_side * (1 + self.size_tolerance) * 1.5
            )
        )
        return (centroids[1:][keep] / shrink).astype(np.float32)

    # --- fitting ---------------------------------------------------------

    def fit_orientation(self, page_corners, candidates, rotation, image=None):
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
        guesses = self._starting_guesses(coarse, candidates)
        if self.index_seed and image is not None and len(self.index_points) >= 4:
            # The index points' own fit (found by size and shape, not as
            # track-like blobs) as one more starting guess, tried first
            seed = self.fit_index_points_only(image, page_corners, rotation)
            if seed is not None and _well_conditioned(seed["homography"]):
                guesses = [np.asarray(seed["homography"], np.float64)] + list(guesses)
        for start in guesses:
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
        if homography is None or not _well_conditioned(homography):
            return None
        try:
            residual = self.residual_in_template_units(
                homography, template_pts, image_pts
            )
        except np.linalg.LinAlgError:
            # A degenerate guess: this start failed, the next one may not
            return None
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
        tps = {
            "points": [[float(x), float(y)] for x, y in targets],
            "weights": [[float(a), float(b)] for a, b in weights],
            "grid_step": TPS_GRID_STEP,
            "size": [page_w, page_h],
        }
        map_x, map_y = tps_maps(tps)

        def remap(im):
            return cv2.remap(im, map_x, map_y, cv2.INTER_LINEAR, borderValue=255)

        self.record_geometry(remap, dict(tps, op="tps"))
        return remap(warped), tps

    def track_grid_correction(self, warped, fit):
        """
        Local correction from the timing marks used as row and column rulers
        (nonRigid: "tracks"). A vertical track measures each row's offset, a
        horizontal track each column's; with tracks on both sides the offset
        is interpolated across the page. Offsets are held, never extrapolated,
        past a track's ends; a direction without a track keeps the homography.
        """
        page_w, page_h = int(self.page_dimensions[0]), int(self.page_dimensions[1])
        template_pts = np.asarray(fit.get("template_pts", []), np.float64)
        if not len(template_pts):
            return warped, None
        inverse = np.linalg.inv(fit["homography"])
        observed = cv2.perspectiveTransform(
            np.asarray(fit["image_pts"], np.float64)[None], inverse
        )[0]
        shift = observed - template_pts
        # Coarse grid: kept in result.json for replay, offsets vary slowly
        step = TRACK_GRID_STEP
        gx = np.arange(0, page_w + step, step, dtype=np.float64)
        gy = np.arange(0, page_h + step, step, dtype=np.float64)
        rows, cols = [], []  # (position across, coords along, offsets along)
        for marks in self.tracks.values():
            if len(marks) < 2:
                continue
            d = np.linalg.norm(template_pts[:, None] - marks[None].astype(np.float64), axis=2)
            hit = d.min(axis=1) < 0.5
            if hit.sum() < 2:
                continue
            pts, offs = template_pts[hit], shift[hit]
            vertical = np.ptp(marks[:, 1]) >= np.ptp(marks[:, 0])
            if vertical:
                order = np.argsort(pts[:, 1])
                rows.append((float(np.median(pts[:, 0])), pts[order, 1], offs[order, 1]))
            else:
                order = np.argsort(pts[:, 0])
                cols.append((float(np.median(pts[:, 1])), pts[order, 0], offs[order, 0]))

        def blend(rulers, along, across):
            """Offsets on the grid: each ruler interpolated along itself, then
            across between the outermost two rulers (clamped, no extrapolation)."""
            rulers = sorted(rulers, key=lambda r: r[0])
            first, last = rulers[0], rulers[-1]
            a = np.interp(along, first[1], first[2])
            if len(rulers) == 1 or last[0] - first[0] < 1:
                return np.repeat(a[:, None], len(across), axis=1)
            b = np.interp(along, last[1], last[2])
            t = np.clip((across - first[0]) / (last[0] - first[0]), 0.0, 1.0)
            return a[:, None] * (1 - t[None]) + b[:, None] * t[None]

        dy = blend(rows, gy, gx) if rows else np.zeros((len(gy), len(gx)))
        dx = blend(cols, gx, gy).T if cols else np.zeros((len(gy), len(gx)))
        grid = {
            "grid": {"dx": np.round(dx, 2).tolist(), "dy": np.round(dy, 2).tolist()},
            "grid_step": step,
            "size": [page_w, page_h],
        }
        map_x, map_y = tps_maps(grid)

        def remap(im):
            return cv2.remap(im, map_x, map_y, cv2.INTER_LINEAR, borderValue=255)

        self.record_geometry(remap, dict(grid, op="tps"))
        return remap(warped), grid

    # --- index points ------------------------------------------------------

    def locate_index_points(self, image, fit, radius_units=None):
        """Find each index point near where fit's homography puts it."""
        homography = fit["homography"]
        ppu = self._pixels_per_unit(homography)
        found = []
        for point in self.index_points:
            radius = radius_units
            if radius is None:
                # Close to where the fit puts it: a fit from many marks is
                # accurate to a few px even where the page curls
                radius = max(INDEX_SEARCH_UNITS, 0.5 * float(point["size"].max()))
            centre = cv2.perspectiveTransform(point["center"][None, None], homography)[
                0, 0
            ]
            found.append(self._find_index_point(image, centre, point, ppu, radius * ppu))
        fit["index_image_pts"] = found
        fit["index_found"] = int(sum(1 for p in found if p is not None))
        return found

    def _find_index_point(self, image, centre, point, ppu, radius):
        w, h = float(point["size"][0]) * ppu, float(point["size"][1]) * ppu
        half = 0.5 * max(w, h) * (1 + self.size_tolerance) + radius
        img_h, img_w = image.shape[:2]
        left, top = int(max(0, centre[0] - half)), int(max(0, centre[1] - half))
        right = int(min(img_w, centre[0] + half + 1))
        bottom = int(min(img_h, centre[1] + half + 1))
        if right - left < 3 or bottom - top < 3:
            return None
        window = image[top:bottom, left:right]
        if window.ndim == 3:
            window = cv2.cvtColor(window, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(
            cv2.GaussianBlur(window, (3, 3), 0),
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )
        count, _, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
        tolerance = max(self.size_tolerance, 0.3)
        best, best_distance = None, None
        for i in range(1, count):
            bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
            area = stats[i, cv2.CC_STAT_AREA]
            # Orientation is unknown here: compare sizes sorted
            if not _size_matches((bw, bh), (w, h), tolerance):
                continue
            extent = area / float(max(bw * bh, 1))
            shape = point["shape"]
            if shape in ("square", "circle") and not (
                0.7 <= bw / float(max(bh, 1)) * (h / max(w, 1e-6)) <= 1.4
                or 0.7 <= bh / float(max(bw, 1)) * (h / max(w, 1e-6)) <= 1.4
            ):
                continue
            if shape == "square" and extent < 0.75:
                continue
            if shape == "circle" and not 0.6 <= extent <= 0.92:
                continue
            if shape == "any" and extent < 0.3:
                continue
            cx, cy = centroids[i][0] + left, centroids[i][1] + top
            distance = float(np.hypot(cx - centre[0], cy - centre[1]))
            if distance > radius + 0.5 * max(w, h):
                continue
            if best is None or distance < best_distance:
                best, best_distance = (float(cx), float(cy)), distance
        return best

    def fit_index_points_only(self, image, page_corners, rotation):
        """Registration from index points alone (no timing tracks)."""
        page_w, page_h = self.page_dimensions
        coarse = self.coarse_homography(page_corners, rotation)
        fit = {"homography": coarse, "rotation": rotation, "matched": 0}
        wide = 0.08 * float(np.hypot(page_w, page_h))
        for radius in (wide, wide / 3.0, None):
            found = self.locate_index_points(image, fit, radius)
            pairs = [
                (p["center"], f) for p, f in zip(self.index_points, found) if f is not None
            ]
            if len(pairs) < 4:
                return None
            template_pts = np.float32([p for p, _ in pairs])
            image_pts = np.float32([f for _, f in pairs])
            homography, _ = cv2.findHomography(template_pts, image_pts, 0)
            if homography is None:
                return None
            fit["homography"] = homography
        fit["template_pts"] = template_pts
        fit["image_pts"] = image_pts
        fit["residual"] = float(
            self.residual_in_template_units(homography, template_pts, image_pts)
        )
        return fit

    def joint_refit(self, image, fit):
        """One homography from timing marks, index points and printed block corners."""
        template_pts = [fit["template_pts"]] if len(fit.get("template_pts", [])) else []
        image_pts = [fit["image_pts"]] if len(fit.get("image_pts", [])) else []
        found = fit.get("index_image_pts") or self.locate_index_points(image, fit)
        if len(self.expected):
            # A point far off the marks' fit is something else (a letter, a
            # mark): count it as not found rather than bend the page to it
            found = list(found)
            for i, (point, position) in enumerate(zip(self.index_points, found)):
                if position is None:
                    continue
                back = cv2.perspectiveTransform(
                    np.float32([[position]]), np.linalg.inv(fit["homography"])
                )[0, 0]
                limit = max(self.max_residual, 0.25 * float(point["size"].max()))
                if float(np.linalg.norm(back - point["center"])) > limit:
                    found[i] = None
            fit["index_image_pts"] = found
            fit["index_found"] = int(sum(1 for p in found if p is not None))
        index_pairs = [
            (p["center"], f) for p, f in zip(self.index_points, found) if f is not None
        ]
        if index_pairs and len(self.expected):
            template_pts.append(np.float32([p for p, _ in index_pairs]))
            image_pts.append(np.float32([f for _, f in index_pairs]))
        block_pairs = self.block_corner_pairs(image, fit["homography"])
        if block_pairs:
            template_pts.append(np.float32([p for p, _ in block_pairs]))
            image_pts.append(np.float32([f for _, f in block_pairs]))
        if not template_pts:
            return
        template_pts = np.concatenate(template_pts).astype(np.float32)
        image_pts = np.concatenate(image_pts).astype(np.float32)
        if len(template_pts) < 4:
            return
        homography, _ = cv2.findHomography(template_pts, image_pts, 0)
        if homography is None:
            return
        fit["homography"] = homography
        fit["template_pts"] = template_pts
        fit["image_pts"] = image_pts
        fit["block_corners_used"] = len(block_pairs)
        fit["residual"] = float(
            self.residual_in_template_units(homography, template_pts, image_pts)
        )

    def block_corner_pairs(self, image, homography):
        """(template corner, image corner) of printed block borders that are found."""
        template = getattr(self, "template", None)
        if template is None or not getattr(template, "field_blocks", None):
            return []
        ops = self.image_instance_ops
        default = ops.alignment_option(template, "rectify_on_border", False)
        search = ops.alignment_option(template, "rectify_search_px", 20)
        blocks = [
            b
            for b in template.field_blocks
            if (default if b.rectify_on_border is None else b.rectify_on_border)
        ]
        if not blocks:
            return []
        from src.rectify import find_border, padding_of

        page_w, page_h = (int(v) for v in self.page_dimensions)
        page = cv2.warpPerspective(
            image,
            homography,
            (page_w, page_h),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderValue=255,
        )
        pairs = []
        for block in blocks:
            padding = padding_of(block)
            if padding is None:
                # Without a known gap the expected corners are not fixed
                continue
            found = find_border(page, block, padding, search)
            if isinstance(found, str):
                continue
            corners, expected = found
            in_image = cv2.perspectiveTransform(corners[None], homography)[0]
            pairs.extend(zip(expected, in_image))
        return pairs

    # --- reporting -----------------------------------------------------------

    def alignment_report(self, image, fit, tps):
        """Residuals per page region, trimmed margins and index points found."""
        homography = fit["homography"]
        inverse = np.linalg.inv(homography)
        template_pts = fit["template_pts"]
        observed = cv2.perspectiveTransform(fit["image_pts"][None], inverse)[0]
        if tps is not None:
            from src.geometry import map_points

            observed = np.float32(
                map_points({"steps": [dict(tps, op="tps")]}, observed)
            )
        errors = np.linalg.norm(observed - template_pts, axis=1)
        page_w, page_h = self.page_dimensions
        regions = {}
        cols = np.clip((template_pts[:, 0] / page_w * 3).astype(int), 0, 2)
        rows = np.clip((template_pts[:, 1] / page_h * 3).astype(int), 0, 2)
        for (row, col), name in REGION_NAMES.items():
            mask = (rows == row) & (cols == col)
            if mask.any():
                regions[name] = {
                    "mean": round(float(errors[mask].mean()), 3),
                    "max": round(float(errors[mask].max()), 3),
                    "points": int(mask.sum()),
                }
        info = {
            "rotation": int(fit["rotation"] * 90),
            "alignment_method": self.last_registration.get("method"),
            "residual": {
                "page": round(float(errors.mean()), 3) if len(errors) else 0.0,
                "regions": regions,
            },
        }
        review = []
        if self.max_region_residual is not None:
            bad = sorted(
                name
                for name, region in regions.items()
                if region["mean"] > self.max_region_residual
            )
            if bad:
                review.append(
                    {
                        "kind": "sheet",
                        "name": "alignment_residual",
                        "flags": ["alignment_residual"],
                        "regions": bad,
                        "max_region_residual": self.max_region_residual,
                    }
                )
        if self.index_points:
            found = fit.get("index_image_pts") or [None] * len(self.index_points)
            info["index_points"] = [
                {
                    "name": point["name"],
                    "found": position is not None,
                    "image": None
                    if position is None
                    else [round(position[0], 2), round(position[1], 2)],
                }
                for point, position in zip(self.index_points, found)
            ]
            missing = [
                point["name"]
                for point, position in zip(self.index_points, found)
                if position is None and point["required"]
            ]
            if missing:
                review.append(
                    {
                        "kind": "sheet",
                        "name": "index_points",
                        "flags": ["index_point_missing"],
                        "missing": missing,
                    }
                )
        if len(self.expected):
            review.extend(self.ambiguity_review(fit))
        if self.trim_margins:
            info["margin_trim"] = self.margin_trim(image, homography)
        if review:
            info["review"] = review
        return info

    def margin_trim(self, image, homography):
        """Template px of the scan that lie outside the template page, per side."""
        h, w = image.shape[:2]
        corners = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
        mapped = cv2.perspectiveTransform(corners[None], np.linalg.inv(homography))[0]
        page_w, page_h = self.page_dimensions
        return {
            "top": round(float(max(0.0, -mapped[:, 1].min())), 1),
            "bottom": round(float(max(0.0, mapped[:, 1].max() - (page_h - 1))), 1),
            "left": round(float(max(0.0, -mapped[:, 0].min())), 1),
            "right": round(float(max(0.0, mapped[:, 0].max() - (page_w - 1))), 1),
        }


def _well_conditioned(homography, max_condition=1e8):
    """False for a singular or nearly singular homography (a collapsed guess)."""
    if not np.all(np.isfinite(homography)):
        return False
    try:
        return bool(np.linalg.cond(homography) < max_condition)
    except np.linalg.LinAlgError:
        return False


def _moment_solidity(labels, count, areas, centroids):
    """Area over the area of the rectangle with the same second moments, per
    component (about 1 for a solid rectangle at any angle, lower for rings)."""
    ys, xs = np.nonzero(labels)
    ids = labels[ys, xs]
    dx = xs - centroids[ids, 0]
    dy = ys - centroids[ids, 1]
    n = np.maximum(np.bincount(ids, minlength=count).astype(np.float64), 1)
    sxx = np.bincount(ids, dx * dx, minlength=count) / n
    syy = np.bincount(ids, dy * dy, minlength=count) / n
    sxy = np.bincount(ids, dx * dy, minlength=count) / n
    det = np.maximum(sxx * syy - sxy * sxy, 1e-9)
    rect = 12.0 * np.sqrt(det)
    return (areas / np.maximum(rect[1:], 1.0)).astype(np.float32)


def _size_matches(found, expected, tolerance):
    a = sorted(float(v) for v in found)
    b = sorted(float(v) for v in expected)
    return all(
        (1 - tolerance) * e - 2 <= f <= (1 + tolerance) * e + 2 for f, e in zip(a, b)
    )


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
