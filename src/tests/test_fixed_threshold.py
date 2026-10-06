"""Fixed threshold mode (threshold_params.mode = "fixed")."""

import json
import random
from pathlib import Path

import pytest

from src.core import ImageInstanceOps
from src.pipeline import OMREngine
from src.synth import default_spec, random_answers, render_sheet
from src.utils.validations import validate_config_json


def write_template(directory, template):
    path = Path(directory, "template.json")
    path.write_text(json.dumps(template))
    return path


def wrong_answers(result, truth, labels=None):
    labels = labels or truth["answers"].keys()
    return [k for k in labels if result.responses[k] != truth["answers"][k]]


# --------------------------------------------------------------------------- fixed


@pytest.fixture(scope="module")
def small_spec():
    return default_spec(questions=20, roll_digits=4, with_zones=False)


def test_fixed_threshold_reads_without_adaptive_search(
    tmp_path, small_spec, monkeypatch
):
    path = write_template(tmp_path, small_spec.to_template(pre_processors=[]))
    rng = random.Random(2)
    answers = random_answers(small_spec, rng, blank_rate=0.1)
    image, truth = render_sheet(small_spec, answers, rng=rng, mark_style="mixed")

    adaptive = OMREngine(path).scan(image)
    assert "mode" not in adaptive.thresholds

    def no_search(*args, **kwargs):
        raise AssertionError("adaptive threshold computed in fixed mode")

    monkeypatch.setattr(ImageInstanceOps, "get_global_threshold", no_search)
    monkeypatch.setattr(ImageInstanceOps, "get_local_threshold", no_search)
    config = {
        "threshold_params": {
            "mode": "fixed",
            "fixed_threshold": 140,
            "fixed_min_fill_ratio": 0.15,
        }
    }
    fixed = OMREngine(path, config_overrides=config).scan(image)
    assert not wrong_answers(fixed, truth)
    assert fixed.thresholds["mode"] == "fixed"
    assert fixed.thresholds["global"] == 140
    for details in fixed.fields.values():
        assert "ambiguous_threshold" not in details["flags"]
        for bubble in details["bubbles"]:
            # Marked exactly when enough of the interior is darker than the line
            assert bubble["marked"] == (bubble["fill_ratio"] >= 0.15)
            expected = min(abs(bubble["fill_ratio"] - 0.15) / 0.15, 1.0)
            assert bubble["confidence"] == pytest.approx(expected, abs=5e-3)

    # A line darker than the pencil marks misses them
    strict = dict(config["threshold_params"], fixed_threshold=40)
    missed = OMREngine(path, config_overrides={"threshold_params": strict}).scan(image)
    assert wrong_answers(missed, truth)


def test_fixed_threshold_config_schema(tmp_path):
    from src.defaults import CONFIG_DEFAULTS
    from src.utils.parsing import OVERRIDE_MERGER

    def config_with(**threshold):
        merged = OVERRIDE_MERGER.merge(
            CONFIG_DEFAULTS.toDict(), {"threshold_params": threshold}
        )
        return merged

    validate_config_json(config_with(mode="fixed", fixed_threshold=100), "c.json")
    assert CONFIG_DEFAULTS.threshold_params.mode == "adaptive"
    for bad in (
        {"mode": "otsu"},
        {"fixed_threshold": 300},
        {"fixed_min_fill_ratio": 2},
    ):
        with pytest.raises(Exception):
            validate_config_json(config_with(**bad), "c.json")
