"""
Handwritten character recognition (ICR).

Forms that collect handwriting almost always use one box per character, which
turns ICR into isolated-character classification, the approach production ICR
systems use. Each box is cleaned of its printed border, checked for ink, and
classified by a learned crop classifier (src/ml/classifiers.py) whose labels are
the allowed characters.

No trained ICR weights ship with the repository. Without a model the reader
uses PaddleOCR's PP-OCRv5 recognition model (trained on printed and handwritten
text) when its ONNX files are installed; zones it is unsure of go to review
through the usual minConfidence check. Without PaddleOCR it falls back to
Tesseract and flags every result for review, because Tesseract is not built
for handwriting.
"""

import cv2
import numpy as np

from src.logger import logger
from src.readers import ocr
from src.readers.base import ZoneReadResult

BLANK_LABEL = "<blank>"
# A box is blank if less than this fraction of its interior is ink
MIN_INK_RATIO = 0.015


# Printed box borders: searched for within this fraction of a box width around
# each equal-split edge; a border is a vertical run at least this fraction of
# the zone height
BORDER_SEARCH = 0.3
BORDER_MIN_HEIGHT = 0.7
# Grid lines are removed only within this outer fraction of a cleaned box
EDGE_BAND = 0.2


def split_character_boxes(crop, count):
    """
    The zone's character boxes, cut at the printed box borders found on this
    sheet. Each inner edge starts at its equal-split position and moves to the
    nearest printed vertical line within BORDER_SEARCH of a box width, so a
    zone a few pixels off the grid (or per-sheet registration wobble) does not
    put one box's border inside its neighbour. No line found: equal split.
    """
    h, w = crop.shape[:2]
    edges = np.linspace(0, w, count + 1).astype(int)
    if count > 1 and h >= 8:
        edges[1:-1] = [
            border_near(crop, int(edge), w / float(count)) for edge in edges[1:-1]
        ]
    return [crop[:, edges[i] : edges[i + 1]] for i in range(count)]


def border_near(crop, edge, box_width):
    grey = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h, w = grey.shape[:2]
    reach = max(int(box_width * BORDER_SEARCH), 1)
    lo, hi = max(edge - reach, 1), min(edge + reach + 1, w - 1)
    if hi <= lo:
        return edge
    strip = grey[:, lo:hi]
    ink = cv2.adaptiveThreshold(
        strip, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 8
    )
    tall = max(int(h * BORDER_MIN_HEIGHT), 3)
    lines = cv2.morphologyEx(
        ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, tall))
    )
    column = lines.sum(axis=0) / 255.0
    if column.max() < tall:
        return edge
    # Centre of the strongest columns: a doubled printed line counts as one
    hot = np.flatnonzero(column >= 0.5 * column.max())
    return int(round(lo + hot.mean()))


def clean_box(box):
    """Remove the printed box border and return a tight crop around the ink."""
    h, w = box.shape[:2]
    margin_y, margin_x = max(int(h * 0.12), 1), max(int(w * 0.12), 1)
    inner = box[margin_y : h - margin_y, margin_x : w - margin_x]
    if inner.size == 0:
        return inner, 0.0
    _, ink = cv2.threshold(inner, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ink = cv2.subtract(ink, grid_lines(ink))
    ink_ratio = float(np.count_nonzero(ink)) / float(ink.size)
    # Otsu on a blank box splits paper noise; require real contrast as well
    if inner.max() - inner.min() < 40:
        ink_ratio = 0.0
    return 255 - ink, ink_ratio


def grid_lines(ink):
    """
    Leftovers of the printed grid: straight runs over half the box long, but
    only in the outer EDGE_BAND of the box where the borders sit. A run that
    long in the middle is the stroke of a 1, 4, 7 or 9, or the side of a 0,
    and removing it there erased the digit itself.
    """
    h, w = ink.shape[:2]
    found = np.zeros_like(ink)
    for horizontal in (True, False):
        kernel = (max(w // 2, 1), 1) if horizontal else (1, max(h // 2, 1))
        lines = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, kernel)
        )
        band = np.zeros_like(lines)
        if horizontal:
            edge = max(int(h * EDGE_BAND), 2)
            band[:edge], band[-edge:] = 255, 255
        else:
            edge = max(int(w * EDGE_BAND), 2)
            band[:, :edge], band[:, -edge:] = 255, 255
        found = cv2.bitwise_or(found, cv2.bitwise_and(lines, band))
    return found


def read_icr_zone(zone, image, classifier=None, ocr_params=None):
    """
    Read a handwriting zone. options.direction turns the crop first (see
    src/readers/text_reader.py); "auto" keeps the best valid read of the four.
    """
    from src.readers import text_reader

    crop = zone.crop(image)
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    settings = text_reader.ocr_settings(ocr_params)
    directions = text_reader.zone_directions(zone)
    best, best_key = None, None
    for direction in directions:
        result = read_icr_crop(
            zone, text_reader.rotate_crop(crop, direction), classifier, settings
        )
        if "direction" in zone.options:
            result.details["direction"] = direction
        valid = text_reader.is_valid(result.value, zone)
        key = (valid, bool(result.value), result.confidence)
        if best is None or key > best_key:
            best, best_key = result, key
    return best


def paddle_second_reader(settings, zone):
    """PaddleOCR recognition as a second reader for boxed handwriting, or None."""
    from src.readers import text_reader

    # "auto" is off: PaddleOCR reading single cleaned boxes disagreed with a
    # good box classifier on 915 of 932 TERI roll numbers, so it sent nearly
    # every zone to review. "paddle" still turns it on.
    choice = settings.get("icr_second_reader", "auto")
    if choice != "paddle":
        return None
    try:
        engine = text_reader.paddle_engine(settings, zone=zone)
        return engine if engine.available else None
    except Exception:  # pragma: no cover - optional engine
        return None


def compare_with_second_reader(engine, boxes, characters, whitelist):
    """Per-box PaddleOCR reads and the box indices where the two readers differ."""
    allowed = set(whitelist) if whitelist else None
    second, differ = [], []
    for index, (box, first) in enumerate(zip(boxes, characters)):
        if box is None:
            second.append("")
            continue
        padded = cv2.copyMakeBorder(box, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=255)
        text, _ = engine.recognize_line(padded, allowed)
        text = text.strip()[:1]
        second.append(text)
        if text != first:
            differ.append(index)
    return second, differ


def read_icr_crop(zone, crop, classifier=None, settings=None):
    settings = settings or {}
    count = zone.options.get("characterBoxes")
    whitelist = zone.options.get("whitelist")

    if icr_engine_choice(settings, zone) == "vlm":
        engine = vlm_icr_engine(settings, zone)
        if engine is not None:
            return read_with_vlm(zone, crop, count, whitelist, engine, classifier)
        # Read the usual way (PaddleOCR, the ICR model or Tesseract) instead
        result = read_icr_crop(zone, crop, classifier, {**settings, "_no_vlm": True})
        result.details["vlm_unavailable"] = vlm_unavailable_reason(settings, zone)
        return result

    if classifier is None or not count:
        paddle = paddle_icr_engine(settings, zone)
        if paddle is not None:
            return read_with_paddle(zone, crop, count, whitelist, paddle)
    if not count:
        return read_free_text(zone, crop, whitelist)

    if classifier is None:
        return read_boxes_without_model(zone, crop, count, whitelist)

    characters, confidences, flags = [], [], []
    inked_boxes = []
    for box in split_character_boxes(crop, count):
        cleaned, ink_ratio = clean_box(box)
        if ink_ratio < MIN_INK_RATIO:
            characters.append("")
            confidences.append(1.0)
            inked_boxes.append(None)
            continue
        inked_boxes.append(cleaned)
        if classifier is not None:
            probs = classifier.predict_proba([cleaned])[0]
            if whitelist:
                allowed = [
                    i
                    for i, label in enumerate(classifier.labels)
                    if label in whitelist or label == BLANK_LABEL
                ]
                mask = np.zeros_like(probs)
                mask[allowed] = 1
                probs = probs * mask
                probs = probs / max(probs.sum(), 1e-9)
            best = int(np.argmax(probs))
            label = classifier.labels[best]
            characters.append("" if label == BLANK_LABEL else label)
            confidences.append(float(probs[best]))

    value = "".join(characters).strip()
    confidence = float(min(confidences)) if confidences else 0.0
    details = {
        "characters": characters,
        "confidences": [round(c, 3) for c in confidences],
    }
    second = paddle_second_reader(settings, zone)
    if second is not None:
        try:
            read, differ = compare_with_second_reader(
                second, inked_boxes, characters, whitelist
            )
            details["second_reader"] = {"engine": "paddle", "characters": read}
            if differ:
                details["second_reader"]["differ"] = differ
                if settings.get("disagree_to_review", True):
                    flags.append("engine_disagree")
        except Exception as error:  # the second reader must not lose the zone
            details["second_reader"] = {"engine": "paddle", "error": str(error)}
    return ZoneReadResult(zone.name, zone.type, value, confidence, flags, details=details)


def paddle_icr_engine(settings, zone):
    """PaddleOCR as the handwriting reader when no ICR model is loaded, or None."""
    from src.readers import text_reader

    if icr_engine_choice(settings, zone) not in ("auto", "paddle"):
        return None
    try:
        engine = text_reader.paddle_engine(settings, zone=zone)
        return engine if engine.available else None
    except Exception:  # pragma: no cover - optional engine
        return None


def icr_engine_choice(settings, zone):
    """The zone's icrEngine option, else ocr_params.icr_engine."""
    choice = zone.options.get("icrEngine") if zone is not None else None
    if choice in (None, "default"):
        choice = settings.get("icr_engine", "auto")
    if choice == "vlm" and settings.get("_no_vlm"):
        return "auto"
    return choice


def vlm_icr_engine(settings, zone):
    """The vision-language model reader (src/readers/vlm.py), or None."""
    from src.readers import vlm

    try:
        engine = vlm.get_engine(settings, zone)
        return engine if engine.available else None
    except Exception as error:  # pragma: no cover - optional engine
        logger.warning(f"VLM reader unavailable: {error}")
        return None


def vlm_unavailable_reason(settings, zone):
    from src.readers import vlm

    try:
        return vlm.get_engine(settings, zone).unavailable_reason
    except Exception as error:  # pragma: no cover
        return str(error)


def inked_box_count(crop, count):
    return sum(
        1
        for box in split_character_boxes(crop, count)
        if clean_box(box)[1] >= MIN_INK_RATIO
    )


def read_with_vlm(zone, crop, count, whitelist, engine, classifier=None):
    """
    Handwriting through a vision-language model, which reads the raw zone crop
    (box lines and all: the models read worse on cleaned boxes). A VLM gives
    no confidence, so the box count is the check: a boxed zone whose read has
    a different number of characters than boxes with ink goes to review
    (icr_alignment). With an ICR model loaded, it reads the boxes as a second
    opinion and a disagreement is flagged engine_disagree.
    """
    from src.readers import vlm

    try:
        raw = engine.read(crop, vlm.prompt_for(engine.model, whitelist, count))
    except Exception as error:
        return ZoneReadResult(
            zone.name, zone.type, "", 0.0, ["read_error"],
            details={"engine": "vlm", "model": engine.model, "error": str(error)},
            engine="vlm",
        )
    text = "".join(ch for ch in raw if not ch.isspace())
    if whitelist:
        text = "".join(ch for ch in text if ch in whitelist)
    flags, details = [], {"engine": "vlm", "model": engine.model, "raw": raw.strip()}
    confidence = 1.0
    if count:
        inked = inked_box_count(crop, count)
        details["inked_boxes"] = inked
        if not inked and not text:
            return ZoneReadResult(
                zone.name, zone.type, "", 1.0, ["empty"], details=details, engine="vlm"
            )
        if len(text) != inked:
            flags.append("icr_alignment")
            confidence = 0.0
        if classifier is not None and len(text) == inked:
            second = classify_boxes(crop, count, classifier, whitelist)
            details["second_reader"] = {"engine": "icr_model", "characters": second}
            if "".join(second) != text:
                flags.append("engine_disagree")
    elif not text:
        flags.append("empty")
    return ZoneReadResult(
        zone.name, zone.type, text, confidence, flags, details=details, engine="vlm"
    )


def classify_boxes(crop, count, classifier, whitelist):
    """The ICR model's character for each inked box (blank boxes left out)."""
    characters = []
    for box in split_character_boxes(crop, count):
        cleaned, ink_ratio = clean_box(box)
        if ink_ratio < MIN_INK_RATIO:
            continue
        probs = classifier.predict_proba([cleaned])[0]
        if whitelist:
            mask = np.array(
                [label in whitelist or label == BLANK_LABEL for label in classifier.labels],
                dtype=float,
            )
            probs = probs * mask
        label = classifier.labels[int(np.argmax(probs))]
        characters.append("" if label == BLANK_LABEL else label)
    return characters


def boxes_strip(boxes, spans=None):
    """Cleaned character boxes side by side on one white line. spans: optional
    list that receives each box's (start, end) x range on the line."""
    height = max(b.shape[0] for b in boxes)
    gap = np.full((height, max(height // 3, 4)), 255, np.uint8)
    strip, x, ranges = [], 0, []
    for b in boxes:
        strip += [
            cv2.copyMakeBorder(
                b, 0, height - b.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=255
            ),
            gap,
        ]
        ranges.append((x, x + b.shape[1]))
        x += b.shape[1] + gap.shape[1]
    if spans is not None:
        spans[:] = ranges
    return np.hstack(strip[:-1])


PAD = 6


def pad_white(image, size=PAD):
    return cv2.copyMakeBorder(image, size, size, size, size, cv2.BORDER_CONSTANT, value=255)


def read_with_paddle(zone, crop, count, whitelist, engine):
    """
    Handwriting through PaddleOCR. Boxed zones are read as one line of the
    inked boxes (context helps the recogniser); if that line does not give one
    character per inked box, each box is read on its own.
    """
    allowed = set(whitelist) if whitelist else None
    if not count:
        text, chars = engine.recognize_line(pad_white(crop), allowed)
        confidence = float(min(chars)) if chars else 0.0
        return ZoneReadResult(
            zone.name, zone.type, text, confidence, [],
            details={"engine": "paddle", "confidences": [round(c, 3) for c in chars]},
            engine="paddle",
        )
    boxes = []
    for box in split_character_boxes(crop, count):
        cleaned, ink_ratio = clean_box(box)
        boxes.append(cleaned if cleaned.size and ink_ratio >= MIN_INK_RATIO else None)
    inked = [b for b in boxes if b is not None]
    if not inked:
        return ZoneReadResult(
            zone.name, zone.type, "", 1.0, ["empty"],
            details={"engine": "paddle"}, engine="paddle",
        )
    spans, where = [], []
    line = pad_white(boxes_strip(inked, spans))
    text, chars = engine.recognize_line(line, allowed, positions=where)
    # Each character goes to the box it was read over. A line that does not
    # give exactly one character per box keeps the boxes it does fit; only the
    # others are read on their own (a lone character is hard for a line
    # recogniser, so throwing the whole line away lost good reads)
    per_box = [[] for _ in inked]
    for char, prob, at in zip(text, chars, where):
        if char == " ":
            continue
        x = at * line.shape[1] - PAD
        nearest = min(
            range(len(spans)),
            key=lambda i: (
                0
                if spans[i][0] <= x < spans[i][1]
                else min(abs(x - spans[i][0]), abs(x - spans[i][1]))
            ),
        )
        per_box[nearest].append((char, prob))
    mode, flags, pairs = "line", [], []
    for box, found in zip(inked, per_box):
        if len(found) == 1:
            pairs.append(found[0])
            continue
        mode = "aligned"
        if found:  # two characters over one box: keep the surer
            pairs.append(max(found, key=lambda pair: pair[1]))
            continue
        alone, probs = engine.recognize_line(pad_white(box), allowed)
        alone = alone.replace(" ", "")
        if alone:
            pairs.append((alone[:1], float(np.mean(probs)) if probs else 0.0))
        else:  # ink but no character: never drop it from the value silently
            pairs.append(("", 0.0))
            flags.append("unread_box")
    if mode == "aligned":  # the line did not fit the boxes: a person checks it
        flags.append("icr_alignment")
    found = iter(pairs)
    characters, confidences = [], []
    for box in boxes:
        if box is None:
            characters.append("")
            confidences.append(1.0)
        else:
            char, prob = next(found)
            characters.append(char)
            confidences.append(prob)
    value = "".join(characters).strip()
    return ZoneReadResult(
        zone.name,
        zone.type,
        value,
        float(min(confidences)),
        sorted(set(flags)),
        details={
            "engine": "paddle",
            "mode": mode,
            "characters": characters,
            "confidences": [round(c, 3) for c in confidences],
        },
        engine="paddle",
    )


def read_boxes_without_model(zone, crop, count, whitelist):
    """Fallback: strip the box borders and read the characters as one text line.

    Always flagged for review, because Tesseract is not trained on handwriting.
    """
    flags = ["no_icr_model"]
    if not ocr.tesseract_available():
        return ZoneReadResult(
            zone.name, zone.type, "", 0.0, flags + ["engine_unavailable"]
        )
    cleaned_boxes = []
    for box in split_character_boxes(crop, count):
        cleaned, ink_ratio = clean_box(box)
        if cleaned.size and ink_ratio >= MIN_INK_RATIO:
            cleaned_boxes.append(cleaned)
    if not cleaned_boxes:
        return ZoneReadResult(zone.name, zone.type, "", 1.0, flags + ["empty"])
    height = max(b.shape[0] for b in cleaned_boxes)
    gap = np.full((height, max(height // 3, 4)), 255, np.uint8)
    strip = []
    for b in cleaned_boxes:
        strip += [
            cv2.copyMakeBorder(
                b, 0, height - b.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=255
            ),
            gap,
        ]
    line = np.hstack(strip[:-1])
    text, conf = ocr.recognize_text(
        ocr.prepare_for_ocr(line), psm=7, whitelist=whitelist
    )
    return ZoneReadResult(zone.name, zone.type, text.replace(" ", ""), conf, flags)


def read_free_text(zone, crop, whitelist):
    """Unboxed handwriting: needs a line-level model; fall back to Tesseract for now."""
    flags = ["no_icr_model"]
    if not ocr.tesseract_available():
        return ZoneReadResult(
            zone.name, zone.type, "", 0.0, flags + ["engine_unavailable"]
        )
    text, conf = ocr.recognize_text(
        ocr.prepare_for_ocr(crop), psm=7, whitelist=whitelist
    )
    return ZoneReadResult(zone.name, zone.type, text, conf, flags)
