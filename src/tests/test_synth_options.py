"""Synthetic renderer options: colour print and ink, block borders, local warps."""

import random

import cv2
import numpy as np
import pytest

from src.synth import augment, default_spec, random_answers, render_sheet

SCAN = dict(
    rotation=1.0,
    perspective=0.005,
    blur=1.0,
    noise=4.0,
    shadow=0.15,
    jpeg_quality=(80, 95),
)


@pytest.fixture(scope="module")
def bordered_spec():
    spec = default_spec(questions=40, roll_digits=6, with_zones=False)
    spec.block_border = 8
    return spec


def test_synth_block_border_and_colour_render(bordered_spec):
    rng = random.Random(1)
    answers = random_answers(bordered_spec, rng)
    image, _ = render_sheet(bordered_spec, answers, rng=random.Random(1))
    assert image.ndim == 2
    x0, y0, x1, y1 = bordered_spec.block_box(bordered_spec.blocks[1])
    # The border line sits 8px outside the bubbles
    assert image[y0 - 8, (x0 + x1) // 2] < 128
    colour, _ = render_sheet(
        bordered_spec, answers, rng=random.Random(1), print_color="#E8618C"
    )
    assert colour.ndim == 3
    assert (cv2.cvtColor(colour, cv2.COLOR_BGR2GRAY) <= 255).all()
    scanned, _ = augment(colour, random.Random(2), **SCAN)
    assert scanned.ndim == 3 and scanned.shape[2] == 3
    # Without the new options the render is unchanged: grey and deterministic
    again, _ = render_sheet(bordered_spec, answers, rng=random.Random(1))
    assert np.array_equal(image, again)
