"""
Scanner imprint numbers: the short code a scanner's imprinter prints on each
sheet as it feeds (for example "S100001"), in dot-matrix characters from a
small set such as "S0123456789".

The print lands wherever the paper was when the imprinter fired, often
outside the timing marks, so an "imprint" zone is a search strip rather than
a box: the reader finds the line of dots inside it, cuts it out and reads it
with a small CTC line recogniser (ONNX) trained on that scanner's print.

Optional module: nothing loads until a template has an imprint zone, and the
model is a separate file (packaging/models/imprint/<name>.onnx plus a .json
sidecar, see src/ml/train_imprint.py). Without a model the zone is read by
Tesseract and always goes to review (no_imprint_model).

Zone options:
    model          model name in packaging/models/imprint (default "imprint")
                   or a path to the .onnx file
    direction      how the print reads in the template (as for OCR zones);
                   default: the model's own
    charHeight     character height in template px (default: found)
    whitelist      characters allowed (default: the model's charset)
    pattern        regular expression the value must match, e.g. "S\\d{6}"
    minConfidence  as for other zones
"""

import json
import os
import re
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

from src.logger import logger
from src.readers.base import ZoneReadResult

DEFAULT_MODEL = "imprint"
DEFAULT_CHARSET = "S0123456789"
INPUT_HEIGHT = 32
# Ink: local mean threshold; a dot must be this much darker than its
# surroundings (TERI's grey dots are 50-125 levels below the paper)
INK_OFFSET = 35
# Character heights tried when the zone gives none (template px)
CHAR_HEIGHTS = (14, 18, 22, 28, 36, 46)
# Line candidates read before giving up (best dot count first)
CANDIDATES = 3

_MODELS = {}
_LOCK = threading.Lock()


# ------------------------------------------------------------------ locating
def line_candidates(grey, char_height=None, limit=CANDIDATES):
    """
    Boxes (x, y, w, h) in a horizontal strip that look like a line of
    dot-matrix print, most dots first. With the character height h:

    1. ink = adaptive mean threshold (block ~1.4 h, offset INK_OFFSET);
    2. keep only dot-sized components (area <= (0.35 h)^2), or open
       character-sized ones (dots run together): timing marks, printed lines
       and the scanner's edge shadow drop out;
    3. join the dots of a line (closing 1.4 h along the line, 0.4 h across);
    4. candidates are 0.64-1.8 h thick and 2-25 h long, ranked by the dot
       pixels they hold.

    Without charHeight every height in CHAR_HEIGHTS is tried and the boxes
    with the most dots win.
    """
    heights = [float(char_height)] if char_height else CHAR_HEIGHTS
    found = []
    for h in heights:
        block = max(int(1.4 * h) | 1, 15)
        ink = cv2.adaptiveThreshold(
            grey, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, INK_OFFSET
        )
        count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
        area = stats[:, cv2.CC_STAT_AREA]
        bw, bh = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]
        # Dots, or dots run together into one open character (print heavier
        # than TERI's): no bigger than a character and far from solid
        glyph = (bw <= 1.3 * h) & (bh <= 1.3 * h) & (area < 0.5 * bw * bh)
        dots_ok = (area <= max((0.35 * h) ** 2, 4)) | glyph
        dots_ok[0] = False
        dots = (dots_ok[labels] * 255).astype(np.uint8)
        kernel = np.ones((max(int(0.4 * h), 3), max(int(1.4 * h), 5)), np.uint8)
        joined = cv2.morphologyEx(dots, cv2.MORPH_CLOSE, kernel)
        n, _, boxes, _ = cv2.connectedComponentsWithStats(joined, 8)
        for i in range(1, n):
            x, y, w, bh = (int(v) for v in boxes[i][:4])
            if not (0.64 * h <= bh <= 1.8 * h and 2 * h <= w <= 25 * h):
                continue
            weight = int(np.count_nonzero(dots[y : y + bh, x : x + w]))
            found.append((weight, (x, y, w, bh)))
    found.sort(key=lambda item: -item[0])
    boxes = []
    for _, box in found:  # the same line found at several heights: keep one
        if all(overlap(box, other) < 0.5 for other in boxes):
            boxes.append(box)
        if len(boxes) == limit:
            break
    return boxes


def overlap(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(x1 - x0, 0) * max(y1 - y0, 0)
    return inter / float(min(a[2] * a[3], b[2] * b[3]) or 1)


def locate_line(grey, char_height=None):
    """The best line candidate, or None."""
    boxes = line_candidates(grey, char_height, limit=1)
    return boxes[0] if boxes else None


def cut_line(grey, box):
    """The line box with a margin of about a quarter of its height."""
    x, y, w, h = box
    pad = max(int(round(h * 0.2)), 4)
    H, W = grey.shape[:2]
    return grey[max(y - pad, 0) : min(y + h + pad, H), max(x - pad, 0) : min(x + w + pad, W)]


def model_input(line, height=INPUT_HEIGHT, max_width=None):
    """Grey line crop -> (1, 1, height, W) float32, paper ~1 and ink ~0."""
    h, w = line.shape[:2]
    width = max(int(round(w * height / float(h))), 1)
    if max_width:
        width = min(width, max_width)
    resized = cv2.resize(line, (width, height), interpolation=cv2.INTER_AREA)
    return (resized.astype(np.float32) / 255.0)[None, None]


# ------------------------------------------------------------------ model
def model_dirs():
    roots = [Path(__file__).resolve().parents[2]]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.insert(1, Path(meipass))
    dirs = [Path(os.environ["OMR_IMPRINT_MODEL_DIR"])] if os.environ.get("OMR_IMPRINT_MODEL_DIR") else []
    for root in roots:
        dirs += [root / "models" / "imprint", root / "packaging" / "models" / "imprint"]
    return dirs


def find_model(name):
    name = name or DEFAULT_MODEL
    path = Path(name)
    if path.suffix == ".onnx":
        return path if path.is_file() else None
    for directory in model_dirs():
        candidate = directory / f"{name}.onnx"
        if candidate.is_file():
            return candidate
    return None


class ImprintModel:
    """A CTC line recogniser: input "x" [N, 1, 32, W], output [N, T, 1 + charset]
    with class 0 the CTC blank and class i the charset's character i - 1."""

    def __init__(self, path):
        import onnxruntime as ort

        self.path = Path(path)
        meta = json.loads(self.path.with_suffix(".json").read_text())
        self.meta = meta
        self.charset = meta.get("charset", DEFAULT_CHARSET)
        self.blank = int(meta.get("blank", 0))
        self.height = int(meta.get("input_height", INPUT_HEIGHT))
        self.max_width = meta.get("max_width")
        self.direction = meta.get("direction", "horizontal")
        options = ort.SessionOptions()
        threads = int(os.environ.get("OMR_ONNX_THREADS", "0") or 0)
        if threads:
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(self.path), options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name

    def read(self, line, allowed=None):
        """(text, per-character confidences) for one line crop."""
        logits = self.session.run(
            None, {self.input_name: model_input(line, self.height, self.max_width)}
        )[0][0]
        return ctc_greedy(logits, self.charset, self.blank, allowed)


def softmax(logits):
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


def ctc_greedy(logits, charset, blank=0, allowed=None):
    """Greedy CTC decode: (text, confidence per character = mean top
    probability over the frames of that character)."""
    probs = softmax(np.asarray(logits, dtype=np.float32))
    # Classes other than the blank are the charset, in order
    classes = [c for c in range(probs.shape[-1]) if c != blank]
    char_of = {c: charset[i] for i, c in enumerate(classes[: len(charset)])}
    if allowed:
        for c, ch in char_of.items():
            if ch not in allowed:
                probs[:, c] = 0
    best = probs.argmax(axis=-1)
    text, confidences, run, previous = [], [], [], blank
    for t, index in enumerate(best):
        if index != previous and run:
            confidences.append(float(np.mean(run)))
            run = []
        if index != blank:
            if index != previous:
                text.append(char_of.get(int(index), ""))
            run.append(float(probs[t, index]))
        previous = index
    if run:
        confidences.append(float(np.mean(run)))
    return "".join(text), confidences


def get_model(name):
    path = find_model(name)
    if path is None:
        return None
    key = str(path)
    with _LOCK:
        if key not in _MODELS:
            try:
                _MODELS[key] = ImprintModel(path)
            except Exception as error:
                logger.warning(f"Imprint model {path} did not load: {error}")
                _MODELS[key] = None
        return _MODELS[key]


def reset_cache():
    _MODELS.clear()


def installed_models():
    """Names of the imprint models found (each .onnx with its .json)."""
    names = set()
    for directory in model_dirs():
        if directory.is_dir():
            names |= {p.stem for p in directory.glob("*.onnx") if p.with_suffix(".json").is_file()}
    return sorted(names)


# ------------------------------------------------------------------ zone
def read_imprint_zone(zone, image, ocr_params=None):
    from src.readers import text_reader

    options = zone.options
    crop = zone.crop(image)
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    model = get_model(options.get("model"))
    direction = options.get("direction") or (model.direction if model else "horizontal")
    strip = text_reader.rotate_crop(crop, direction)
    details = {"direction": direction}
    boxes = line_candidates(strip, options.get("charHeight"))
    if not boxes:
        return ZoneReadResult(zone.name, zone.type, "", 0.0, ["not_found"], details=details)
    whitelist = options.get("whitelist")
    if model is None:
        details["line"] = list(boxes[0])
        line = cut_line(strip, boxes[0])
        return read_without_model(zone, line, whitelist or DEFAULT_CHARSET, details)
    # Try the best few candidates; keep the read that fits the pattern, then
    # the surest (a registration tick or bubble column can outrank faint print)
    pattern = options.get("pattern")
    best = None
    for box in boxes:
        text, confidences = model.read(cut_line(strip, box), set(whitelist) if whitelist else None)
        confidence = float(min(confidences)) if confidences else 0.0
        fits = bool(text) and (not pattern or re.fullmatch(pattern, text) is not None)
        key = (fits, confidence)
        if best is None or key > best[0]:
            best = (key, box, text, confidences, confidence)
    _, box, text, confidences, confidence = best
    details.update(
        line=list(box),
        engine="imprint",
        model=model.path.stem,
        char_confidences=[round(c, 3) for c in confidences],
    )
    flags = [] if text else ["empty"]
    return ZoneReadResult(
        zone.name, zone.type, text, confidence, flags, details=details, engine="imprint"
    )


def read_without_model(zone, line, whitelist, details):
    """Tesseract on the line with its dots joined; always reviewed."""
    from src.readers import ocr

    flags = ["no_imprint_model"]
    if not ocr.tesseract_available():
        return ZoneReadResult(
            zone.name, zone.type, "", 0.0, flags + ["engine_unavailable"], details=details
        )
    dot = max(int(round(line.shape[0] * 0.08)), 2)
    joined = cv2.morphologyEx(line, cv2.MORPH_OPEN, np.ones((dot, dot), np.uint8))
    text, confidence = ocr.recognize_text(
        ocr.prepare_for_ocr(joined), psm=7, whitelist=whitelist
    )
    return ZoneReadResult(
        zone.name, zone.type, text.replace(" ", ""), confidence, flags,
        details=details, engine="tesseract",
    )
