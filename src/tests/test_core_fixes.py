"""Bubble reading edge cases: bubbles off the page and block shifts per sheet."""

import json
import random
from pathlib import Path

import numpy as np

from src.core import ImageInstanceOps
from src.pipeline import OMREngine
from src.synth import default_spec, random_answers, render_sheet
from src.utils.parsing import Settings


def test_a_bubble_off_the_page_is_not_read_as_marked():
    page = np.full((100, 100), 255, np.uint8)
    page[:10, :10] = 0
    # Fully off the page: white, not the empty slice's mean of 0 (a full mark)
    assert ImageInstanceOps.bubble_mean(page, -30, -30, 20, 20) == 255.0
    assert ImageInstanceOps.bubble_mean(page, 120, 10, 20, 20) == 255.0
    # Partly off: only the part on the page counts (no wrap-around to the far side)
    assert ImageInstanceOps.bubble_mean(page, -10, -10, 20, 20) == 0.0
    assert not ImageInstanceOps.bubble_on_page(page, -10, -10, 20, 20)
    assert ImageInstanceOps.bubble_on_page(page, 0, 0, 20, 20)


def test_out_of_page_always_goes_to_review():
    params = Settings(
        {
            "min_marked_fill_ratio": 0.25,
            "max_unmarked_fill_ratio": 0.6,
            "min_confidence": 0.0,
            "review_flags": [],
        }
    )
    bubbles = [{"marked": False, "fill_ratio": 0.0, "confidence": 1.0}]
    field = ImageInstanceOps.summarize_field(
        "q1", "", bubbles, 1, False, params, ["out_of_page"]
    )
    assert "out_of_page" in field["flags"] and field["needs_review"]


def test_block_shift_does_not_carry_over_to_the_next_sheet(tmp_path):
    spec = default_spec(questions=10, roll_digits=2, with_zones=False)
    path = Path(tmp_path, "template.json")
    path.write_text(json.dumps(spec.to_template(pre_processors=[])))
    rng = random.Random(3)
    image, truth = render_sheet(spec, random_answers(spec, rng), rng=rng)
    engine = OMREngine(path)
    expected = engine.scan(image).responses
    # A shift left over from an earlier sheet (as a failed block snap returned it)
    for block in engine.template.field_blocks:
        block.shift = 9
    again = engine.scan(image)
    assert all(block.shift == 0 for block in engine.template.field_blocks)
    assert again.responses == expected
