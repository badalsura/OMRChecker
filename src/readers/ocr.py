"""
Printed-text OCR using Tesseract's LSTM engine (OEM 1).

Tesseract is the standard open-source OCR engine; the zone is upscaled to a
text height Tesseract handles well and binarised before recognition. Word
confidences reported by Tesseract are averaged into the zone confidence.
"""
import shutil

import cv2
import numpy as np

from src.readers.base import ZoneReadResult

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None

TARGET_TEXT_HEIGHT = 48


def tesseract_available():
    return pytesseract is not None and shutil.which("tesseract") is not None


def prepare_for_ocr(crop, expected_lines=1):
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h = crop.shape[0]
    line_height = max(h / max(expected_lines, 1), 1)
    scale = TARGET_TEXT_HEIGHT / line_height
    if scale > 1.2:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    crop = cv2.GaussianBlur(crop, (3, 3), 0)
    _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Tesseract expects dark text on a light background with a margin
    return cv2.copyMakeBorder(binary, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=255)


def tesseract_config(psm, whitelist=None):
    config = f"--oem 1 --psm {psm}"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    return config


def recognize_text(image, psm=7, whitelist=None, lang="eng"):
    """Return (text, confidence in [0, 1]) for an already-prepared image."""
    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config=tesseract_config(psm, whitelist),
        output_type=pytesseract.Output.DICT,
    )
    words, confidences = [], []
    for text, conf in zip(data["text"], data["conf"]):
        conf = float(conf)
        if text.strip() and conf >= 0:
            words.append(text.strip())
            confidences.append(conf / 100.0)
    if not words:
        return "", 0.0
    return " ".join(words), float(np.mean(confidences))


def read_ocr_zone(zone, image):
    if not tesseract_available():
        return ZoneReadResult(zone.name, zone.type, "", 0.0, ["engine_unavailable"])
    options = zone.options
    prepared = prepare_for_ocr(zone.crop(image, padding=2))
    text, confidence = recognize_text(
        prepared,
        psm=options.get("psm", 7),
        whitelist=options.get("whitelist"),
        lang=options.get("lang", "eng"),
    )
    flags = [] if text else ["empty"]
    return ZoneReadResult(zone.name, zone.type, text, confidence, flags)
