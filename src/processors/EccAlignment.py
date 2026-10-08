"""
Dense intensity-based refinement with the ECC algorithm (Evangelidis & Psarakis,
2008), OpenCV's findTransformECC.

Feature- or mark-based registration gets a sheet to within a few pixels; ECC
then maximises the correlation between the sheet and a clean reference image of
the blank form to remove the remaining sub-pixel to few-pixel offsets. It runs on
a downscaled copy for speed and applies the scaled transform at full resolution.
Use it after CropOnMarkers / TimingMarkAlignment, not as the only step.
"""

import cv2
import numpy as np

from src.logger import logger
from src.processors.interfaces.ImagePreprocessor import ImagePreprocessor
from src.utils.image import ImageUtils

MOTION_TYPES = {
    "translation": cv2.MOTION_TRANSLATION,
    "euclidean": cv2.MOTION_EUCLIDEAN,
    "affine": cv2.MOTION_AFFINE,
    "homography": cv2.MOTION_HOMOGRAPHY,
}


class EccAlignment(ImagePreprocessor):
    geometry = "recorded"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        options = self.options
        self.ref_path = self.relative_dir.joinpath(options["reference"])
        reference = cv2.imread(str(self.ref_path), cv2.IMREAD_GRAYSCALE)
        if reference is None:
            raise FileNotFoundError(f"ECC reference image not found: {self.ref_path}")
        self.reference = reference
        self.motion = MOTION_TYPES[options.get("motion", "affine")]
        self.iterations = int(options.get("iterations", 50))
        self.epsilon = float(options.get("epsilon", 1e-4))
        self.scale = float(options.get("scale", 0.5))
        self.min_correlation = float(options.get("minCorrelation", 0.5))
        self.last_correlation = None

    def __str__(self):
        return self.ref_path.name

    def exclude_files(self):
        return [self.ref_path]

    def apply_filter(self, image, file_path):
        ref_h, ref_w = self.reference.shape[:2]
        image = ImageUtils.resize_util(image, ref_w, ref_h)
        self.record_geometry(
            lambda im: ImageUtils.resize_util(im, ref_w, ref_h),
            {"op": "resize", "size": [ref_w, ref_h]},
        )
        small_w, small_h = int(ref_w * self.scale), int(ref_h * self.scale)
        reference_small = cv2.resize(self.reference, (small_w, small_h)).astype(
            np.float32
        )
        image_small = cv2.resize(image, (small_w, small_h)).astype(np.float32)

        if self.motion == cv2.MOTION_HOMOGRAPHY:
            warp = np.eye(3, dtype=np.float32)
        else:
            warp = np.eye(2, 3, dtype=np.float32)
        criteria = (
            cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
            self.iterations,
            self.epsilon,
        )
        try:
            correlation, warp = cv2.findTransformECC(
                reference_small, image_small, warp, self.motion, criteria, None, 5
            )
        except cv2.error as error:
            logger.error(f"ECC alignment did not converge for '{file_path}': {error}")
            return None
        self.last_correlation = float(correlation)
        if correlation < self.min_correlation:
            logger.error(
                f"ECC alignment rejected for '{file_path}': correlation {correlation:.3f} < {self.min_correlation}"
            )
            return None

        # Scale the translation terms back to full resolution
        if self.motion == cv2.MOTION_HOMOGRAPHY:
            scale = np.diag([self.scale, self.scale, 1.0]).astype(np.float32)
            warp = np.linalg.inv(scale) @ warp @ scale
            warp_image = cv2.warpPerspective
        else:
            warp[:, 2] /= self.scale
            warp_image = cv2.warpAffine

        def transform(im):
            return warp_image(
                im,
                warp,
                (ref_w, ref_h),
                flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                borderValue=255,
            )

        self.record_geometry(
            transform,
            {
                "op": "warp",
                "matrix": warp,
                "size": [ref_w, ref_h],
                "inverse": True,
                "affine": self.motion != cv2.MOTION_HOMOGRAPHY,
                "border": 255,
            },
        )
        return transform(image)
