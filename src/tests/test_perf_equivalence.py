"""Speed-ups that must not change a single output (Gemini tracker items 43b, 44)."""

import random

import cv2
import numpy as np
from dotmap import DotMap

from src.core import ImageInstanceOps, _ellipse_mask
from src.defaults import CONFIG_DEFAULTS


def make_ops():
    return ImageInstanceOps(DotMap(CONFIG_DEFAULTS.toDict(), _dynamic=False))


def reference_global_threshold(ops, q_vals_orig, looseness=1):
    """The element-by-element loop the vectorised version replaced."""
    params = ops.tuning_config.threshold_params
    min_jump, jump_delta = params.MIN_JUMP, params.JUMP_DELTA
    from src.constants.common import (
        GLOBAL_PAGE_THRESHOLD_BLACK,
        GLOBAL_PAGE_THRESHOLD_WHITE,
    )

    default = (
        GLOBAL_PAGE_THRESHOLD_WHITE
        if params.PAGE_TYPE_FOR_THRESHOLD == "white"
        else GLOBAL_PAGE_THRESHOLD_BLACK
    )
    q_vals = sorted(q_vals_orig)
    ls = (looseness + 1) // 2
    l = len(q_vals) - ls
    max1, thr1 = min_jump, default
    for i in range(ls, l):
        jump = q_vals[i + ls] - q_vals[i - ls]
        if jump > max1:
            max1 = jump
            thr1 = q_vals[i - ls] + jump / 2
    max2, thr2 = min_jump, default
    for i in range(ls, l):
        jump = q_vals[i + ls] - q_vals[i - ls]
        new_thr = q_vals[i - ls] + jump / 2
        if jump > max2 and abs(thr1 - new_thr) > jump_delta:
            max2 = jump
            thr2 = new_thr
    if max1 == min_jump and len(q_vals) >= 4:
        otsu_thr = ops.otsu_threshold(q_vals)
        if otsu_thr is not None:
            thr1 = otsu_thr
    return (thr1, thr1 - max1 // 2, thr1 + max1 // 2), thr2


def test_vectorised_global_threshold_matches_loop():
    ops = make_ops()
    rng = random.Random(7)
    cases = [[], [120.0], [100.0, 200.0], [50.0, 60.0, 70.0]]
    for _ in range(400):
        n = rng.randint(0, 120)
        kind = rng.random()
        if kind < 0.3:  # two clusters: marked and empty bubbles
            vals = [rng.gauss(80, 15) for _ in range(n // 4)] + [
                rng.gauss(210, 10) for _ in range(n - n // 4)
            ]
        elif kind < 0.5:  # integer-valued with ties (equal jumps)
            vals = [float(rng.choice([40, 90, 140, 190, 240])) for _ in range(n)]
        elif kind < 0.7:  # std-devs, rounded like the engine does
            vals = [round(np.float64(abs(rng.gauss(20, 25))), 2) for _ in range(n)]
        else:
            vals = [rng.uniform(0, 255) for _ in range(n)]
        cases.append(vals)
    for vals in cases:
        for looseness in (1, 2, 4, 5):
            expected, _ = reference_global_threshold(ops, vals, looseness)
            got = ops.get_global_threshold(vals, looseness=looseness)
            assert got == expected, (vals, looseness, got, expected)


def test_cached_ellipse_mask_matches_drawn_mask():
    for h in range(1, 40, 3):
        for w in range(1, 40, 4):
            mask = np.zeros((h, w), dtype=np.uint8)
            cv2.ellipse(
                mask,
                (w // 2, h // 2),
                (max(int(w * 0.35), 1), max(int(h * 0.35), 1)),
                0, 0, 360, 255, -1,
            )
            cached = _ellipse_mask(h, w)
            assert np.array_equal(cached, mask > 0)
            assert cached is _ellipse_mask(h, w)
            assert not cached.flags.writeable


def test_fill_ratio_unchanged_by_cache():
    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, size=(200, 300), dtype=np.uint8)
    for _ in range(200):
        x, y = int(rng.integers(-5, 290)), int(rng.integers(-5, 190))
        w, h = int(rng.integers(1, 30)), int(rng.integers(1, 30))
        thr = float(rng.integers(0, 256))
        roi = img[max(y, 0) : y + h, max(x, 0) : x + w]
        if roi.size == 0:
            expected = 0.0
        else:
            rh, rw = roi.shape
            mask = np.zeros((rh, rw), dtype=np.uint8)
            cv2.ellipse(
                mask, (rw // 2, rh // 2),
                (max(int(rw * 0.35), 1), max(int(rh * 0.35), 1)),
                0, 0, 360, 255, -1,
            )
            inside = roi[mask > 0]
            expected = (
                0.0
                if inside.size == 0
                else float(np.count_nonzero(inside < thr)) / float(inside.size)
            )
        assert ImageInstanceOps.get_fill_ratio(img, x, y, w, h, thr) == expected
