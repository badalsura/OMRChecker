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


def read_icr_zone(zone, image, classifier=None):
    crop = zone.crop(image)
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    count = zone.options.get("characterBoxes")
    whitelist = zone.options.get("whitelist")

    if not count:
        return read_free_text(zone, crop, whitelist)

    if classifier is None:
        return read_boxes_without_model(zone, crop, count, whitelist)

    characters, confidences, flags = [], [], []
    for box in split_character_boxes(crop, count):
        cleaned, ink_ratio = clean_box(box)
        if ink_ratio < MIN_INK_RATIO:
            characters.append("")
            confidences.append(1.0)
            continue
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
    return ZoneReadResult(
        zone.name,
        zone.type,
        value,
        confidence,
        flags,
        details={
            "characters": characters,
            "confidences": [round(c, 3) for c in confidences],
        },
    )


def read_boxes_without_model(zone, crop, count, whitelist):
    """Fallback: strip the box borders and read the characters as one text line.

    Always flagged for review, because Tesseract is not trained on handwriting.
    """
    flags = ["no_icr_model"]
    if not ocr.tesseract_available():
        return ZoneReadResult(zone.name, zone.type, "", 0.0, flags + ["engine_unavailable"])
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
        strip += [cv2.copyMakeBorder(b, 0, height - b.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=255), gap]
    line = np.hstack(strip[:-1])
    text, conf = ocr.recognize_text(ocr.prepare_for_ocr(line), psm=7, whitelist=whitelist)
    return ZoneReadResult(zone.name, zone.type, text.replace(" ", ""), conf, flags)


def read_free_text(zone, crop, whitelist):
    """Unboxed handwriting: needs a line-level model; fall back to Tesseract for now."""
    flags = ["no_icr_model"]
    if not ocr.tesseract_available():
        return ZoneReadResult(zone.name, zone.type, "", 0.0, flags + ["engine_unavailable"])
    text, conf = ocr.recognize_text(ocr.prepare_for_ocr(crop), psm=7, whitelist=whitelist)
    return ZoneReadResult(zone.name, zone.type, text, conf, flags)
