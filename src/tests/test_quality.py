import random

import cv2
import numpy as np

from src.quality import measure, review
from src.synth import default_spec, random_answers, render_sheet

LIMITS = {"min_sharpness": 10, "min_contrast": 40, "min_bubble_px": 5}


def test_quality_gate_flags_only_unreadable_images():
    spec = default_spec(questions=10, with_zones=False)
    rng = random.Random(1)
    image, _ = render_sheet(spec, random_answers(spec, rng), rng=rng)
    identity = {"page_homography": np.eye(3).tolist()}
    good = measure(image, identity, 20)
    assert review(good, LIMITS) == []

    washed = (image.astype(np.float32) * 0.1 + 200).astype(np.uint8)
    blurred = cv2.GaussianBlur(washed, (0, 0), 12)
    items = review(measure(blurred, identity, 20), LIMITS)
    assert items and items[0]["name"] == "poor_image"
    assert set(items[0]["measures"]) >= {"contrast", "sharpness"}

    # Bubbles 3 scan pixels wide: the page was shrunk 7x
    tiny = {"page_homography": np.diag([7.0, 7.0, 1.0]).tolist()}
    assert "bubble_px" in review(measure(image, tiny, 20), LIMITS)[0]["measures"]
