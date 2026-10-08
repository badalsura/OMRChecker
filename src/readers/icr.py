"""
Handwritten character recognition (ICR).

Forms that collect handwriting almost always use one box per character, which
turns ICR into isolated-character classification, the approach production ICR
systems use. Each box is cleaned of its printed border, checked for ink, and
classified by a learned crop classifier (src/ml/classifiers.py) whose labels are
the allowed characters.

No trained ICR weights ship with the repository. Without a model the reader falls
back to Tesseract per character and flags every result for review, because
Tesseract is not built for handwriting.
"""

import cv2
import numpy as np

from src.readers import ocr
from src.readers.base import ZoneReadResult

BLANK_LABEL = "<blank>"
# A box is blank if less than this fraction of its interior is ink
MIN_INK_RATIO = 0.015


def split_character_boxes(crop, count):
    h, w = crop.shape[:2]
    edges = np.linspace(0, w, count + 1).astype(int)
    return [crop[:, edges[i] : edges[i + 1]] for i in range(count)]


def clean_box(box):
    """Remove the printed box border and return a tight crop around the ink."""
    h, w = box.shape[:2]
    margin_y, margin_x = max(int(h * 0.12), 1), max(int(w * 0.12), 1)
    inner = box[margin_y : h - margin_y, margin_x : w - margin_x]
    if inner.size == 0:
        return inner, 0.0
    _, ink = cv2.threshold(inner, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Remove long straight strokes left over from the printed grid
    for kernel in ((max(inner.shape[1] // 2, 1), 1), (1, max(inner.shape[0] // 2, 1))):
        lines = cv2.morphologyEx(
            ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, kernel)
        )
        ink = cv2.subtract(ink, lines)
    ink_ratio = float(np.count_nonzero(ink)) / float(ink.size)
    # Otsu on a blank box splits paper noise; require real contrast as well
    if inner.max() - inner.min() < 40:
        ink_ratio = 0.0
    return 255 - ink, ink_ratio


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

    choice = settings.get("icr_second_reader", "auto")
    if choice not in ("auto", "paddle"):
        return None
    try:
        engine = text_reader.paddle_engine(settings, zone.options.get("lang"))
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
