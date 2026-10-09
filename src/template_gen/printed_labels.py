"""
Bubble values read from the print (generator, no labels given).

On the blank page most forms print each bubble's value inside it (A B C D,
0..9). With Tesseract available, a few fields of each block are read one
bubble at a time; a value order is accepted only when the sampled fields
agree, the values are distinct and all of one kind (letters or digits).
Otherwise the caller keeps its guess and marks the block for checking.
"""

from collections import Counter

import cv2
import numpy as np

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DIGITS = "0123456789"
MAX_VALUES = 26
SAMPLED_FIELDS = 3


def available():
    try:
        from src.readers.ocr import tesseract_available

        return tesseract_available()
    except Exception:
        return False


def _read_char(gray, centre, size, whitelist):
    from src.readers.ocr import recognize_text

    w, h = size
    x0, y0 = int(round(centre[0] - w / 2)), int(round(centre[1] - h / 2))
    x1, y1 = int(round(centre[0] + w / 2)), int(round(centre[1] + h / 2))
    crop = gray[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)]
    if crop.size == 0:
        return ""
    # The outline is not a character: keep the inside (an ellipse well
    # inside the printed ring), paper elsewhere
    mx, my = max(1, int(0.12 * crop.shape[1])), max(1, int(0.12 * crop.shape[0]))
    inner = crop[my : crop.shape[0] - my, mx : crop.shape[1] - mx].copy()
    if inner.size == 0:
        return ""
    mask = np.zeros(inner.shape, np.uint8)
    cv2.ellipse(
        mask,
        (inner.shape[1] // 2, inner.shape[0] // 2),
        (max(1, int(0.36 * crop.shape[1])), max(1, int(0.36 * crop.shape[0]))),
        0, 0, 360, 255, -1,
    )
    inner[mask == 0] = 255
    if inner.size == 0 or float(inner.min()) > 160:
        return ""
    scale = 48.0 / max(inner.shape[0], 1)
    big = cv2.resize(inner, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Single characters are flaky in Tesseract: a few renderings and modes,
    # first clean single-character answer wins. Their confidences are low
    # even when right; the agreement between sampled fields (read_values)
    # is the check instead
    for variant in (binary, big):
        padded = cv2.copyMakeBorder(variant, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)
        for psm in (10, 8):
            text, _ = recognize_text(padded, psm=psm, whitelist=whitelist)
            text = (text or "").strip().upper()
            if len(text) == 1:
                return text
    return ""


def read_values(gray, grid, direction):
    """The printed value of each bubble position, or None when unsure."""
    centres = grid.centres()  # (rows, cols, 2)
    if direction == "vertical":
        centres = centres.transpose(1, 0, 2)  # fields are columns
    n_fields, n_values = centres.shape[:2]
    if n_values < 2 or n_values > MAX_VALUES:
        return None
    picks = sorted({0, n_fields // 2, n_fields - 1})[:SAMPLED_FIELDS]
    # Digits and letters are read in separate passes: with one whitelist
    # Tesseract confuses 0/O and 1/I
    for whitelist in (DIGITS, LETTERS) if n_values <= 10 else (LETTERS,):
        values = _vote(gray, centres, picks, n_values, grid.bubble, whitelist)
        if values:
            return values
    return None


def _vote(gray, centres, picks, n_values, size, whitelist):
    reads = [
        [_read_char(gray, centres[f, v], size, whitelist) for v in range(n_values)]
        for f in picks
    ]
    values = []
    for v in range(n_values):
        seen = Counter(r[v] for r in reads if r[v])
        value, count = seen.most_common(1)[0] if seen else ("", 0)
        values.append(value if count >= min(2, len(picks)) else "")
    known = [v for v in values if v]
    if len(set(known)) != len(known):
        return None
    missing = values.count("")
    if missing == 1 and whitelist == DIGITS and n_values == 10:
        # A full digit column with one unread bubble: the absent digit
        values[values.index("")] = (set(DIGITS) - set(known)).pop()
    elif missing:
        return None
    return values


def apply(gray, grids, assignments):
    """Replace guessed values with printed ones where they read cleanly.
    Marks the others values_unverified. Returns the names of read blocks."""
    if not available():
        return []
    read = []
    for k, assignment in assignments.items():
        if not assignment.get("default_named"):
            continue
        try:
            values = read_values(np.asarray(gray), grids[k], assignment["direction"])
        except Exception:
            values = None
        if values and len(values) == len(assignment["values"]):
            assignment["values"] = values
            assignment["values_from_print"] = True
            read.append(k)
        else:
            assignment["values_unverified"] = True
    return read
