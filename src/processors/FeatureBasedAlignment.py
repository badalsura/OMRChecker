"""
Image based feature alignment
Credits: https://www.learnopencv.com/image-alignment-feature-based-using-opencv-c-python/
"""

import cv2
import numpy as np

from src.constants.image_processing import (
    DEFAULT_GOOD_MATCH_PERCENT,
    DEFAULT_MAX_FEATURES,
    DEFAULT_MAX_TRANSFORM_SCALE_CHANGE,
    DEFAULT_MIN_ALIGNMENT_INLIERS,
)
from src.logger import logger
from src.processors.interfaces.ImagePreprocessor import ImagePreprocessor
from src.utils.image import ImageUtils
from src.utils.interaction import InteractionUtils


class FeatureBasedAlignment(ImagePreprocessor):
    geometry = "recorded"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        options = self.options
        config = self.tuning_config

        # process reference image
        self.ref_path = self.relative_dir.joinpath(options["reference"])
        ref_img = cv2.imread(str(self.ref_path), cv2.IMREAD_GRAYSCALE)
        self.ref_img = ImageUtils.resize_util(
            ref_img,
            config.dimensions.processing_width,
            config.dimensions.processing_height,
        )
        # get options with defaults
        self.max_features = int(options.get("maxFeatures", DEFAULT_MAX_FEATURES))
        self.good_match_percent = options.get(
            "goodMatchPercent", DEFAULT_GOOD_MATCH_PERCENT
        )
        self.transform_2_d = options.get("2d", False)
        self.min_inliers = int(options.get("minInliers", DEFAULT_MIN_ALIGNMENT_INLIERS))
        self.max_scale_change = float(
            options.get("maxScaleChange", DEFAULT_MAX_TRANSFORM_SCALE_CHANGE)
        )
        # Extract keypoints and description of source image
        self.orb = cv2.ORB_create(self.max_features)
        self.to_keypoints, self.to_descriptors = self.orb.detectAndCompute(
            self.ref_img, None
        )

    def __str__(self):
        return self.ref_path.name

    def exclude_files(self):
        return [self.ref_path]

    def apply_filter(self, image, file_path):
        config = self.tuning_config
        # Convert images to grayscale
        # im1Gray = cv2.cvtColor(im1, cv2.COLOR_BGR2GRAY)
        # im2Gray = cv2.cvtColor(im2, cv2.COLOR_BGR2GRAY)

        image = cv2.normalize(image, 0, 255, norm_type=cv2.NORM_MINMAX)

        # Detect ORB features and compute descriptors.
        from_keypoints, from_descriptors = self.orb.detectAndCompute(image, None)
        if from_descriptors is None or self.to_descriptors is None:
            logger.error(f"No features found for alignment in '{file_path}'")
            return None

        # Match features.
        matcher = cv2.DescriptorMatcher_create(
            cv2.DESCRIPTOR_MATCHER_BRUTEFORCE_HAMMING
        )

        # create BFMatcher object (alternate matcher)
        # matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

        # Lowe's ratio test drops ambiguous matches (common on repetitive bubble grids)
        knn_matches = matcher.knnMatch(from_descriptors, self.to_descriptors, k=2)
        matches = [
            pair[0]
            for pair in knn_matches
            if len(pair) == 2 and pair[0].distance < 0.8 * pair[1].distance
        ]

        # Sort matches by score
        matches = sorted(matches, key=lambda x: x.distance, reverse=False)

        # Remove not so good matches
        num_good_matches = int(len(matches) * self.good_match_percent)
        matches = matches[:num_good_matches]

        # Draw top matches
        if config.outputs.show_image_level > 2:
            im_matches = cv2.drawMatches(
                image, from_keypoints, self.ref_img, self.to_keypoints, matches, None
            )
            InteractionUtils.show("Aligning", im_matches, resize=True, config=config)

        # Extract location of good matches
        points1 = np.zeros((len(matches), 2), dtype=np.float32)
        points2 = np.zeros((len(matches), 2), dtype=np.float32)

        for i, match in enumerate(matches):
            points1[i, :] = from_keypoints[match.queryIdx].pt
            points2[i, :] = self.to_keypoints[match.trainIdx].pt

        if len(matches) < self.min_inliers:
            logger.error(
                f"Too few feature matches ({len(matches)}) to align '{file_path}'"
            )
            return None

        # Find homography
        height, width = self.ref_img.shape
        if self.transform_2_d:
            m, inliers = cv2.estimateAffine2D(points1, points2)
            if not self.is_transform_sane(m, inliers, file_path):
                return None
            self.record_geometry(lambda im: cv2.warpAffine(im, m, (width, height)))
            return cv2.warpAffine(image, m, (width, height))

        # Use homography
        h, inliers = cv2.findHomography(points1, points2, cv2.RANSAC)
        if not self.is_transform_sane(h, inliers, file_path):
            return None
        self.record_geometry(lambda im: cv2.warpPerspective(im, h, (width, height)))
        return cv2.warpPerspective(image, h, (width, height))

    def is_transform_sane(self, matrix, inliers, file_path):
        """Reject degenerate or wildly distorting transforms instead of silently warping."""
        if matrix is None or inliers is None:
            logger.error(f"Could not estimate an alignment transform for '{file_path}'")
            return False
        inlier_count = int(np.count_nonzero(inliers))
        if inlier_count < self.min_inliers:
            logger.error(
                f"Too few alignment inliers ({inlier_count}) for '{file_path}'"
            )
            return False
        # Area scale factor of the linear part must stay near 1 for a page-to-page warp
        det = float(np.linalg.det(matrix[:2, :2]))
        low, high = 1 / self.max_scale_change, self.max_scale_change
        if not (low <= det <= high):
            logger.error(
                f"Alignment transform rejected for '{file_path}': scale factor {det:.2f} outside [{low:.2f}, {high:.2f}]"
            )
            return False
        return True
