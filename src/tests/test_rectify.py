"""Field block rectification onto printed borders (rectifyOnBorder)."""

import json
import random
from pathlib import Path

import pytest

from src.pipeline import OMREngine
from src.synth import augment, default_spec, random_answers, render_sheet

SCAN = dict(
    rotation=1.0,
    perspective=0.005,
    blur=1.0,
    noise=4.0,
    shadow=0.15,
    jpeg_quality=(80, 95),
)


def write_template(directory, template):
    path = Path(directory, "template.json")
    path.write_text(json.dumps(template))
    return path


def wrong_answers(result, truth, labels=None):
    labels = labels or truth["answers"].keys()
    return [k for k in labels if result.responses[k] != truth["answers"][k]]


# --------------------------------------------------------------------------- fixed


# --------------------------------------------------------------------------- rectify


@pytest.fixture(scope="module")
def bordered_spec():
    spec = default_spec(questions=40, roll_digits=6, with_zones=False)
    spec.block_border = 8
    return spec


def bordered_sheet(spec, seed, warps=None, scan=False, border=True):
    rng = random.Random(seed)
    answers = random_answers(spec, rng, blank_rate=0.0)
    original = spec.block_border
    if not border:
        spec.block_border = 0
    try:
        image, truth = render_sheet(
            spec, answers, rng=rng, mark_style="mixed", block_warps=warps
        )
    finally:
        spec.block_border = original
    if scan:
        image, _ = augment(image, rng, **SCAN)
    return image, truth


def rectify_template(spec, block_options=None, register=False):
    template = spec.to_template(pre_processors=None if register else [])
    for name, options in (block_options or {}).items():
        template["fieldBlocks"][name].update(options)
    return template


def block_of(engine, name):
    return next(b for b in engine.template.field_blocks if b.name == name)


@pytest.mark.parametrize("padding", [8, None])
@pytest.mark.parametrize("scan", [False, True])
def test_rectify_corrects_local_warp(tmp_path, bordered_spec, padding, scan):
    warps = {"MCQ_1": (6, -5, 0.8)}
    image, truth = bordered_sheet(bordered_spec, 3, warps, scan=scan)
    labels = bordered_spec.blocks[1].field_labels
    plain = OMREngine(
        write_template(tmp_path, rectify_template(bordered_spec, register=scan))
    ).scan(image)
    assert wrong_answers(plain, truth, labels) or any(
        plain.fields[label]["needs_review"] for label in labels
    )

    options = {"rectifyOnBorder": True}
    if padding is not None:
        options["borderPadding"] = padding
    engine = OMREngine(
        write_template(
            tmp_path,
            rectify_template(bordered_spec, {"MCQ_1": options}, register=scan),
        )
    )
    result = engine.scan(image)
    assert not wrong_answers(result, truth)
    assert block_of(engine, "MCQ_1").last_rectification["ok"]
    for label in labels:
        assert "rectify_failed" not in result.fields[label]["flags"]
        assert result.fields[label]["confidence"] > 0.9
    # Reported bubble positions follow the printed bubbles
    first = result.fields[labels[0]]["bubbles"][0]
    last = result.fields[labels[-1]]["bubbles"][-1]
    assert (first["x"], first["y"]) != (520, 330) and (last["x"], last["y"]) != (
        670,
        1375,
    )


def test_rectify_failure_keeps_page_alignment(tmp_path, bordered_spec):
    image, truth = bordered_sheet(bordered_spec, 4, border=False)
    labels = bordered_spec.blocks[1].field_labels
    plain = OMREngine(write_template(tmp_path, rectify_template(bordered_spec))).scan(
        image
    )
    engine = OMREngine(
        write_template(
            tmp_path,
            rectify_template(bordered_spec, {"MCQ_1": {"rectifyOnBorder": True}}),
        )
    )
    result = engine.scan(image)
    info = block_of(engine, "MCQ_1").last_rectification
    assert not info["ok"] and "not found" in info["reason"]
    assert result.responses == plain.responses
    for label in labels:
        assert "rectify_failed" in result.fields[label]["flags"]
        # Not a review flag by default
        assert (
            result.fields[label]["needs_review"] == plain.fields[label]["needs_review"]
        )
    other = bordered_spec.blocks[2].field_labels[0]
    assert "rectify_failed" not in result.fields[other]["flags"]

    # Opting in sends it to review
    review_flags = list(engine.tuning_config.review_params.review_flags)
    flagged = OMREngine(
        write_template(
            tmp_path,
            rectify_template(bordered_spec, {"MCQ_1": {"rectifyOnBorder": True}}),
        ),
        config_overrides={
            "review_params": {"review_flags": review_flags + ["rectify_failed"]}
        },
    ).scan(image)
    assert all(flagged.fields[label]["needs_review"] for label in labels)


def test_rectify_rejects_implausible_or_worse(tmp_path, bordered_spec):
    # Border moved beyond the search margin
    image, truth = bordered_sheet(bordered_spec, 5, {"MCQ_1": (30, 0, 0)})
    engine = OMREngine(
        write_template(
            tmp_path,
            rectify_template(
                bordered_spec, {"MCQ_1": {"rectifyOnBorder": True, "borderPadding": 8}}
            ),
        )
    )
    engine.scan(image)
    assert not block_of(engine, "MCQ_1").last_rectification["ok"]

    # Wrong padding: the border is found but would stretch the bubbles off their
    # printed positions, so the fit check refuses it
    image, truth = bordered_sheet(bordered_spec, 6)
    engine = OMREngine(
        write_template(
            tmp_path,
            rectify_template(
                bordered_spec, {"MCQ_1": {"rectifyOnBorder": True, "borderPadding": 0}}
            ),
        ),
        config_overrides={"alignment_params": {"rectify_search_px": 12}},
    )
    result = engine.scan(image)
    info = block_of(engine, "MCQ_1").last_rectification
    assert not info["ok"], info
    assert not wrong_answers(result, truth)


def test_rectify_config_default_and_block_override(tmp_path, bordered_spec):
    warps = {"MCQ_1": (-4, 6, -0.6), "MCQ_2": (5, 4, 0.5)}
    image, truth = bordered_sheet(bordered_spec, 7, warps)
    template = rectify_template(bordered_spec, {"Roll": {"rectifyOnBorder": False}})
    engine = OMREngine(
        write_template(tmp_path, template),
        config_overrides={"alignment_params": {"rectify_on_border": True}},
    )
    result = engine.scan(image)
    assert not wrong_answers(result, truth)
    assert block_of(engine, "MCQ_1").last_rectification["ok"]
    assert block_of(engine, "MCQ_2").last_rectification["ok"]
    assert block_of(engine, "Roll").last_rectification is None

    # Offsets belong to one sheet: the next (unwarped) sheet starts clean
    straight, truth2 = bordered_sheet(bordered_spec, 8)
    result2 = engine.scan(straight)
    assert not wrong_answers(result2, truth2)
    mcq = block_of(engine, "MCQ_1")
    assert mcq.last_rectification["max_corner_shift"] < 2
    assert max(abs(b.dx) + abs(b.dy) for s in mcq.traverse_bubbles for b in s) <= 2


def test_rectify_disabled_costs_nothing(tmp_path, bordered_spec, monkeypatch):
    import src.rectify

    def boom(*args, **kwargs):
        raise AssertionError("rectification ran while disabled")

    monkeypatch.setattr(src.rectify, "rectify_field_block", boom)
    image, truth = bordered_sheet(bordered_spec, 9)
    result = OMREngine(write_template(tmp_path, rectify_template(bordered_spec))).scan(
        image
    )
    assert not wrong_answers(result, truth)


def test_template_schema_accepts_new_block_keys(tmp_path, bordered_spec):
    template = rectify_template(
        bordered_spec,
        {
            "MCQ_1": {"rectifyOnBorder": True, "borderPadding": [8, 6]},
            "MCQ_2": {"rectifyOnBorder": False, "borderPadding": 4},
        },
    )
    engine = OMREngine(write_template(tmp_path, template))
    assert block_of(engine, "MCQ_1").border_padding == [8, 6]
    template["fieldBlocks"]["MCQ_1"]["rectifyOnBorder"] = "yes"
    with pytest.raises(Exception):
        OMREngine(write_template(tmp_path, template))


def test_border_never_slides_a_block_by_a_pitch(tmp_path, bordered_spec):
    # A padding off by one bubble pitch would stretch the block over its
    # neighbours: refused and sent to review, page alignment kept
    image, truth = bordered_sheet(bordered_spec, 7)
    template = rectify_template(bordered_spec)
    block = template["fieldBlocks"]["MCQ_1"]
    pitch = block.get("bubblesGap") or 40
    block.update({"rectifyOnBorder": True, "borderPadding": 8 + pitch})
    engine = OMREngine(
        write_template(tmp_path, template),
        config_overrides={
            "alignment_params": {"rectify_search_px": 2 * pitch, "verify_bubble_fit": False}
        },
    )
    result = engine.scan(image)
    info = block_of(engine, "MCQ_1").last_rectification
    assert not info["ok"] and info.get("slide"), info
    label = bordered_spec.blocks[1].field_labels[0]
    assert "border_slide" in result.fields[label]["flags"]
    assert result.fields[label]["needs_review"]
    assert not wrong_answers(result, truth)
