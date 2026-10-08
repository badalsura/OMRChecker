"""
Zone candidates: barcodes/QR codes, boxed handwriting (ICR) and variable text (OCR).

- Barcodes and 2D codes are located by ZXing-C++ (src/readers/barcode.py) on
  several registered sheets; boxes of the same symbol type are merged across
  sheets. Linear barcodes report a thin scan line, so their box is grown
  vertically while image rows keep the bar/space transition count.
- Character boxes are hole contours of the adaptive-thresholded reference
  that are near-rectangular, similar in size and touch their neighbours; a
  run of two or more becomes an ICR zone with characterBoxes = run length.
- Printed or handwritten text that changes from sheet to sheet is found from
  the per-pixel disagreement with the blank page (static instructions do
  not vary and are ignored); text-line shaped regions outside every other
  element become OCR candidates.
"""

from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from src.readers.barcode import read_all_symbols
from src.template_gen.assignment import boxes_overlap, odd

QR_FORMATS = {"QRCode", "MicroQRCode", "RMQRCode", "QRCodeModel1", "QRCodeModel2"}
TWO_D_FORMATS = QR_FORMATS | {
    "DataMatrix",
    "Aztec",
    "PDF417",
    "CompactPDF417",
    "MicroPDF417",
    "MaxiCode",
}


def format_name(fmt):
    """'Code 128' -> 'Code128' (the spelling zone options.formats expect)."""
    try:
        import zxingcpp

        name = zxingcpp.barcode_format_from_str(fmt).name
        if name != "NONE":
            return name
    except Exception:
        pass
    # zxing-cpp 2.2 doesn't parse the spaced spellings
    return fmt.replace(" ", "").replace("-", "")


def _iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1 = min(a[0] + a[2], b[0] + b[2])
    y1 = min(a[1] + a[3], b[1] + b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _grow_linear_barcode(gray, box):
    """Extend a 1D barcode's scan-line box to the full bar height."""
    x, y, w, h = box
    page_h, page_w = gray.shape[:2]
    x0, x1 = max(int(x), 0), min(int(x + w), page_w)
    if x1 - x0 < 10:
        return box
    region = gray[:, x0:x1]
    _, binary = cv2.threshold(region, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    transitions = np.abs(np.diff(binary.astype(np.int16), axis=1)).sum(axis=1)
    centre = int(min(max(y + h / 2, 0), page_h - 1))
    reference = transitions[centre]
    if reference < 6:
        return box
    top = centre
    while top > 0 and transitions[top - 1] >= 0.5 * reference:
        top -= 1
    bottom = centre
    while bottom < page_h - 1 and transitions[bottom + 1] >= 0.5 * reference:
        bottom += 1
    top, bottom = min(top, int(y)), max(bottom, int(y + h))
    return [x, top, w, bottom - top + 1]


def detect_symbol_zones(images, page_size, max_sheets=6, sheet_ids=None, workers=4):
    """Returns a list of {type, formats, box, texts: [[sheet, text]], support}."""
    page_w, page_h = page_size
    sheet_ids = list(range(len(images))) if sheet_ids is None else list(sheet_ids)
    pairs = list(zip(sheet_ids, images))[:max_sheets]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        found = list(pool.map(lambda pair: read_all_symbols(pair[1]), pairs))
    clusters = []
    for (sheet, gray), symbols in zip(pairs, found):
        for symbol in symbols:
            fmt = format_name(symbol["format"])
            box = [float(v) for v in symbol["box"]]
            if fmt not in TWO_D_FORMATS:
                box = _grow_linear_barcode(gray, box)
            kind = "qrcode" if fmt in QR_FORMATS else "barcode"
            for cluster in clusters:
                if cluster["type"] == kind and _iou(cluster["box"], box) > 0.2:
                    bx = cluster["box"]
                    x0, y0 = min(bx[0], box[0]), min(bx[1], box[1])
                    x1 = max(bx[0] + bx[2], box[0] + box[2])
                    y1 = max(bx[1] + bx[3], box[1] + box[3])
                    cluster["box"] = [x0, y0, x1 - x0, y1 - y0]
                    cluster["formats"].add(fmt)
                    cluster["texts"].append([sheet, symbol["text"]])
                    break
            else:
                clusters.append(
                    {
                        "type": kind,
                        "box": box,
                        "formats": {fmt},
                        "texts": [[sheet, symbol["text"]]],
                    }
                )
    zones = []
    for cluster in clusters:
        x, y, w, h = cluster["box"]
        pad = max(10.0, 0.08 * max(w, h))
        x0, y0 = max(0, int(x - pad)), max(0, int(y - pad))
        x1, y1 = min(page_w - 1, int(x + w + pad)), min(page_h - 1, int(y + h + pad))
        zones.append(
            {
                "type": cluster["type"],
                "formats": sorted(cluster["formats"]),
                "box": [x0, y0, x1 - x0, y1 - y0],
                "texts": cluster["texts"],
                "support": len(cluster["texts"]),
            }
        )
    return zones


def detect_character_boxes(gray, page_size, exclude_boxes=()):
    """Rows of equal touching rectangles -> ICR zone candidates."""
    page_w = page_size[0]
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        odd(page_w / 40),
        8,
    )
    contours, hierarchy = cv2.findContours(
        binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    if hierarchy is None:
        return []
    min_side, max_side = 0.012 * page_w, 0.12 * page_w
    boxes = []
    for contour, (_, _, _, parent) in zip(contours, hierarchy[0]):
        if parent < 0:  # holes only
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if not (min_side <= w <= max_side and min_side <= h <= max_side):
            continue
        if not 0.35 <= w / h <= 1.6:
            continue
        if cv2.contourArea(contour) / float(w * h) < 0.85:
            continue
        if any(boxes_overlap([x, y, w, h], ex) for ex in exclude_boxes):
            continue
        boxes.append([x, y, w, h])
    boxes.sort(key=lambda b: (b[0], b[1]))  # runs grow rightwards from the left
    used, rows = set(), []
    for i, box in enumerate(boxes):
        if i in used:
            continue
        run = [box]
        used.add(i)
        while True:
            last = run[-1]
            nxt = None
            for j, other in enumerate(boxes):
                if j in used:
                    continue
                gap = other[0] - (last[0] + last[2])
                if (
                    -0.05 * last[2] <= gap <= 0.3 * last[2]
                    and abs(other[1] - last[1]) < 0.2 * last[3]
                    and abs(other[2] - last[2]) < 0.15 * last[2]
                    and abs(other[3] - last[3]) < 0.15 * last[3]
                ):
                    nxt = j
                    break
            if nxt is None:
                break
            used.add(nxt)
            run.append(boxes[nxt])
        if len(run) >= 2:
            rows.append(run)
    zones = []
    for run in rows:
        x0 = min(b[0] for b in run)
        y0 = min(b[1] for b in run)
        x1 = max(b[0] + b[2] for b in run)
        y1 = max(b[1] + b[3] for b in run)
        # holes sit inside the printed lines; include the border
        border = max(1, int(round(np.median([b[3] for b in run]) * 0.03)))
        zones.append(
            {
                "type": "icr",
                "box": [
                    x0 - border,
                    y0 - border,
                    x1 - x0 + 2 * border,
                    y1 - y0 + 2 * border,
                ],
                "character_boxes": len(run),
            }
        )
    return zones


def detect_variable_text(
    images, reference, page_size, exclude_boxes=(), min_rate=0.25, max_zones=8
):
    """Text-line shaped regions whose ink changes between sheets -> OCR candidates."""
    if len(images) < 5:
        return []  # too few sheets to tell variable from static content
    page_w, page_h = page_size
    votes = np.zeros(reference.shape[:2], np.uint16)
    # New ink only: darker than the darkest blank-form pixel nearby, so printed
    # headings that sit a pixel or two off on some sheets don't count
    nearby_ink = cv2.erode(reference, np.ones((5, 5), np.uint8))
    for image in images:
        diff = cv2.subtract(nearby_ink, image)
        votes += (diff > 60).astype(np.uint16)
    changing = (votes >= max(2, min_rate * len(images))).astype(np.uint8) * 255
    changing = cv2.morphologyEx(changing, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    # Page borders differ with every capture (paper edge, background): ignore
    margin = int(0.02 * max(page_w, page_h))
    changing[:margin], changing[-margin:] = 0, 0
    changing[:, :margin], changing[:, -margin:] = 0, 0
    for x, y, w, h in exclude_boxes:
        x0, y0 = max(int(x), 0), max(int(y), 0)
        changing[y0 : int(y + h) + 1, x0 : int(x + w) + 1] = 0
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (odd(page_w * 0.025), odd(page_h * 0.004))
    )
    merged = cv2.dilate(changing, kernel)
    count, _, stats, _ = cv2.connectedComponentsWithStats(merged, 8)
    _, ink = cv2.threshold(reference, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    for x, y, w, h in exclude_boxes:
        x0, y0 = max(int(x), 0), max(int(y), 0)
        ink[y0 : int(y + h) + 1, x0 : int(x + w) + 1] = 0
    zones = []
    for k in range(1, count):
        x, y, w, h, area = stats[k]
        if not (0.008 * page_h <= h <= 0.08 * page_h) or w < 1.2 * h:
            continue
        # Grow sideways over static text on the same line (e.g. a printed prefix)
        columns = ink[y : y + h].any(axis=0)
        max_gap = int(0.8 * h)
        left, gap = x, 0
        while left > 0 and gap <= max_gap:
            left -= 1
            gap = 0 if columns[left] else gap + 1
        x_start = left + gap
        right, gap = x + w - 1, 0
        while right < page_w - 1 and gap <= max_gap:
            right += 1
            gap = 0 if columns[right] else gap + 1
        x, w = x_start, right - gap - x_start + 1
        pad_x, pad_y = int(0.3 * h), int(0.25 * h)
        x0, y0 = max(0, x - pad_x), max(0, y - pad_y)
        x1, y1 = min(page_w - 1, x + w + pad_x), min(page_h - 1, y + h + pad_y)
        zones.append(
            {
                "type": "ocr",
                "box": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
                "changing_pixels": int(area),
            }
        )
    # Only the strongest few: these are suggestions for a person to confirm
    zones.sort(key=lambda z: -z["changing_pixels"])
    return zones[:max_zones]
